import Foundation
import ScreenCaptureKit
import AVFoundation
import CoreMedia

/// 系统声音采集（ScreenCaptureKit，**进程内**）。
///
/// 为什么必须进程内：CLI 助手（`helpers/main.swift`）是由服务端 `swiftc` 现场编译后拉起的
/// 临时二进制，屏幕录制权限会挂到那个临时路径上——重编译一次路径就变，权限白批
/// （"批过还是黑屏"）。放进 app 后权限记在 `com.chzy.aula` 上，配合自签证书的稳定 DR，
/// 一次授权永久有效。
///
/// 参数从 helper 原样搬来（已在真实网课场景调过）：目标 16kHz 单声道、
/// `excludesCurrentProcessAudio = true`、视频流压到 2×2 只为了拿音频。
final class SystemAudioSource: NSObject, SCStreamOutput, SCStreamDelegate {

    enum CaptureError: LocalizedError {
        case noContent
        case denied
        var errorDescription: String? {
            switch self {
            case .noContent: return "没有可捕获的显示器或窗口"
            case .denied: return "屏幕录制未授权：系统设置 → 隐私与安全性 → 屏幕录制，勾上 aula"
            }
        }
    }

    /// 16kHz 单声道 PCM16
    var onSamples: (([Int16]) -> Void)?
    /// 被系统掐断（权限收回、流错误）
    var onFailure: ((String) -> Void)?

    private var stream: SCStream?
    private let audioQueue = DispatchQueue(label: "com.chzy.aula.system-audio")
    private var srcRate: Double = 0
    private var srcChannels = 1
    private var srcInterleaved = true
    /// 首次收到音频时记录实际格式，排查「有流但全静音」用
    private(set) var formatDescription: String = ""
    private(set) var seenFormatIDs: Set<String> = []
    /// 捕获统计（诊断用）：样本数、块数、峰值
    private(set) var sampleCount = 0
    private(set) var chunkCount = 0
    private(set) var peak: Int = 0

    /// 只查不弹窗。用户在设置里能自己看到状态。
    static func hasPermission() -> Bool {
        CGPreflightScreenCaptureAccess()
    }

    /// 会弹系统授权框（首次）。返回是否已授权。
    @discardableResult
    static func requestPermission() -> Bool {
        CGPreflightScreenCaptureAccess() || CGRequestScreenCaptureAccess()
    }

    func start() async throws {
        let granted = Self.hasPermission()
        AppLog.write("sysaudio: 屏幕录制权限=\(granted ? "有" : "无")")
        guard granted || Self.requestPermission() else {
            AppLog.write("sysaudio: 权限被拒，放弃捕获")
            throw CaptureError.denied
        }
        if !granted && Self.hasPermission() {
            // 首次授权后 macOS 需要重启进程才真正放行音频——不重启会拿到静音流
            AppLog.write("sysaudio: 刚拿到授权，本次可能仍为静音（系统通常要求重启应用）")
        }
        sampleCount = 0
        chunkCount = 0
        peak = 0
        let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: true)
        AppLog.write("sysaudio: 可共享内容 displays=\(content.displays.count) windows=\(content.windows.count)")
        // 音频是系统混音，过滤器只决定视频画面取哪块屏。优先整屏；
        // 显示器列表为空（某些虚拟显示环境）时回退到按窗口捕获。
        let filter: SCContentFilter
        if let display = content.displays.first {
            filter = SCContentFilter(display: display, excludingApplications: [], exceptingWindows: [])
        } else if let window = content.windows.first(where: { $0.frame.width > 100 && $0.frame.height > 100 }) {
            filter = SCContentFilter(desktopIndependentWindow: window)
        } else {
            throw CaptureError.noContent
        }

        let config = SCStreamConfiguration()
        config.capturesAudio = true
        config.excludesCurrentProcessAudio = true
        config.sampleRate = 16000       // macOS 13+ 支持；不生效时有线性重采样兜底
        config.channelCount = 1
        config.width = 2                // 视频流压到最小，只为拿音频
        config.height = 2
        config.minimumFrameInterval = CMTime(value: 1, timescale: 2)
        config.queueDepth = 8

        let stream = SCStream(filter: filter, configuration: config, delegate: self)
        self.stream = stream
        srcRate = 0
        try stream.addStreamOutput(self, type: .audio, sampleHandlerQueue: audioQueue)
        try await stream.startCapture()
        AppLog.write("sysaudio: startCapture 成功")
    }

    func stop() {
        guard let stream else { return }
        let semaphore = DispatchSemaphore(value: 0)
        Task {
            try? await stream.stopCapture()
            semaphore.signal()
        }
        // 给 stopCapture 几秒收尾；等不到也不能把 UI 卡死
        _ = semaphore.wait(timeout: .now() + 3)
        self.stream = nil
    }

    // MARK: - SCStreamOutput

    func stream(_ stream: SCStream, didOutputSampleBuffer sampleBuffer: CMSampleBuffer, of type: SCStreamOutputType) {
        guard type == .audio, sampleBuffer.isValid, CMSampleBufferDataIsReady(sampleBuffer) else { return }
        audioQueue.async { [weak self] in
            self?.consume(sampleBuffer: sampleBuffer)
        }
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        onFailure?("捕获被系统终止：\(error.localizedDescription)（多半是屏幕录制权限被收回）")
    }

    // MARK: - 样本 → Int16

    private func consume(sampleBuffer: CMSampleBuffer) {
        guard let formatDesc = CMSampleBufferGetFormatDescription(sampleBuffer),
              let asbdPointer = CMAudioFormatDescriptionGetStreamBasicDescription(formatDesc) else { return }
        let asbd = asbdPointer.pointee
        if srcRate == 0 {
            srcRate = asbd.mSampleRate
            srcChannels = max(1, Int(asbd.mChannelsPerFrame))
            srcInterleaved = (asbd.mFormatFlags & kAudioFormatFlagIsNonInterleaved) == 0
            formatDescription = "rate=\(Int(asbd.mSampleRate)) ch=\(srcChannels) "
                + "flags=0x\(String(asbd.mFormatFlags, radix: 16)) "
                + "bits=\(asbd.mBitsPerChannel) bytesPerFrame=\(asbd.mBytesPerFrame) "
                + "formatID=\(asbd.mFormatID)"
        }

        // 取出 AudioBufferList（两步：先问尺寸，再取内容）
        var neededSize = 0
        var blockBuffer: CMBlockBuffer?
        var status = CMSampleBufferGetAudioBufferListWithRetainedBlockBuffer(
            sampleBuffer, bufferListSizeNeededOut: &neededSize, bufferListOut: nil, bufferListSize: 0,
            blockBufferAllocator: kCFAllocatorDefault, blockBufferMemoryAllocator: kCFAllocatorDefault,
            flags: UInt32(kCMSampleBufferFlag_AudioBufferList_Assure16ByteAlignment),
            blockBufferOut: &blockBuffer)
        guard status == noErr, neededSize > 0 else { return }
        let listMemory = UnsafeMutableRawPointer.allocate(byteCount: neededSize, alignment: 16)
        defer { listMemory.deallocate() }
        let abl = listMemory.bindMemory(to: AudioBufferList.self, capacity: 1)
        status = CMSampleBufferGetAudioBufferListWithRetainedBlockBuffer(
            sampleBuffer, bufferListSizeNeededOut: nil, bufferListOut: abl, bufferListSize: neededSize,
            blockBufferAllocator: kCFAllocatorDefault, blockBufferMemoryAllocator: kCFAllocatorDefault,
            flags: UInt32(kCMSampleBufferFlag_AudioBufferList_Assure16ByteAlignment),
            blockBufferOut: &blockBuffer)
        guard status == noErr else { return }

        let buffers = UnsafeMutableAudioBufferListPointer(abl)
        var floats: [Double] = []
        if srcInterleaved {
            let audioBuffer = buffers[0]
            let count = Int(audioBuffer.mDataByteSize) / MemoryLayout<Float>.size
            guard count > 0, let base = audioBuffer.mData?.bindMemory(to: Float.self, capacity: count) else { return }
            let pointer = UnsafePointer(base)
            var index = 0
            while index + srcChannels <= count {
                var mix = 0.0
                for channel in 0..<srcChannels { mix += Double(pointer[index + channel]) }
                floats.append(mix / Double(srcChannels))
                index += srcChannels
            }
        } else {
            let frames = Int(buffers[0].mDataByteSize) / MemoryLayout<Float>.size
            var pointers: [UnsafePointer<Float>] = []
            for channel in 0..<min(buffers.count, srcChannels) {
                let audioBuffer = buffers[channel]
                let count = Int(audioBuffer.mDataByteSize) / MemoryLayout<Float>.size
                guard count >= frames,
                      let base = audioBuffer.mData?.bindMemory(to: Float.self, capacity: count) else { return }
                pointers.append(UnsafePointer(base))
            }
            guard !pointers.isEmpty, frames > 0 else { return }
            floats.reserveCapacity(frames)
            for frame in 0..<frames {
                var mix = 0.0
                for pointer in pointers { mix += Double(pointer[frame]) }
                floats.append(mix / Double(pointers.count))
            }
        }

        // 若 SCK 没按配置给 16k（老系统会忽略 sampleRate），线性重采样兜底。
        // 逐块插值会有极小的块间不连续，语音识别完全够用——与 helper 保持同一口径。
        var resampled = floats
        if abs(srcRate - 16000) > 1 {
            resampled = Self.linearResample(floats, from: srcRate, to: 16000)
        }
        let int16s: [Int16] = resampled.map { value in
            let clamped = max(-1.0, min(1.0, value))
            return Int16((clamped < 0 ? clamped * 32768.0 : clamped * 32767.0).rounded())
        }
        if !int16s.isEmpty {
            let localPeak = int16s.reduce(0) { max($0, abs(Int($1))) }
            sampleCount += int16s.count
            chunkCount += 1
            peak = max(peak, localPeak)
            // 每 2 秒记一次：能看出"有流但一直静音"，这是权限问题的典型症状
            if chunkCount % 40 == 0 {
                AppLog.write("sysaudio: chunks=\(chunkCount) samples=\(sampleCount) peak=\(peak) srcRate=\(Int(srcRate))")
            }
            onSamples?(int16s)
        }
    }

    private static func linearResample(_ input: [Double], from: Double, to: Double) -> [Double] {
        if input.count < 2 { return [] }
        let ratio = from / to
        var output: [Double] = []
        output.reserveCapacity(Int(Double(input.count) / ratio) + 1)
        var sourcePosition = 0.0
        while sourcePosition < Double(input.count - 1) {
            let index = Int(sourcePosition)
            let fraction = sourcePosition - Double(index)
            output.append(input[index] * (1 - fraction) + input[index + 1] * fraction)
            sourcePosition += ratio
        }
        return output
    }
}
