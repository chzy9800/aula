// aula 系统音频助手：ScreenCaptureKit 抓电脑正在播放的声音 → 16k 单声道 PCM
// → 与浏览器 worklet 同口径的 VAD 切句 → 按 aula 的 WS 二进制帧协议推送。
//
// 为什么存在：macOS 上浏览器页面拿不到系统级音频（getDisplayMedia 只给标签页
// 音频），抓系统声音需要 ScreenCaptureKit 这个原生 API——AirTranslate 走的
// 就是这条路（见 docs/reverse-engineering-airtranslate.md）。
//
// 编译（无需 Xcode，CommandLineTools 即可；文件须以 main.swift 名义编译，
// aula 的编译步骤会自动复制——见 server.py 的 _ensure_helper_binary）：
//   swiftc -O -swift-version 5 -o ~/.aula/bin/aula-system-audio main.swift
// 运行：
//   aula-system-audio --url ws://127.0.0.1:8780/ws/live?session_id=<id>
// 首次运行会触发「屏幕录制」权限弹窗：对运行它的终端/应用打勾，再重跑。
//
// 协议与浏览器完全一致：帧 = uint32 LE start_ms + uint32 LE end_ms + PCM16。
// 服务端与前端不需要任何改动即可消化这些帧。

import Foundation
import ScreenCaptureKit
import AVFoundation
import CoreMedia

func log(_ message: String) {
    FileHandle.standardError.write(("【aula】" + message + "\n").data(using: .utf8)!)
}

// MARK: - 命令行参数

struct Args {
    var url = ""
    var silenceMs: Double = 700
    var minSpeechMs: Double = 350
    var maxSegmentMs: Double = 15000
    var verbose = false
    var listOnly = false

    static func parse(_ argv: [String]) -> Args? {
        var args = Args()
        var iterator = argv.makeIterator()
        while let arg = iterator.next() {
            func value(_ name: String) -> String? {
                if arg == name { return iterator.next() }
                if arg.hasPrefix(name + "=") { return String(arg.dropFirst(name.count + 1)) }
                return nil
            }
            switch arg {
            case "--url": args.url = value("--url") ?? ""
            case "--silence-ms": args.silenceMs = Double(value("--silence-ms") ?? "") ?? args.silenceMs
            case "--min-speech-ms": args.minSpeechMs = Double(value("--min-speech-ms") ?? "") ?? args.minSpeechMs
            case "--max-segment-ms": args.maxSegmentMs = Double(value("--max-segment-ms") ?? "") ?? args.maxSegmentMs
            case "--verbose": args.verbose = true
            case "--list": args.listOnly = true
            case "--help", "-h":
                print("用法: aula-system-audio --url ws://…/ws/live?session_id=<id> [--silence-ms 700] [--min-speech-ms 350] [--max-segment-ms 15000] [--list] [--verbose]")
                return nil
            default: break
            }
        }
        return args
    }
}

// MARK: - VAD 切句（与浏览器 worklet 逐行同口径）

final class VadSegmenter {
    let targetRate: Double = 16000
    let windowSamples = 320          // 20ms @16k
    let silenceMs: Double
    let minSpeechMs: Double
    let maxSegmentMs: Double

    // 自适应噪声底与阈值：初值与 worklet 相同（0.004 / 3x / 2.5x / 硬下限 0.010 / 0.008）。
    // 系统音频的噪声底通常比麦克风低得多，自适应逻辑会自己学出来。
    var noise = 0.004
    var speaking = false
    var chunk: [Int16] = []
    var segSamples = 0
    var silenceSamples = 0
    var startSample = 0
    var lastVoice = 0
    var clock = 0

    var onSegment: ((Data, UInt32, UInt32) -> Void)?
    private var windowCount = 0
    private var pending: [Int16] = []

    init(silenceMs: Double, minSpeechMs: Double, maxSegmentMs: Double) {
        self.silenceMs = silenceMs
        self.minSpeechMs = minSpeechMs
        self.maxSegmentMs = maxSegmentMs
    }

    func feed(samples: [Int16]) {
        var position = 0
        while position + windowSamples <= samples.count {
            handleWindow(Array(samples[position..<position + windowSamples]))
            position += windowSamples
        }
        // 尾部不足一个窗口的零头滚到下一批
        if position < samples.count {
            pending.append(contentsOf: samples[position...])
            if pending.count >= windowSamples {
                let remainder = pending
                pending.removeAll(keepingCapacity: true)
                feed(samples: remainder)
            }
        }
    }

    private func handleWindow(_ window: [Int16]) {
        var sum = 0.0
        for sample in window {
            let value = Double(sample) / 32768.0
            sum += value * value
        }
        let rms = (sum / Double(window.count)).squareRoot()

        if !speaking {
            noise = noise * 0.97 + rms * 0.03
            let threshold = max(0.010, noise * 3)
            if rms > threshold {
                speaking = true
                chunk = window
                segSamples = window.count
                silenceSamples = 0
                startSample = clock
                lastVoice = window.count
            }
            clock += window.count
        } else {
            chunk.append(contentsOf: window)
            segSamples += window.count
            clock += window.count
            let threshold = max(0.008, noise * 2.5)
            if rms < threshold {
                silenceSamples += window.count
            } else {
                silenceSamples = 0
                lastVoice = segSamples
            }
            if Double(silenceSamples) / targetRate * 1000 >= silenceMs
                || Double(segSamples) / targetRate * 1000 >= maxSegmentMs {
                flush()
            }
        }

        windowCount += 1
    }

    private func flush() {
        guard speaking else { return }
        let total = segSamples
        let minSamples = Int(minSpeechMs / 1000 * targetRate)
        // 裁掉尾部静音（留 100ms 余量），与 worklet 一致：触发断句的那段静音
        // 对识别毫无用处，留着只会让模型在空白上瞎猜。
        let pad = Int(targetRate * 0.1)
        var keep = min(total, max(lastVoice, 0) + pad)
        if keep < minSamples { keep = total }
        keep = min(keep, total)

        if total >= minSamples {
            let payload = chunk.prefix(keep)
            var data = Data(capacity: 8 + keep * 2)
            let startMs = UInt32((Double(startSample) / targetRate) * 1000)
            let endMs = UInt32((Double(startSample + keep) / targetRate) * 1000)
            withUnsafeBytes(of: startMs.littleEndian) { data.append(contentsOf: $0) }
            withUnsafeBytes(of: endMs.littleEndian) { data.append(contentsOf: $0) }
            payload.withUnsafeBufferPointer { buffer in
                data.append(contentsOf: UnsafeRawBufferPointer(buffer))
            }
            onSegment?(data, startMs, endMs)
        }
        speaking = false
        chunk = []
        segSamples = 0
        silenceSamples = 0
        lastVoice = 0
    }
}

// MARK: - WebSocket 发送

final class WsSender {
    private let task: URLSessionWebSocketTask

    init?(url: URL) {
        let session = URLSession(configuration: .default)
        let task = session.webSocketTask(with: url)
        self.task = task
        task.resume()
        drain()
    }

    func send(_ data: Data) {
        task.send(.data(data)) { error in
            if let error { log("发送失败：" + error.localizedDescription) }
        }
    }

    // 服务端会推 ready/translation 等文本消息，收掉以免积压；内容忽略。
    private func drain() {
        task.receive { [weak self] result in
            guard let self else { return }
            switch result {
            case .success: self.drain()
            case .failure(let error): log("连接断开：" + error.localizedDescription)
            }
        }
    }
}

// MARK: - 系统音频捕获（ScreenCaptureKit）

enum CaptureError: LocalizedError {
    case message(String)
    var errorDescription: String? {
        if case .message(let text) = self { return text }
        return nil
    }
}

final class SystemAudioCapture: NSObject, SCStreamOutput, SCStreamDelegate {
    private var stream: SCStream?
    private var sender: WsSender?
    private var segmenter: VadSegmenter?
    private let audioQueue = DispatchQueue(label: "aula.capture")
    // 采样率与声道以实际送达的 ASBD 为准；不等 16k/单声道时走线性重采样兜底
    private var srcRate: Double = 0
    private var srcChannels = 1
    private var srcInterleaved = true
    private var captureStart = Date()

    func listContent() async throws {
        let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: true)
        log("权限正常：\(content.displays.count) 块屏幕、\(content.windows.count) 个窗口")
    }

    func start(url: URL, args: Args) async throws {
        sender = WsSender(url: url)
        segmenter = VadSegmenter(silenceMs: args.silenceMs, minSpeechMs: args.minSpeechMs, maxSegmentMs: args.maxSegmentMs)
        segmenter?.onSegment = { [weak self] data, _, _ in
            self?.sender?.send(data)
        }

        let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: true)
        // 音频是系统混音，过滤器只决定视频画面取哪块屏。优先整屏；
        // 显示器列表为空（某些虚拟显示环境）时回退到按窗口捕获。
        let filter: SCContentFilter
        if let display = content.displays.first {
            filter = SCContentFilter(display: display, excludingApplications: [], exceptingWindows: [])
        } else if let window = content.windows.first(where: { $0.frame.width > 100 && $0.frame.height > 100 }) {
            log("没有可用显示器，回退为按窗口捕获：「\(window.title)」")
            filter = SCContentFilter(desktopIndependentWindow: window)
        } else {
            throw CaptureError.message("没有可捕获的显示器或窗口")
        }
        let config = SCStreamConfiguration()
        config.capturesAudio = true
        config.excludesCurrentProcessAudio = true
        config.sampleRate = 16000       // macOS 13+ 支持；不生效时代码里有线性重采样兜底
        config.channelCount = 1
        config.width = 2
        config.height = 2
        config.minimumFrameInterval = CMTime(value: 1, timescale: 2)   // 0.5s 一帧视频，越少越好
        config.queueDepth = 8

        let stream = SCStream(filter: filter, configuration: config, delegate: self)
        self.stream = stream
        try stream.addStreamOutput(self, type: .audio, sampleHandlerQueue: audioQueue)
        captureStart = Date()
        try await stream.startCapture()
        log("已开始捕获系统音频（目标 16kHz 单声道；VAD：静音 \(Int(args.silenceMs))ms / 最短 \(Int(args.minSpeechMs))ms / 上限 \(Int(args.maxSegmentMs))ms）")
    }

    func stop() {
        guard let stream else { return }
        let semaphore = DispatchSemaphore(value: 0)
        Task {
            try? await stream.stopCapture()
            semaphore.signal()
        }
        semaphore.wait()
        self.stream = nil
    }

    // SCStreamOutput：音频样本在这里到达
    func stream(_ stream: SCStream, didOutputSampleBuffer sampleBuffer: CMSampleBuffer, of type: SCStreamOutputType) {
        guard type == .audio, sampleBuffer.isValid else { return }
        guard CMSampleBufferDataIsReady(sampleBuffer) else { return }
        audioQueue.async { [weak self] in
            self?.consume(sampleBuffer: sampleBuffer)
        }
    }

    // SCStreamDelegate：被系统掐断（权限收回等）时如实退出，让 aula 看得到
    func stream(_ stream: SCStream, didStopWithError error: Error) {
        log("捕获被系统终止：" + error.localizedDescription + "（多半是屏幕录制权限被收回）")
        exit(3)
    }

    private func consume(sampleBuffer: CMSampleBuffer) {
        guard let formatDesc = CMSampleBufferGetFormatDescription(sampleBuffer),
              let asbdPointer = CMAudioFormatDescriptionGetStreamBasicDescription(formatDesc) else { return }
        let asbd = asbdPointer.pointee
        if srcRate == 0 {
            srcRate = asbd.mSampleRate
            srcChannels = max(1, Int(asbd.mChannelsPerFrame))
            srcInterleaved = (asbd.mFormatFlags & kAudioFormatFlagIsNonInterleaved) == 0
            log("实际音频格式：\(Int(srcRate)) Hz × \(srcChannels) 声道（\(srcInterleaved ? "交织" : "非交织")）")
            if abs(srcRate - 16000) > 1 || srcChannels != 1 {
                log("与目标 16kHz/单声道不一致，走线性重采样兜底")
            }
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
            // 单个 buffer，样本按声道交织
            let audioBuffer = buffers[0]
            let count = Int(audioBuffer.mDataByteSize) / MemoryLayout<Float>.size
            guard count > 0, let base = audioBuffer.mData?.bindMemory(to: Float.self, capacity: count) else { return }
            let pointer = UnsafePointer(base)
            var index = 0
            while index + srcChannels <= count {
                var mix = 0.0
                for ch in 0..<srcChannels { mix += Double(pointer[index + ch]) }
                floats.append(mix / Double(srcChannels))
                index += srcChannels
            }
        } else {
            // 每声道一个 buffer，逐帧平均
            let frames = Int(buffers[0].mDataByteSize) / MemoryLayout<Float>.size
            var pointers: [UnsafePointer<Float>] = []
            for ch in 0..<min(buffers.count, srcChannels) {
                let audioBuffer = buffers[ch]
                let count = Int(audioBuffer.mDataByteSize) / MemoryLayout<Float>.size
                guard count >= frames, let base = audioBuffer.mData?.bindMemory(to: Float.self, capacity: count) else { return }
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

        // 重采样到 16k（若 SCK 没有按配置给采样率）
        var resampled = floats
        if abs(srcRate - 16000) > 1 {
            resampled = Self.linearResample(floats, from: srcRate, to: 16000)
        }
        let int16s: [Int16] = resampled.map { value in
            let clamped = max(-1.0, min(1.0, value))
            return Int16((clamped < 0 ? clamped * 32768.0 : clamped * 32767.0).rounded())
        }
        segmenter?.feed(samples: int16s)
    }

    // 线性插值重采样：语音识别够用，也不用把 AVAudioConverter 的拉取模型拖进来
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

// MARK: - 入口（top-level code，须以 main.swift 名义编译）

let args = Args.parse(CommandLine.arguments)
if args == nil { exit(0) }
let options = args!

let capture = SystemAudioCapture()

if options.listOnly {
    do {
        try await capture.listContent()
    } catch {
        log("拿不到可共享内容：\(String(describing: error))")
        log("→ 需要在「系统设置 → 隐私与安全性 → 屏幕录制」里，给运行本程序的终端/应用打勾，然后重跑。")
        exit(2)
    }
    exit(0)
}

if options.url.isEmpty {
    log("缺少 --url。示例：--url ws://127.0.0.1:8780/ws/live?session_id=<id>")
    exit(1)
}
guard let wsURL = URL(string: options.url) else {
    log("--url 不是合法地址")
    exit(1)
}

// 权限弹窗出现在「拉起本进程的宿主」（终端/aula 服务端）身上，批一次就永久有效
do {
    try await capture.listContent()
    try await capture.start(url: wsURL, args: options)
} catch {
    log("启动失败：\(String(describing: error))")
    log("→ 若是权限问题：系统设置 → 隐私与安全性 → 屏幕录制，给运行 aula 的终端/应用打勾，然后重新开始听课。")
    exit(2)
}

log("连接 aula：\(options.url)")
log("开始听课吧——识别结果会出现在 aula 页面上。Ctrl-C 结束。")

// 优雅退出
signal(SIGINT, SIG_IGN)
signal(SIGTERM, SIG_IGN)
let stopSignal = DispatchSource.makeSignalSource(signal: SIGINT, queue: .global())
stopSignal.setEventHandler {
    log("收到退出信号，收尾…")
    capture.stop()
    exit(0)
}
stopSignal.resume()
let termSignal = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .global())
termSignal.setEventHandler {
    capture.stop()
    exit(0)
}
termSignal.resume()

// 常驻。不能用 dispatchMain()：顶层 async 代码跑在主队列上，
// libdispatch 会直接 trap（BUG IN CLIENT OF LIBDISPATCH，实测崩过）。
// 信号量驻留会占住一个协作线程，但本进程的其余工作都在自己的
// DispatchQueue 与 SCK 内部队列上，不受影响；进程由信号处理器退出。
let park = DispatchSemaphore(value: 0)
park.wait()
