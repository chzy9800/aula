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
        /// 译文还在流式到达——界面据此在末尾画一个光标，让人知道"还没写完"
        var streaming: Bool = false

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
        /// 控制条上的短名，对齐 AirTranslate 的「Mac 音频 / 麦克风」
        var shortLabel: String {
            switch self {
            case .microphone: return "麦克风"
            case .systemAudio: return "Mac 音频"
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
    /// 服务侧状态：与听课状态分开，服务还在起的时候界面要能自己变，
    /// 不能等用户碰一下控件才重绘。
    @Published private(set) var serviceStatus = "服务启动中…"
    @Published private(set) var serviceReady = false
    /// 真实语言对（来自 /api/health），空态副标题用它而不是硬编码
    @Published private(set) var languagePair = "西班牙语 to 中文"
    /// 服务端配置的转写 provider，就绪后立刻可显示（不必等 WebSocket）
    @Published private(set) var healthAsr = ""
    @Published private(set) var lines: [Line] = []
    @Published private(set) var draft: Line?
    @Published private(set) var statusText = "未开始"
    @Published private(set) var sessionTitle = ""
    @Published private(set) var asrLabel = ""
    @Published private(set) var lastError: String = ""
    @Published private(set) var elapsed: TimeInterval = 0
    @Published private(set) var digest: String?
    @Published private(set) var digestBusy = false
    @Published private(set) var digestError: String?
    /// 界面上的 sheet 开关。
    /// 放这里而不是视图里的 `@State`：只有 Command Line Tools 时没有 SwiftUIMacros 插件，
    /// `@State`/`@StateObject` 会直接编译失败（`plugin for module 'SwiftUIMacros' not found`）；
    /// `@Published` 是 propertyWrapper，不需要宏。
    @Published var showDigestSheet = false

    /// 有转写、没在录音、没在生成 → 才能出听课记录
    var canMakeDigest: Bool {
        sessionId != nil && !lines.isEmpty && !digestBusy && phase != .listening && phase != .starting
    }

    private let sidecar: Sidecar
    let settings: AppSettings
    let style: CaptionStyle
    let speech: SpeechOutput
    private let realtime = RealtimeClient()
    /// 段落静音阈值可调，所以 VAD 要能按当前设置重建
    private var vad: VadSegmenter
    private let engine = AVAudioEngine()
    private let systemAudio = SystemAudioSource()
    private var converter: AVAudioConverter?
    private var micTapInstalled = false
    private var micChunks = 0
    private var micPeak = 0
    private var sessionId: String?
    private var startedAt: Date?
    private var timer: Timer?
    private var serviceCancellables: Set<AnyCancellable> = []
    private let draftKey = -1
    /// 诊断计数（VAD 触发次数）——只在诊断里读
    private(set) var segmentCount = 0
    private(set) var partialCount = 0

    var baseURL: String { sidecar.baseURL }
    var canStart: Bool { sidecar.state == .ready && (phase == .idle || phase == .failed) }
    var canStop: Bool { phase == .listening || phase == .starting }

    init(sidecar: Sidecar,
         settings: AppSettings = AppSettings(),
         style: CaptionStyle = CaptionStyle(),
         speech: SpeechOutput = SpeechOutput()) {
        self.sidecar = sidecar
        self.settings = settings
        self.style = style
        self.speech = speech
        self.vad = VadSegmenter(silenceMs: settings.paragraphSilenceMs,
                                minSpeechMs: settings.minSpeechMs,
                                maxSegmentMs: settings.maxSegmentMs,
                                partialMs: settings.transcriptionSpeed.partialMs,
                                partialWindowSeconds: settings.transcriptionSpeed.partialWindowSeconds)
        wireRealtime()
        wireVad()
        wireService()
    }

    /// 订阅服务状态。不做这件事的症状：app 刚启动时界面停在"未开始"，
    /// 服务其实已经就绪，但界面要等下一次交互才刷新。
    private func wireService() {
        sidecar.$state
            .receive(on: RunLoop.main)
            .sink { [weak self] state in
                guard let self else { return }
                self.serviceReady = (state == .ready)
                switch state {
                case .ready:
                    self.serviceStatus = "服务就绪 · 选好声音来源后按 ⌥⌘R 开始"
                    self.refreshHealth()
                case .starting:
                    self.serviceStatus = "服务启动中…"
                case .stopping:
                    self.serviceStatus = "服务停止中…"
                case .failed:
                    let reason = self.sidecar.lastError
                    self.serviceStatus = reason.isEmpty ? "服务异常" : "服务异常：\(reason)"
                case .stopped:
                    self.serviceStatus = "服务未启动"
                }
            }
            .store(in: &serviceCancellables)
    }

    // MARK: - 开始 / 停止

    func start() {
        // 每次开始都按当前设置重建：用户可能刚改过段落静音阈值
        vad = VadSegmenter(silenceMs: settings.paragraphSilenceMs,
                           minSpeechMs: settings.minSpeechMs,
                           maxSegmentMs: settings.maxSegmentMs,
                           partialMs: settings.transcriptionSpeed.partialMs,
                           partialWindowSeconds: settings.transcriptionSpeed.partialWindowSeconds)
        wireVad()
        speech.resetSession()
        guard sidecar.state == .ready else {
            setPhase(.failed, "服务还没起来（\(sidecar.statusText)）")
            return
        }
        setPhase(.starting, "正在申请麦克风…")
        AVCaptureDevice.requestAccess(for: .audio) { [weak self] granted in
            guard let self else { return }
            guard granted else {
                self.setPhase(.failed, "麦克风未授权：系统设置 → 隐私与安全性 → 麦克风，勾上 aula 后重启 app")
                return
            }
            self.createSession()
        }
    }

    private func createSession() {
        // 续用上次课堂（对应 Start New Session / Keep Current Session）
        if !settings.newSessionEachStart, let last = Self.lastSessionId,
           let detail = Sidecar.getAny(baseURL + "/api/sessions/\(last)") as? [String: Any],
           let sid = detail["id"] as? String {
            sessionId = sid
            let stored = (detail["title"] as? String) ?? ""
            sessionTitle = stored.isEmpty ? "继续上次课堂" : stored
            // 把已有句子填回来，界面不是空的
            if let items = detail["utterances"] as? [[String: Any]] {
                lines = items.compactMap { item -> Line? in
                    guard let seq = item["seq"] as? Int,
                          let source = item["source"] as? String else { return nil }
                    return Line(id: seq, source: source,
                                target: (item["target"] as? String) ?? "",
                                refined: false,
                                latencyMs: (item["latency_ms"] as? Int) ?? 0,
                                at: Date())
                }
            }
            realtime.connect(baseURL: baseURL, sessionId: sid)
            return
        }

        let title = Self.defaultTitle()
        guard let session = Sidecar.post(baseURL + "/api/sessions", payload: ["title": title]),
              let sid = session["id"] as? String else {
            setPhase(.failed, "建课堂失败：服务端没有响应。看「打开服务日志」确认网关是否可用")
            return
        }
        sessionId = sid
        Self.lastSessionId = sid
        sessionTitle = title
        lines = []
        realtime.connect(baseURL: baseURL, sessionId: sid)
        // 等 ready 再开麦：服务端没准备好就发音频会被丢
    }

    /// 从转录库载入某节课继续看/继续说
    func resume(sessionID: String, title: String) {
        sessionId = sessionID
        Self.lastSessionId = sessionID
        sessionTitle = title
        if let detail = Sidecar.getAny(baseURL + "/api/sessions/\(sessionID)") as? [String: Any],
           let items = detail["utterances"] as? [[String: Any]] {
            lines = items.compactMap { item -> Line? in
                guard let seq = item["seq"] as? Int,
                      let source = item["source"] as? String else { return nil }
                return Line(id: seq, source: source,
                            target: (item["target"] as? String) ?? "",
                            refined: false,
                            latencyMs: (item["latency_ms"] as? Int) ?? 0,
                            at: Date())
            }
        }
        setPhase(.idle, lines.isEmpty ? "已载入课堂（暂无句子）" : "已载入课堂 · \(lines.count) 句")
    }

    /// 上次课堂 id（用于「保留当前会话」模式）
    static var lastSessionId: String? {
        get { UserDefaults.standard.string(forKey: "aula.lastSessionId") }
        set { UserDefaults.standard.set(newValue, forKey: "aula.lastSessionId") }
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
            setPhase(.failed, "拿不到麦克风格式：系统里可能没有可用的输入设备。插一个麦克风，或在设置 → 声音 里检查输入源")
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
            setPhase(.failed, "启动音频引擎失败：\(error.localizedDescription)。先试「系统声音」这一档，或检查麦克风是否被别的应用独占")
            return
        }
        startedAt = Date()
        startTimer()
        setPhase(.listening, "正在听课（麦克风）· \(sessionTitle)")
    }

    /// 系统声音：ScreenCaptureKit 进程内抓混音。首次会弹屏幕录制授权。
    private func beginSystemAudio() {
        DispatchQueue.main.async { self.setPhase(.starting, "正在申请屏幕录制权限…") }
        // 必须 @MainActor：startTimer 用 Timer.scheduledTimer，挂在没有 RunLoop 的
        // 后台线程上永远不会触发——上一版就是这里导致"计时不动"。
        Task { @MainActor [weak self] in
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
        // 与系统声音路径同口径的统计：mic 全静音也是常见故障（静音键、选错输入设备）
        micChunks += 1
        micPeak = max(micPeak, chunk.reduce(0) { max($0, abs(Int($1))) })
        if micChunks % 40 == 0 {
            AppLog.write("mic: chunks=\(micChunks) peak=\(micPeak) srcRate=\(Int(buffer.format.sampleRate))")
        }
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
            self?.segmentCount += 1
            self?.realtime.send(frame)
        }
        vad.onPartial = { [weak self] frame in
            self?.partialCount += 1
            self?.realtime.send(frame)
        }
        // 系统声音与麦克风走同一个 VAD，切句口径一致
        systemAudio.onSamples = { [weak self] samples in
            self?.vad.feed(samples: samples)
        }
        systemAudio.onFailure = { [weak self] message in
            guard let self, self.phase == .listening || self.phase == .starting else { return }
            AppLog.write("sysaudio: 捕获中断 \(message)")
            self.setPhase(.failed, message)
            self.teardownCapture()
        }
    }

    /// 拉一次健康信息：语言对与 provider 在开课前就该显示出来。
    /// 同步网络调用，必须离开主线程，否则界面会卡在启动那一秒。
    private func refreshHealth() {
        let url = baseURL + "/api/health"
        DispatchQueue.global(qos: .utility).async { [weak self] in
            guard let health = Sidecar.getAny(url) as? [String: Any] else { return }
            var pair: String?
            var asrLabel: String?
            if let asr = health["asr"] as? [String: Any] {
                let provider = (asr["provider"] as? String) ?? ""
                let model = (asr["model"] as? String) ?? ""
                if !provider.isEmpty {
                    asrLabel = model.isEmpty ? provider : "\(provider)/\(model)"
                }
            }
            if let translate = health["translate"] as? [String: Any] {
                let src = Self.languageName(translate["source_lang"] as? String)
                let dst = Self.languageName(translate["target_lang"] as? String)
                pair = "\(src) to \(dst)"
            }
            DispatchQueue.main.async {
                guard let self else { return }
                if let pair { self.languagePair = pair }
                if let asrLabel { self.healthAsr = asrLabel }
            }
        }
    }

    static func languageName(_ code: String?) -> String {
        switch (code ?? "").lowercased() {
        case "es": return "西班牙语"
        case "zh": return "中文"
        case "en": return "英语"
        case "fr": return "法语"
        case "de": return "德语"
        case "it": return "意大利语"
        case "pt": return "葡萄牙语"
        case "ja": return "日语"
        case "ko": return "韩语"
        case "ru": return "俄语"
        default: return (code ?? "").isEmpty ? "未知语言" : (code ?? "").uppercased()
        }
    }

    // MARK: - 音频诊断

    /// 诊断「系统声音识别不出」：分清是没有数据、数据全静音、还是 VAD 没触发。
    /// 三种情况修法完全不同，不能靠猜。
    /// 用独立的 VAD 实例统计，避免污染正式链路的切句状态。
    func diagnoseSystemAudio(seconds: Double = 6, completion: @escaping (String) -> Void) {
        guard phase == .idle || phase == .failed else {
            completion("正在听课／收尾中，先停止再诊断。")
            return
        }
        let permission = SystemAudioSource.hasPermission()
        AppLog.write("诊断: 开始 \(Int(seconds))s，屏幕录制权限=\(permission ? "有" : "无")")

        let box = StatsBox()
        let probeVad = VadSegmenter()
        probeVad.onSegment = { _ in box.bump(segments: 1) }
        probeVad.onPartial = { _ in box.bump(partials: 1) }
        systemAudio.onSamples = { samples in probeVad.feed(samples: samples) }

        Task {
            do {
                try await systemAudio.start()
            } catch {
                wireVad()
                await MainActor.run { completion("启动失败：\(error.localizedDescription)") }
                return
            }
            try? await Task.sleep(nanoseconds: UInt64(max(1, seconds) * 1_000_000_000))
            systemAudio.stop()
            wireVad()   // 恢复正式接线

            let samples = systemAudio.sampleCount
            let peak = systemAudio.peak
            let chunks = systemAudio.chunkCount
            let stats = box.snapshot()
            var lines: [String] = []
            lines.append("屏幕录制权限：\(permission ? "有" : "无")")
            lines.append("音频格式：\(systemAudio.formatDescription.isEmpty ? "未收到音频" : systemAudio.formatDescription)")
            lines.append(String(format: "样本：%d（≈%.1f 秒）  块数：%d", samples, Double(samples) / 16000, chunks))
            lines.append("峰值：\(peak)")
            lines.append("VAD 触发：\(stats.segments) 段 / \(stats.partials) 次草稿")
            lines.append("")
            if samples == 0 {
                lines.append("→ 一个样本都没收到：多半是屏幕录制权限没生效。")
                lines.append("  批过权限后 macOS 一般要求**退出并重新打开 app** 才真正放行音频。")
            } else if peak == 0 {
                lines.append("→ 有数据但全程静音：这就是「权限已给、音频未放行」的典型症状。")
                lines.append("  到 系统设置 → 隐私与安全性 → 屏幕录制 里把 aula 关掉再打开，然后重启 app。")
            } else if stats.segments == 0 {
                lines.append("→ 有声音但 VAD 没切出句子：可能是音量太低（峰值 \(peak)，低于触发阈值）。")
                lines.append("  把系统音量调大一些再试，或改用「麦克风」。")
            } else {
                lines.append("→ 捕获与断句都正常（\(stats.segments) 段），问题应在转写/翻译环节，看 sidecar 日志。")
            }
            let report = lines.joined(separator: "\n")
            AppLog.write("诊断完成:\n\(report)")
            await MainActor.run { completion(report) }
        }
    }

    private func wireRealtime() {
        realtime.onEvent = { [weak self] event in
            guard let self else { return }
            switch event {
            case .ready(let asr, let model, _):
                self.asrLabel = "\(asr)/\(model)"
                // 告诉服务端这节课要不要翻译（「只要原文」时整条 LLM 调用都省掉）
                self.realtime.sendText("{\"output_mode\":\"\(self.settings.outputMode.serverValue)\"}")
                self.realtime.sendText("{\"translation_priority\":\"\(self.settings.translationPriority.rawValue)\"}")
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
                                           refined: false, latencyMs: latency, at: Date(),
                                           streaming: true))
                }
                self.setStatus("转录中 · \(self.lines.count) 句 · 上句 \(latency)ms")
            case .translationDelta(let seq, let delta):
                // 逐字追加：只有"增加"，没有"替换"——这是消除字幕抖动的关键
                guard !delta.isEmpty,
                      let index = self.lines.firstIndex(where: { $0.id == seq }) else { return }
                self.lines[index].target += delta
                self.lines[index].streaming = true
            case .translation(let seq, let target):
                guard let index = self.lines.firstIndex(where: { $0.id == seq }) else { return }
                // 收尾的完整文本：与本地拼出来的一致就只关掉流式标记，
                // 不一致说明漏接了增量，用完整文本兜底（幂等）
                if !target.isEmpty && target != self.lines[index].target {
                    self.lines[index].target = target
                    self.lines[index].refined = true
                }
                self.lines[index].streaming = false
                // 只朗读定稿译文：读草稿或读增量会把一句话读成几十段碎片
                if !target.isEmpty {
                    self.speech.speak(target, settings: self.settings)
                }
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
            self.setPhase(.failed, "实时连接断开：\(reason)。已收到的句子都还在；重新按 ⌥⌘R 即可继续，服务日志里有细节")
            self.teardownCapture()
        }
    }

    // MARK: - 听课记录

    /// 把整节课压成一份带 YAML frontmatter 的 Markdown。
    /// 服务端是「分块抽取 → 全局合成」，一分钟的课也要十几秒，期间必须给可见反馈。
    func makeDigest() {
        guard let sessionId, !lines.isEmpty, !digestBusy else { return }
        digestBusy = true
        digestError = nil
        digest = nil
        // Sidecar.post 是同步阻塞的，不能占主线程
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self else { return }
            let result = Sidecar.post(self.baseURL + "/api/sessions/\(sessionId)/digest",
                                      payload: [:], timeout: 180)
            DispatchQueue.main.async {
                self.digestBusy = false
                if let markdown = result?["markdown"] as? String, !markdown.isEmpty {
                    self.digest = markdown
                } else {
                    self.digestError = "生成失败：看 sidecar 日志（多半是模型额度或网关没起）"
                }
            }
        }
    }

    // MARK: - 杂项

    /// 仅供 `--render-preview` 与自检用：灌假数据，让界面渲染出有内容的样子。
    /// 正常运行时不会调用——真实数据只能从 WS 事件流里来。
    /// `count` 给性能基准用（灌 500 条看长列表渲染）。
    func loadDemoLines(count: Int = 3, listening: Bool = true) {
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
                        refined: index % 3 == 2, latencyMs: item.2, at: Date(),
                        streaming: listening && index == count - 1)
        }
        // 已结束就没有草稿行了——真实停止时 draft 也会被清空
        draft = listening ? Line(id: -1,
                                 source: "El diferencial protege a las personas contra las corrientes de fuga",
                                 target: "差动开关保护人员免受泄漏电流的伤害",
                                 refined: false, latencyMs: 0, at: Date()) : nil
        sessionTitle = "10月23日 12:30 课堂"
        asrLabel = "whisper/small"
        if listening {
            statusText = "转录中 · \(count) 句 · 上句 886ms"
            phase = .listening
            elapsed = 754
        } else {
            // 已结束状态：footer 里要出现「生成听课记录」
            statusText = "已结束 · 共 \(count) 句"
            phase = .idle
            elapsed = 2680
        }
        sessionId = "demo-session-for-preview"
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

/// 跨线程计数（音频线程写、主线程读）
final class StatsBox: @unchecked Sendable {
    private let lock = NSLock()
    private var segments = 0
    private var partials = 0

    func bump(segments: Int = 0, partials: Int = 0) {
        lock.lock()
        self.segments += segments
        self.partials += partials
        lock.unlock()
    }

    func snapshot() -> (segments: Int, partials: Int) {
        lock.lock()
        defer { lock.unlock() }
        return (segments, partials)
    }
}
