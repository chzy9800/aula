// aula 菜单栏应用：macOS 上的「上课遥控器 + 悬浮字幕」。
//
// 形态对标 AirTranslate 的日常用法：常驻菜单栏，上课点一下开始，
// 屏幕角落浮着双语字幕，下课点结束。转写与翻译仍由 aula 服务端完成
// （本地 whisper + 网关），本应用负责：系统音频捕获、生命周期、显示。
//
// 权限：屏幕录制权限批给本 app 自己（bundle id 固定，批一次永久有效），
// 不再挂在终端上——这是独立 app 相对 CLI 助手最大的体验差异。
//
// 编译打包见 scripts/build_menubar.sh：
//   swiftc -O -swift-version 5 -parse-as-library -o <bin> menubar/main.swift

import Foundation
import AppKit
import ScreenCaptureKit
import AVFoundation
import CoreMedia

let kBaseURL = "http://127.0.0.1:8780"
let kTargetRate: Double = 16000

func log(_ message: String) {
    FileHandle.standardError.write(("【aula】" + message + "\n").data(using: .utf8)!)
}

// MARK: - 状态

final class AppState {
    static let shared = AppState()
    var sessionId: String?
    var recording = false
    var startedAt: Date?
    var helperStarted = false
}

// MARK: - HTTP

func httpJSON(_ method: String, _ path: String, payload: [String: Any]? = nil,
              timeout: TimeInterval = 8) -> [String: Any]? {
    guard let url = URL(string: kBaseURL + path) else { return nil }
    var request = URLRequest(url: url, timeoutInterval: timeout)
    request.httpMethod = method
    if let payload {
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try? JSONSerialization.data(withJSONObject: payload)
    }
    var result: [String: Any]?
    let semaphore = DispatchSemaphore(value: 0)
    URLSession.shared.dataTask(with: request) { data, _, _ in
        if let data, let parsed = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
            result = parsed
        }
        semaphore.signal()
    }.resume()
    semaphore.wait()
    return result
}

// MARK: - VAD 切句（与浏览器 worklet / CLI 助手同口径）

final class VadSegmenter {
    let targetRate: Double = 16000
    let windowSamples = 320
    let silenceMs: Double
    let minSpeechMs: Double
    let maxSegmentMs: Double

    var noise = 0.004
    var speaking = false
    var chunk: [Int16] = []
    var segSamples = 0
    var silenceSamples = 0
    var startSample = 0
    var lastVoice = 0
    var clock = 0
    var onSegment: ((Data) -> Void)?
    var onPartial: ((Data) -> Void)?
    let partialMs: Double = 900
    let partialWindowSamples = 8000 * 16
    private var pending: [Int16] = []
    private var windowCount = 0
    private var sincePartial = 0

    init(silenceMs: Double = 700, minSpeechMs: Double = 350, maxSegmentMs: Double = 15000) {
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
            if rms > max(0.010, noise * 3) {
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
            if rms < max(0.008, noise * 2.5) {
                silenceSamples += window.count
            } else {
                silenceSamples = 0
                lastVoice = segSamples
            }
            if Double(silenceSamples) / targetRate * 1000 >= silenceMs
                || Double(segSamples) / targetRate * 1000 >= maxSegmentMs {
                flush()
            } else {
                sincePartial += windowSamples
                if sincePartial >= Int(partialMs / 1000 * targetRate) {
                    sincePartial = 0
                    emitPartial()
                }
            }
        }
        windowCount += 1
    }

    /// 流式草稿：flags=1，只带最近 8 秒。字幕窗"边说边出字"靠它。
    private func emitPartial() {
        guard speaking, segSamples >= Int(minSpeechMs / 1000 * targetRate) else { return }
        let total = segSamples
        let keep = min(total, partialWindowSamples)
        let skip = total - keep
        let payload = chunk.suffix(keep)
        var data = Data(capacity: 12 + keep * 2)
        let startMs = UInt32((Double(startSample + skip) / targetRate) * 1000)
        let endMs = UInt32((Double(startSample + total) / targetRate) * 1000)
        withUnsafeBytes(of: startMs.littleEndian) { data.append(contentsOf: $0) }
        withUnsafeBytes(of: endMs.littleEndian) { data.append(contentsOf: $0) }
        withUnsafeBytes(of: UInt32(1).littleEndian) { data.append(contentsOf: $0) }
        payload.withUnsafeBufferPointer { buffer in
            data.append(contentsOf: UnsafeRawBufferPointer(buffer))
        }
        onPartial?(data)
    }

    private func flush() {
        guard speaking else { return }
        let total = segSamples
        let minSamples = Int(minSpeechMs / 1000 * targetRate)
        let pad = Int(targetRate * 0.1)
        var keep = min(total, max(lastVoice, 0) + pad)
        if keep < minSamples { keep = total }
        keep = min(keep, total)
        if total >= minSamples {
            let payload = chunk.prefix(keep)
            var data = Data(capacity: 12 + keep * 2)
            let startMs = UInt32((Double(startSample) / targetRate) * 1000)
            let endMs = UInt32((Double(startSample + keep) / targetRate) * 1000)
            withUnsafeBytes(of: startMs.littleEndian) { data.append(contentsOf: $0) }
            withUnsafeBytes(of: endMs.littleEndian) { data.append(contentsOf: $0) }
            withUnsafeBytes(of: UInt32(0).littleEndian) { data.append(contentsOf: $0) }
            payload.withUnsafeBufferPointer { buffer in
                data.append(contentsOf: UnsafeRawBufferPointer(buffer))
            }
            onSegment?(data)
        }
        speaking = false
        chunk = []
        segSamples = 0
        silenceSamples = 0
        sincePartial = 0
        lastVoice = 0
    }
}

// MARK: - 系统音频捕获（ScreenCaptureKit，与 CLI 助手同款）

final class SystemAudioCapture: NSObject, SCStreamOutput, SCStreamDelegate {
    private var stream: SCStream?
    private var segmenter: VadSegmenter?
    var onSegment: ((Data) -> Void)?
    var onPartial: ((Data) -> Void)?
    var onError: ((String) -> Void)?
    private let audioQueue = DispatchQueue(label: "aula.menubar.capture")
    private var srcRate: Double = 0
    private var srcChannels = 1
    private var srcInterleaved = true

    func start(onSegment: @escaping (Data) -> Void, onPartial: @escaping (Data) -> Void, onError: @escaping (String) -> Void) async throws {
        self.onSegment = onSegment
        self.onPartial = onPartial
        self.onError = onError
        segmenter = VadSegmenter()
        segmenter?.onSegment = { [weak self] data in
            self?.onSegment?(data)
        }
        segmenter?.onPartial = { [weak self] data in
            self?.onPartial?(data)
        }

        let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: true)
        let filter: SCContentFilter
        if let display = content.displays.first {
            filter = SCContentFilter(display: display, excludingApplications: [], exceptingWindows: [])
        } else if let window = content.windows.first(where: { $0.frame.width > 100 && $0.frame.height > 100 }) {
            filter = SCContentFilter(desktopIndependentWindow: window)
        } else {
            throw CaptureError.message("没有可捕获的显示器或窗口")
        }
        let config = SCStreamConfiguration()
        config.capturesAudio = true
        config.excludesCurrentProcessAudio = true
        config.sampleRate = 16000
        config.channelCount = 1
        config.width = 2
        config.height = 2
        config.minimumFrameInterval = CMTime(value: 1, timescale: 2)
        config.queueDepth = 8

        let stream = SCStream(filter: filter, configuration: config, delegate: self)
        self.stream = stream
        try stream.addStreamOutput(self, type: .audio, sampleHandlerQueue: audioQueue)
        try await stream.startCapture()
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

    enum CaptureError: LocalizedError {
        case message(String)
        var errorDescription: String? {
            if case .message(let text) = self { return text }
            return nil
        }
    }

    func stream(_ stream: SCStream, didOutputSampleBuffer sampleBuffer: CMSampleBuffer, of type: SCStreamOutputType) {
        guard type == .audio, sampleBuffer.isValid, CMSampleBufferDataIsReady(sampleBuffer) else { return }
        audioQueue.async { [weak self] in
            self?.consume(sampleBuffer: sampleBuffer)
        }
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        onError?("捕获被系统终止：" + error.localizedDescription)
    }

    private func consume(sampleBuffer: CMSampleBuffer) {
        guard let formatDesc = CMSampleBufferGetFormatDescription(sampleBuffer),
              let asbdPointer = CMAudioFormatDescriptionGetStreamBasicDescription(formatDesc) else { return }
        let asbd = asbdPointer.pointee
        if srcRate == 0 {
            srcRate = asbd.mSampleRate
            srcChannels = max(1, Int(asbd.mChannelsPerFrame))
            srcInterleaved = (asbd.mFormatFlags & kAudioFormatFlagIsNonInterleaved) == 0
        }

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
                for ch in 0..<srcChannels { mix += Double(pointer[index + ch]) }
                floats.append(mix / Double(srcChannels))
                index += srcChannels
            }
        } else {
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

        var resampled = floats
        if abs(srcRate - kTargetRate) > 1 {
            resampled = Self.linearResample(floats, from: srcRate, to: kTargetRate)
        }
        let int16s: [Int16] = resampled.map { value in
            let clamped = max(-1.0, min(1.0, value))
            return Int16((clamped < 0 ? clamped * 32768.0 : clamped * 32767.0).rounded())
        }
        segmenter?.feed(samples: int16s)
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

// MARK: - 悬浮字幕窗

final class CaptionWindow {
    private let panel: NSPanel
    private let stack: NSStackView
    private var lines: [(source: String, target: String)] = []

    init() {
        panel = NSPanel(contentRect: NSRect(x: 0, y: 0, width: 460, height: 150),
                        styleMask: [.borderless, .nonactivatingPanel],
                        backing: .buffered, defer: false)
        panel.level = .floating
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = true
        panel.collectionBehavior = [.canJoinAllSpaces, .ignoresCycle]
        panel.isMovableByWindowBackground = true

        let effect = NSVisualEffectView(frame: NSRect(x: 0, y: 0, width: 460, height: 150))
        effect.material = .hudWindow
        effect.state = .active
        effect.wantsLayer = true
        effect.layer?.cornerRadius = 16
        effect.layer?.masksToBounds = true

        stack = NSStackView()
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 6
        stack.translatesAutoresizingMaskIntoConstraints = false
        effect.addSubview(stack)
        panel.contentView = effect
        NSLayoutConstraint.activate([
            stack.topAnchor.constraint(equalTo: effect.topAnchor, constant: 12),
            stack.leadingAnchor.constraint(equalTo: effect.leadingAnchor, constant: 16),
            stack.trailingAnchor.constraint(lessThanOrEqualTo: effect.trailingAnchor, constant: -16),
            stack.bottomAnchor.constraint(lessThanOrEqualTo: effect.bottomAnchor, constant: -12),
        ])
        reposition()
        render()
    }

    private func reposition() {
        if let screen = NSScreen.main {
            let visible = screen.visibleFrame
            panel.setFrameOrigin(NSPoint(x: visible.maxX - panel.frame.width - 24,
                                         y: visible.maxY - panel.frame.height - 24))
        }
    }

    func toggle() {
        if panel.isVisible {
            panel.orderOut(nil)
        } else {
            render()
            panel.makeKeyAndOrderFront(nil)
        }
    }

    var visible: Bool { panel.isVisible }

    private var draftActive = false

    /// 流式草稿：说话过程中每秒刷新一次，末行原地覆盖，不上新行。
    func updateDraft(source: String, target: String) {
        if draftActive, !lines.isEmpty {
            lines[lines.count - 1] = (source, target)
        } else {
            lines.append((source, target))
            if lines.count > 3 { lines.removeFirst(lines.count - 3) }
            draftActive = true
        }
        if panel.isVisible { render() }
    }

    /// 定稿：草稿行原位转正；没有草稿才上新行。
    func update(source: String, target: String) {
        let wasDraft = draftActive
        draftActive = false
        if let last = lines.last, last.source == source {
            lines[lines.count - 1].target = target
        } else if wasDraft, !lines.isEmpty {
            lines[lines.count - 1] = (source, target)
        } else {
            lines.append((source, target))
            if lines.count > 3 { lines.removeFirst(lines.count - 3) }
        }
        if panel.isVisible { render() }
    }

    /// 精译回填：原文先到时先渲染了快速粗译（或空），LLM 精译到齐后覆盖。
    func updateLastTarget(_ target: String) {
        guard !target.isEmpty, let last = lines.last, last.target != target else { return }
        lines[lines.count - 1].target = target
        if panel.isVisible { render() }
    }

    func clear() {
        lines = []
        draftActive = false
        render()
    }

    private func render() {
        stack.views.forEach { stack.removeView($0) }
        if lines.isEmpty {
            stack.addArrangedSubview(makeLabel("✓ aula 已在菜单栏运行", size: 13, dim: false, bold: true))
            stack.addArrangedSubview(makeLabel("点右上角菜单栏的波形图标，选「开始听课」。", size: 11, dim: true))
            stack.addArrangedSubview(makeLabel("上课时，双语字幕会浮在这里。", size: 11, dim: true))
        }
        for line in lines.reversed() {
            if !line.target.isEmpty {
                stack.addArrangedSubview(makeLabel(line.target, size: 15, dim: false, bold: true))
            }
            stack.addArrangedSubview(makeLabel(line.source, size: 11, dim: true))
        }
        panel.layoutIfNeeded()
    }

    private func makeLabel(_ text: String, size: CGFloat, dim: Bool, bold: Bool = false) -> NSTextField {
        let label = NSTextField(labelWithString: text)
        label.font = bold ? .systemFont(ofSize: size, weight: .semibold) : .systemFont(ofSize: size)
        label.textColor = dim ? NSColor(calibratedWhite: 0.75, alpha: 1) : NSColor.white
        label.lineBreakMode = .byTruncatingTail
        label.maximumNumberOfLines = 2
        label.preferredMaxLayoutWidth = 428
        return label
    }
}

// MARK: - 服务端拉起

enum ServeManager {
    static var process: Process?

    static func isHealthy() -> Bool {
        httpJSON("GET", "/api/health", timeout: 2)?["status"] as? String == "ok"
    }

    @discardableResult
    static func ensureRunning() -> Bool {
        if isHealthy() { return true }
        let candidates = [
            ("\(NSHomeDirectory())/.workbuddy/binaries/python/envs/escucha/bin/python", "-m aula serve"),
        ]
        for (python, arguments) in candidates {
            guard FileManager.default.isExecutableFile(atPath: python) else { continue }
            let proc = Process()
            proc.executableURL = URL(fileURLWithPath: python)
            proc.arguments = arguments.split(separator: " ").map(String.init)
            proc.standardOutput = FileHandle.nullDevice
            proc.standardError = FileHandle.nullDevice
            do {
                try proc.run()
                process = proc
            } catch {
                continue
            }
            // 最多等 8 秒健康
            for _ in 0..<40 {
                if isHealthy() { return true }
                Thread.sleep(forTimeInterval: 0.2)
            }
        }
        return false
    }
}

// MARK: - 应用委托

final class AppDelegate: NSObject, NSApplicationDelegate {
    private var statusItem: NSStatusItem!
    private let captions = CaptionWindow()
    private var capture: SystemAudioCapture?
    private var wsTask: URLSessionWebSocketTask?
    private var sessionItem: NSMenuItem!
    private var toggleItem: NSMenuItem!
    private var captionToggleItem: NSMenuItem!
    private var serverItem: NSMenuItem!
    private var timer: Timer?

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)   // 常驻菜单栏，无 Dock 图标

        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        statusItem.button?.image = NSImage(systemSymbolName: "waveform.badge.magnifyingglass",
                                           accessibilityDescription: "aula")

        let menu = NSMenu()
        sessionItem = NSMenuItem(title: "待机", action: nil, keyEquivalent: "")
        sessionItem.isEnabled = false
        menu.addItem(sessionItem)
        menu.addItem(.separator())

        toggleItem = NSMenuItem(title: "开始听课", action: #selector(toggleRecording), keyEquivalent: "r")
        toggleItem.target = self
        menu.addItem(toggleItem)

        captionToggleItem = NSMenuItem(title: "显示悬浮字幕", action: #selector(toggleCaptions), keyEquivalent: "c")
        captionToggleItem.target = self
        menu.addItem(captionToggleItem)

        let openItem = NSMenuItem(title: "打开工作台…", action: #selector(openWorkspace), keyEquivalent: "o")
        openItem.target = self
        menu.addItem(openItem)

        menu.addItem(.separator())
        serverItem = NSMenuItem(title: "服务：检查中…", action: nil, keyEquivalent: "")
        serverItem.isEnabled = false
        menu.addItem(serverItem)

        menu.addItem(.separator())
        let quitItem = NSMenuItem(title: "退出 aula", action: #selector(quit), keyEquivalent: "q")
        quitItem.target = self
        menu.addItem(quitItem)
        menu.autoenablesItems = false
        statusItem.menu = menu

        // 服务不在就拉起
        DispatchQueue.global().async { [weak self] in
            let ok = ServeManager.ensureRunning()
            DispatchQueue.main.async {
                self?.serverItem.title = ok ? "服务：运行中（8780）" : "服务：未运行（启动失败）"
            }
        }

        // 时钟：菜单栏上的听课时长
        timer = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [weak self] _ in
            self?.tick()
        }

        // 首启自动亮一次字幕窗：菜单栏应用没有窗口，不主动出声一次会像"没打开"
        captions.toggle()
        captionToggleItem.title = "隐藏悬浮字幕"
        log("菜单栏应用已启动，字幕窗已展示")
    }

    private func tick() {
        guard AppState.shared.recording, let started = AppState.shared.startedAt else { return }
        let seconds = Int(Date().timeIntervalSince(started))
        sessionItem.title = String(format: "● 听课中 %02d:%02d", seconds / 60, seconds % 60)
    }

    @objc private func toggleRecording() {
        if AppState.shared.recording { stopRecording() } else { startRecording() }
    }

    private func startRecording() {
        guard ServeManager.ensureRunning() else {
            alert("aula 服务没有起来。先在终端跑 `aula serve`，或检查安装。")
            return
        }
        toggleItem.title = "结束听课"
        sessionItem.title = "连接中…"
        AppState.shared.startedAt = Date()

        DispatchQueue.global().async { [weak self] in
            guard let self else { return }
            // 1) 新建一节课
            guard let session = httpJSON("POST", "/api/sessions", payload: ["title": "菜单栏课堂"]),
                  let sid = session["id"] as? String else {
                self.fail("创建课堂失败"); return
            }
            AppState.shared.sessionId = sid
            // 2) 连 WS 收转写与译文
            self.connectWebSocket(sessionId: sid)
            // 3) 开始抓系统音频，按帧推给服务端
            let capture = SystemAudioCapture()
            self.capture = capture
            DispatchQueue.main.async { self.sessionItem.title = "● 听课中 00:00" }
            do {
                try awaitCapture(capture)
            } catch {
                let reason = (error as? LocalizedError)?.errorDescription ?? error.localizedDescription
                self.fail("捕获失败：" + reason)
                return
            }
        }
    }

    // 包一层，让 async throws 的 start 能在 GCD 闭包里等待
    private func awaitCapture(_ capture: SystemAudioCapture) throws {
        let semaphore = DispatchSemaphore(value: 0)
        var thrown: Error?
        Task {
            do {
                try await capture.start(onSegment: { [weak self] data in
                    self?.wsTask?.send(.data(data)) { error in
                        if let error { log("帧发送失败：" + error.localizedDescription) }
                    }
                }, onPartial: { [weak self] data in
                    self?.wsTask?.send(.data(data)) { error in
                        if let error { log("草稿发送失败：" + error.localizedDescription) }
                    }
                }, onError: { [weak self] message in
                    self?.fail(message)
                })
            } catch {
                thrown = error
            }
            semaphore.signal()
        }
        semaphore.wait()
        if let thrown { throw thrown }
    }

    private func connectWebSocket(sessionId: String) {
        guard let url = URL(string: "ws://127.0.0.1:8780/ws/live?session_id=\(sessionId)") else { return }
        let task = URLSession.shared.webSocketTask(with: url)
        wsTask = task
        task.resume()
        receiveLoop(task)
    }

    private func receiveLoop(_ task: URLSessionWebSocketTask) {
        task.receive { [weak self] result in
            guard let self else { return }
            switch result {
            case .success(.string(let text)):
                if let data = text.data(using: .utf8),
                   let message = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                    self.handleMessage(message)
                }
                if AppState.shared.recording { self.receiveLoop(task) }
            case .success:
                if AppState.shared.recording { self.receiveLoop(task) }
            case .failure(let error):
                log("WS 断开：" + error.localizedDescription)
            }
        }
    }

    private func handleMessage(_ message: [String: Any]) {
        switch message["type"] as? String {
        case "partial":
            if let source = message["source"] as? String {
                let target = message["target"] as? String ?? ""
                DispatchQueue.main.async {
                    self.captions.updateDraft(source: source, target: target)
                }
            }
        case "transcript":
            if let utterance = message["utterance"] as? [String: Any],
               let source = utterance["source"] as? String {
                let rough = utterance["target"] as? String ?? ""
                DispatchQueue.main.async {
                    self.captions.update(source: source, target: rough)
                }
            }
        case "translation":
            if let target = message["target"] as? String {
                DispatchQueue.main.async {
                    // 回填最后一句的译文：字幕窗按原文去重，这里直接更新末行
                    self.captions.updateLastTarget(target)
                }
            }
        default:
            break
        }
    }

    private func fail(_ message: String) {
        log(message)
        DispatchQueue.main.async {
            self.alert(message)
            if AppState.shared.recording { self.stopRecording() }
        }
    }

    private func stopRecording() {
        AppState.shared.recording = false
        capture?.stop()
        capture = nil
        wsTask?.cancel(with: .goingAway, reason: nil)
        wsTask = nil
        toggleItem.title = "开始听课"
        sessionItem.title = "待机"
        captions.clear()
    }

    @objc private func toggleCaptions() {
        captions.toggle()
        captionToggleItem.title = captions.visible ? "隐藏悬浮字幕" : "显示悬浮字幕"
    }

    @objc private func openWorkspace() {
        NSWorkspace.shared.open(URL(string: kBaseURL)!)
    }

    @objc private func quit() {
        if AppState.shared.recording { stopRecording() }
        NSApp.terminate(nil)
    }

    private func alert(_ message: String) {
        let alert = NSAlert()
        alert.messageText = "aula"
        alert.informativeText = message
        alert.runModal()
    }
}

private extension String {
    func or(_ fallback: String) -> String { isEmpty ? fallback : self }
}

// MARK: - 入口

@main
final class AulaMenubarApp {
    static func main() {
        let app = NSApplication.shared
        let delegate = AppDelegate()
        app.delegate = delegate
        app.run()
    }
}
