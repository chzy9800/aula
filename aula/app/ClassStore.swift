import Foundation
import AVFoundation
import Combine

/// 课堂状态与录音。UI 只管读这里的 `@Published`，不碰音频和网络。
///
/// 线程模型：AVAudioEngine 的 tap 在实时线程，网络回调在任意线程，
/// 所有对 `@Published` 的写入都回到主线程——SwiftUI 只接受主线程变更。
final class ClassStore: ObservableObject {

    struct Line: Identifiable {
        let id: Int          // 服务端 seq，草稿行用负数占位
        var source: String
        var target: String
        var refined: Bool    // 精译已覆盖粗译
        var latencyMs: Int
        var at: Date

        /// 复用同一个 formatter：列表长起来后每行 new 一个是明显的浪费
        private static let timeFormatter: DateFormatter = {
            let formatter = DateFormatter()
            formatter.dateFormat = "HH:mm:ss"
            return formatter
        }()

        var timeLabel: String {
            Self.timeFormatter.string(from: at)
        }
    }

    enum Phase {
        case idle, starting, listening, stopping, failed
    }

    /// 声音从哪来。网课声音在电脑里播（Zoom/Teams/网页），麦克风收不到——
    /// 这是 0.7 版本就踩过的坑，原生版把这条选择做进界面。
    enum SourceKind: String, CaseIterable, Identifiable {
        case microphone, systemAudio
        var id: String { rawValue }
        var label: String {
            switch self {
            case .microphone: return "麦克风"
            case .systemAudio: return "系统声音"
            }
        }
        var hint: String {
            switch self {
            case .microphone: return "老师现场讲课，或你戴耳机听网课"
            case .systemAudio: return "网课声音从电脑播出来（需屏幕录制权限）"
            }
        }
    }

    @Published var sourceKind: SourceKind = .microphone
    @Published private(set) var phase: Phase = .idle
    @Published private(set) var lines: [Line] = []
    @Published private(set) var draft: Line?
    @Published private(set) var statusText = "未开始"
    @Published private(set) var sessionTitle = ""
    @Published private(set) var asrLabel = ""
    @Published private(set) var lastError: String = ""
    @Published private(set) var elapsed: TimeInterval = 0

    private let sidecar: Sidecar
    private let realtime = RealtimeClient()
    private let vad = VadSegmenter()
    private let engine = AVAudioEngine()
    private let systemAudio = SystemAudioSource()
    private var converter: AVAudioConverter?
    private var micTapInstalled = false
    private var sessionId: String?
    private var startedAt: Date?
    private var timer: Timer?
    private let draftKey = -1

    var baseURL: String { sidecar.baseURL }
    var canStart: Bool { sidecar.state == .ready && (phase == .idle || phase == .failed) }
    var canStop: Bool { phase == .listening || phase == .starting }

    init(sidecar: Sidecar) {
        self.sidecar = sidecar
        wireRealtime()
        wireVad()
    }

    // MARK: - 开始 / 停止

    func start() {
        guard sidecar.state == .ready else {
            setPhase(.failed, "服务还没起来（\(sidecar.statusText)）")
            return
        }
        setPhase(.starting, "正在申请麦克风…")
        AVCaptureDevice.requestAccess(for: .audio) { [weak self] granted in
            guard let self else { return }
            guard granted else {
                self.setPhase(.failed, "麦克风未授权：系统设置 → 隐私与安全性 → 麦克风，勾上 aula")
                return
            }
            self.createSession()
        }
    }

    private func createSession() {
        let title = Self.defaultTitle()
        guard let session = Sidecar.post(baseURL + "/api/sessions", payload: ["title": title]),
              let sid = session["id"] as? String else {
            setPhase(.failed, "建课堂失败，看 sidecar 日志")
            return
        }
        sessionId = sid
        sessionTitle = title
        realtime.connect(baseURL: baseURL, sessionId: sid)
        // 等 ready 再开麦：服务端没准备好就发音频会被丢
    }

    private func beginCapture() {
        switch sourceKind {
        case .microphone:
            beginMicrophone()
        case .systemAudio:
            beginSystemAudio()
        }
    }

    private func beginMicrophone() {
        let input = engine.inputNode
        let inputFormat = input.outputFormat(forBus: 0)
        guard inputFormat.sampleRate > 0 else {
            setPhase(.failed, "拿不到麦克风格式（可能没有输入设备）")
            return
        }
        let target = AVAudioFormat(commonFormat: .pcmFormatInt16,
                                   sampleRate: 16000, channels: 1, interleaved: true)!
        converter = AVAudioConverter(from: inputFormat, to: target)

        input.installTap(onBus: 0, bufferSize: 1024, format: inputFormat) { [weak self] buffer, _ in
            self?.handleTap(buffer, target: target)
        }
        micTapInstalled = true
        do {
            engine.prepare()
            try engine.start()
        } catch {
            input.removeTap(onBus: 0)
            micTapInstalled = false
            setPhase(.failed, "启动音频引擎失败：\(error.localizedDescription)")
            return
        }
        startedAt = Date()
        startTimer()
        setPhase(.listening, "正在听课（麦克风）· \(sessionTitle)")
    }

    /// 系统声音：ScreenCaptureKit 进程内抓混音。首次会弹屏幕录制授权。
    private func beginSystemAudio() {
        DispatchQueue.main.async { self.setPhase(.starting, "正在申请屏幕录制权限…") }
        Task { [weak self] in
            guard let self else { return }
            do {
                try await self.systemAudio.start()
                self.startedAt = Date()
                self.startTimer()
                self.setPhase(.listening, "正在听课（系统声音）· \(self.sessionTitle)")
            } catch {
                self.setPhase(.failed, error.localizedDescription)
            }
        }
    }

    private func handleTap(_ buffer: AVAudioPCMBuffer, target: AVAudioFormat) {
        guard let converter else { return }
        let ratio = target.sampleRate / buffer.format.sampleRate
        let capacity = AVAudioFrameCount(Double(buffer.frameLength) * ratio) + 64
        guard let output = AVAudioPCMBuffer(pcmFormat: target, frameCapacity: capacity) else { return }

        var supplied = false
        var error: NSError?
        converter.convert(to: output, error: &error) { _, status in
            if supplied {
                status.pointee = .noDataNow
                return nil
            }
            supplied = true
            status.pointee = .haveData
            return buffer
        }
        guard output.frameLength > 0, let channel = output.int16ChannelData else { return }
        let chunk = Array(UnsafeBufferPointer(start: channel[0], count: Int(output.frameLength)))
        vad.feed(samples: chunk)
    }

    func stop() {
        guard phase == .listening || phase == .starting else { return }
        setPhase(.stopping, "正在收尾…")
        teardownCapture()
        // flush 让服务端把队列排空，落库不缺最后几句
        realtime.flush()
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { [weak self] in
            guard let self else { return }
            self.realtime.disconnect()
            self.draft = nil
            self.setPhase(.idle, self.lines.isEmpty ? "没有听到内容" : "已结束 · 共 \(self.lines.count) 句")
        }
    }

    private func teardownCapture() {
        timer?.invalidate()
        timer = nil
        if engine.isRunning { engine.stop() }
        if micTapInstalled {
            engine.inputNode.removeTap(onBus: 0)
            micTapInstalled = false
        }
        converter = nil
        vad.reset()
        // stopCapture 里可能等最多 3 秒收尾，别放在主线程
        DispatchQueue.global(qos: .userInitiated).async { [systemAudio] in
            systemAudio.stop()
        }
    }

    // MARK: - 事件

    private func wireVad() {
        vad.onSegment = { [weak self] frame in
            self?.realtime.send(frame)
        }
        vad.onPartial = { [weak self] frame in
            self?.realtime.send(frame)
        }
        // 系统声音与麦克风走同一个 VAD，切句口径一致
        systemAudio.onSamples = { [weak self] samples in
            self?.vad.feed(samples: samples)
        }
        systemAudio.onFailure = { [weak self] message in
            guard let self, self.phase == .listening || self.phase == .starting else { return }
            self.setPhase(.failed, message)
            self.teardownCapture()
        }
    }

    private func wireRealtime() {
        realtime.onEvent = { [weak self] event in
            guard let self else { return }
            switch event {
            case .ready(let asr, let model, _):
                self.asrLabel = "\(asr)/\(model)"
                self.beginCapture()
            case .partial(_, let source, let target):
                guard !source.isEmpty else { return }
                // 草稿行固定一个负数 id，逐帧覆盖原位，不追加
                let line = Line(id: self.draftKey, source: source, target: target,
                                refined: false, latencyMs: 0, at: Date())
                self.draft = line
                self.setStatus("正在识别…")
            case .transcript(let seq, _, let source, let target, let latency):
                self.draft = nil
                if let index = self.lines.firstIndex(where: { $0.id == seq }) {
                    self.lines[index].source = source
                    self.lines[index].target = target
                } else {
                    self.lines.append(Line(id: seq, source: source, target: target,
                                           refined: false, latencyMs: latency, at: Date()))
                }
                self.setStatus("转录中 · \(self.lines.count) 句 · 上句 \(latency)ms")
            case .translation(let seq, let target):
                guard let index = self.lines.firstIndex(where: { $0.id == seq }) else { return }
                guard !target.isEmpty, target != self.lines[index].target else { return }
                self.lines[index].target = target
                self.lines[index].refined = true   // UI 闪一下表示这句升级过
            case .status(let message):
                self.setStatus(message)
            case .error(let message):
                self.setStatus("错误：" + message)
            case .flushed:
                break
            }
        }
        realtime.onDisconnect = { [weak self] reason in
            guard let self, self.phase == .listening else { return }
            self.setPhase(.failed, "实时连接断开：\(reason)")
            self.teardownCapture()
        }
    }

    // MARK: - 杂项

    /// 仅供 `--render-preview` 与自检用：灌假数据，让界面渲染出有内容的样子。
    /// 正常运行时不会调用——真实数据只能从 WS 事件流里来。
    /// `count` 给性能基准用（灌 500 条看长列表渲染）。
    func loadDemoLines(count: Int = 3) {
        if count == 0 {
            // 空态：用来检查音源选择器与引导文案
            lines = []
            draft = nil
            sessionTitle = ""
            statusText = "未开始"
            asrLabel = ""
            phase = .idle
            elapsed = 0
            return
        }
        let samples: [(String, String, Int)] = [
            ("La instalación eléctrica de la vivienda tiene dos tipos de corriente.",
             "住宅电气装置有两种电流。", 871),
            ("La corriente continua es producida por generadores tales como pilas y baterías.",
             "直流电由电池和蓄电池等发电机产生。", 890),
            ("Antes de manipular el cuadro eléctrico hay que consignar la instalación.",
             "在操作配电板之前，必须对装置执行安全隔离挂牌（consignación）。", 886),
        ]
        lines = (0..<max(0, count)).map { index in
            let item = samples[index % samples.count]
            return Line(id: index + 1, source: item.0, target: item.1,
                        refined: index % 3 == 2, latencyMs: item.2, at: Date())
        }
        draft = Line(id: -1,
                     source: "El diferencial protege a las personas contra las corrientes de fuga",
                     target: "差动开关保护人员免受泄漏电流的伤害",
                     refined: false, latencyMs: 0, at: Date())
        sessionTitle = "10月23日 12:30 课堂"
        asrLabel = "whisper/small"
        statusText = "转录中 · \(count) 句 · 上句 886ms"
        phase = .listening
        elapsed = 754
    }

    private func setPhase(_ new: Phase, _ text: String) {
        DispatchQueue.main.async {
            self.phase = new
            self.statusText = text
            // 失败原因单独存一份：状态文案可能被后续事件覆盖，界面上的引导语要留着
            if new == .failed { self.lastError = text }
            if new != .listening {
                self.timer?.invalidate()
                self.timer = nil
            }
        }
    }

    private func setStatus(_ text: String) {
        DispatchQueue.main.async { self.statusText = text }
    }

    private func startTimer() {
        timer?.invalidate()
        timer = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [weak self] _ in
            guard let self, let startedAt = self.startedAt else { return }
            self.elapsed = Date().timeIntervalSince(startedAt)
        }
    }

    var elapsedLabel: String {
        let total = Int(elapsed)
        return String(format: "%02d:%02d", total / 60, total % 60)
    }

    private static func defaultTitle() -> String {
        let f = DateFormatter()
        f.locale = Locale(identifier: "zh_CN")
        f.dateFormat = "M月d日 HH:mm 课堂"
        return f.string(from: Date())
    }
}
