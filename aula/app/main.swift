// aula 原生应用（B 路线 P0）：菜单栏常驻 + 服务状态窗。
//
// 这一版只做地基：内嵌 Python sidecar 的生命周期。课堂界面与讲义工作区在后续迭代，
// 但 sidecar 一旦稳了，它们就只是「连上 baseURL 画 UI」。
//
// 两种运行方式：
//   aula.app                正常 GUI（双击）
//   aula --probe            命令行自检：起 sidecar → 等健康 → 建课堂 → 停 → 报耗时
//                           （无 GUI，可进 CI，也是「冷启动 ≤4s」指标的测法）
//
// 编译打包见 scripts/build_app.sh。

import Foundation
import AppKit
import SwiftUI
import AVFoundation
import Carbon.HIToolbox

// MARK: - 命令行自检

/// 只有一个目的：把「双击到能用」的时间量出来，不靠感觉。
func runProbe() -> Int32 {
    let sidecar = Sidecar()
    sidecar.onStateChange = { state, text in
        FileHandle.standardError.write("【probe】\(state.rawValue): \(text)\n".data(using: .utf8)!)
    }
    let t0 = Date()
    sidecar.start()

    // 冷启动上限 45 秒（首次要下/加载 whisper 模型），超时按失败算
    while Date().timeIntervalSince(t0) < 45 {
        if sidecar.state == .ready { break }
        if sidecar.state == .failed { break }
        Thread.sleep(forTimeInterval: 0.1)
    }

    guard sidecar.state == .ready else {
        print("PROBE FAIL: \(sidecar.statusText)")
        sidecar.stop()
        return 1
    }

    print("PROBE OK")
    print("  port: \(sidecar.port)")
    print("  cold_start_ms: \(sidecar.coldStartMs)")
    print("  base_url: \(sidecar.baseURL)")

    // 顺手验一个业务接口：能建课堂才算真的可用，不是只有 /health 通
    if let session = Sidecar.post(sidecar.baseURL + "/api/sessions", payload: ["title": "冷启动自检"]) {
        print("  session_created: \(session["id"] as? String ?? "?")")
        if let sid = session["id"] as? String {
            _ = Sidecar.get(sidecar.baseURL + "/api/sessions/\(sid)", timeout: 5)
        }
    } else {
        print("PROBE FAIL: 建课堂失败")
        sidecar.stop()
        return 1
    }

    // 停服务并确认进程真的没了：残留的 sidecar 会一直占着 8781 和 SQLite
    sidecar.stopAndWait()
    let leftover = shellOutput("/usr/bin/pgrep", ["-f", "aula serve --host 127.0.0.1 --port \(sidecar.port)"]).trimmingCharacters(in: .whitespacesAndNewlines)
    print("  leftover_pids: \(leftover.isEmpty ? "none" : leftover)")
    let ok = leftover.isEmpty
    print(ok ? "PROBE CLEAN" : "PROBE FAIL: sidecar 进程残留")
    return ok ? 0 : 1
}

func shellOutput(_ path: String, _ args: [String]) -> String {
    let proc = Process()
    proc.executableURL = URL(fileURLWithPath: path)
    proc.arguments = args
    let pipe = Pipe()
    proc.standardOutput = pipe
    proc.standardError = FileHandle.nullDevice
    try? proc.run()
    proc.waitUntilExit()
    let data = pipe.fileHandleForReading.readDataToEndOfFile()
    return String(data: data, encoding: .utf8) ?? ""
}

// MARK: - 课堂链路自检

/// 转 RunLoop 而不是 Thread.sleep：URLSession/主队列回调要在这段时间里跑完，
/// 用 sleep 会把 WS 事件全堵在队列里，自检必然超时。
func pump(_ seconds: Double) {
    let end = Date().addingTimeInterval(seconds)
    while Date() < end {
        RunLoop.current.run(mode: .default, before: Date().addingTimeInterval(0.02))
    }
}

/// 无 UI 跑通整条课堂链路：起服务 → 建课堂 → 连 WS → 发真实西语 PCM → 断言三层消息。
/// 这条命令就是「界面能上课」的前置条件，进了 CI 就不用人肉点按钮。
func runClassProbe(_ pcmDir: String, withDigest: Bool = false) -> Int32 {
    let sidecar = Sidecar()
    sidecar.start()
    var waited = 0.0
    while sidecar.state != .ready && sidecar.state != .failed && waited < 45 {
        pump(0.2)
        waited += 0.2
    }
    guard sidecar.state == .ready else {
        print("PROBE FAIL: sidecar 未就绪（\(sidecar.statusText)）")
        sidecar.stopAndWait()
        return 1
    }
    print("  sidecar: \(sidecar.coldStartMs)ms · port \(sidecar.port)")

    guard let session = Sidecar.post(sidecar.baseURL + "/api/sessions",
                                    payload: ["title": "课堂链路自检"]),
          let sid = session["id"] as? String else {
        print("PROBE FAIL: 建课堂失败")
        sidecar.stopAndWait()
        return 1
    }

    var readyCount = 0
    var partials = 0
    var transcripts = 0
    var translations = 0
    var errors: [String] = []
    var sources: [Int: String] = [:]
    var targets: [Int: String] = [:]
    var refined: Set<Int> = []

    let realtime = RealtimeClient()
    realtime.onEvent = { event in
        switch event {
        case .ready:
            readyCount += 1
        case .partial:
            partials += 1
        case .transcript(let seq, _, let source, let target, _):
            transcripts += 1
            sources[seq] = source
            if !target.isEmpty { targets[seq] = target }
        case .translation(let seq, let target):
            translations += 1
            refined.insert(seq)
            if !target.isEmpty { targets[seq] = target }
        case .error(let message):
            errors.append(message)
        default:
            break
        }
    }
    realtime.connect(baseURL: sidecar.baseURL, sessionId: sid)
    pump(2)

    guard readyCount > 0 else {
        print("PROBE FAIL: 没收到 ready")
        realtime.disconnect()
        sidecar.stopAndWait()
        return 1
    }

    var clock: UInt32 = 0
    for index in 1...3 {
        let path = (pcmDir as NSString).appendingPathComponent("s\(index).raw")
        guard let pcm = try? Data(contentsOf: URL(fileURLWithPath: path)) else {
            print("PROBE FAIL: 读不到 \(path)")
            break
        }
        // 先发草稿帧（前 65%），再发定稿帧——和真实录音的时序一致
        let cut = pcm.count * 65 / 100
        let draftMs = UInt32(cut / 32)
        realtime.send(VadSegmenter.frame(pcm: Data(pcm.prefix(cut)),
                                        startMs: clock, endMs: clock + draftMs, flags: 1))
        // 草稿要 1.2s 才可能被 partial_worker 处理完（轮询 0.12s + ASR 0.9s）
        pump(1.2)
        let fullMs = UInt32(pcm.count / 32)
        realtime.send(VadSegmenter.frame(pcm: pcm, startMs: clock, endMs: clock + fullMs, flags: 0))
        clock += fullMs
        // 段间等 4s：一句的处理链是 ASR 0.9s + 精译 2.5s，等太短下一句草稿必被定稿挤掉
        pump(4)
    }
    realtime.flush()
    pump(8)
    realtime.disconnect()

    // 验收口径（按服务端的真实语义，不是想当然）：
    //   · 每句都必须有 transcript 且 target 非空——粗译随原文一起到，空译文就是丢内容
    //   · partial 是"尽力而为"：定稿优先、草稿让路是设计意图（server.py 里明写），不能要求条数
    //   · translation 只在精译 != 粗译时才推（省一次回填闪烁），所以条数 ≤ transcript 数是正常的
    let missingTranslation = sources.keys.filter { (targets[$0] ?? "").isEmpty }
    print("--- 课堂链路 ---")
    print("  ready: \(readyCount)")
    print("  partial(草稿，尽力而为): \(partials)")
    print("  transcript(定稿): \(transcripts)")
    print("  translation(精译覆盖): \(translations)  覆盖 seq: \(refined.sorted())")
    for seq in sources.keys.sorted() {
        let src = sources[seq] ?? ""
        let tgt = targets[seq] ?? ""
        print("  [\(seq)] \(src.prefix(52))")
        print("       → \(tgt.prefix(30))")
    }
    if !errors.isEmpty { print("  errors: \(errors)") }
    if !missingTranslation.isEmpty { print("  缺译文的 seq: \(missingTranslation.sorted())") }

    // 听课记录：整条链路最后一步，也是最容易"以为通了其实没通"的一步
    var digestOK = true
    if withDigest {
        print("--- 听课记录 ---")
        var markdown: String?
        var failure: String?
        let semaphore = DispatchSemaphore(value: 0)
        DispatchQueue.global(qos: .userInitiated).async {
            let result = Sidecar.post(sidecar.baseURL + "/api/sessions/\(sid)/digest",
                                      payload: [:], timeout: 180)
            if let md = result?["markdown"] as? String, !md.isEmpty {
                markdown = md
            } else {
                failure = "接口返回空或失败"
            }
            semaphore.signal()
        }
        // 同步等：Sidecar.post 阻塞的是后台线程，主线程在这里等不会死锁
        _ = semaphore.wait(timeout: .now() + 200)
        if let md = markdown {
            print("  markdown: \(md.count) 字符")
            let preview = md.prefix(240).replacingOccurrences(of: "\n", with: "⏎")
            print("  开头: \(preview)")
        } else {
            print("  FAIL: \(failure ?? "超时")")
            digestOK = false
        }
    }

    sidecar.stopAndWait()
    let ok = transcripts >= 3 && missingTranslation.isEmpty && errors.isEmpty && digestOK
    print(ok ? "PROBE OK" : "PROBE FAIL: 消息不完整")
    return ok ? 0 : 1
}

/// 把上课界面渲染成 PNG（不需要屏幕录制权限，用视图自身缓存）。
/// 用途：改完 UI 先看图，而不是起 app 逐个点。第二个参数是要灌多少条（长列表基准）。
func runRenderPreview(_ outputPath: String, count: Int = 3, listening: Bool = true) -> Int32 {
    let app = NSApplication.shared
    app.setActivationPolicy(.accessory)

    let store = ClassStore(sidecar: Sidecar())
    let t0 = Date()
    store.loadDemoLines(count: count, listening: listening)

    let hosting = NSHostingView(rootView: ClassView(store: store))
    hosting.frame = NSRect(x: 0, y: 0, width: 780, height: 560)
    let window = NSWindow(contentRect: hosting.frame,
                          styleMask: [.titled], backing: .buffered, defer: false)
    window.contentView = hosting
    window.orderBack(nil)
    hosting.layoutSubtreeIfNeeded()
    // 计时只算注入 + 同步布局，不含这段等待（否则数字里掺了 1.2s 白等，自欺欺人）
    let layoutMs = Int(Date().timeIntervalSince(t0) * 1000)
    pump(1.2)

    guard let rep = hosting.bitmapImageRepForCachingDisplay(in: hosting.bounds) else {
        print("PREVIEW FAIL: 拿不到位图")
        return 1
    }
    let t1 = Date()
    hosting.cacheDisplay(in: hosting.bounds, to: rep)
    let drawMs = Int(Date().timeIntervalSince(t1) * 1000)
    guard let data = rep.representation(using: .png, properties: [:]) else {
        print("PREVIEW FAIL: PNG 编码失败")
        return 1
    }
    do {
        try data.write(to: URL(fileURLWithPath: outputPath))
    } catch {
        print("PREVIEW FAIL: 写文件 \(error.localizedDescription)")
        return 1
    }
    print("PREVIEW OK: \(outputPath) (\(data.count / 1024)KB)")
    print("  lines: \(count)  注入+布局: \(layoutMs)ms  绘制: \(drawMs)ms")
    return 0
}

/// 权限与运行环境自检。
/// 为什么值得单独一条命令：TCC 认的是**运行它的进程身份**——从终端跑 `--probe`，
/// 权限会记到终端头上；只有从 .app 里跑才记到 bundle id。这条命令把这件事说清楚。
func runPermissionProbe() -> Int32 {
    print("--- 运行环境 ---")
    print("  bundle_id: \(Bundle.main.bundleIdentifier ?? "无（命令行运行，TCC 会记到终端上）")")
    print("  bundle_path: \(Bundle.main.bundlePath)")

    print("--- 权限 ---")
    switch AVCaptureDevice.authorizationStatus(for: .audio) {
    case .authorized: print("  麦克风: 已授权")
    case .notDetermined: print("  麦克风: 未申请（首次点「开始听课」会弹窗）")
    case .denied: print("  麦克风: 已拒绝 → 系统设置 → 隐私与安全性 → 麦克风，勾上 aula")
    case .restricted: print("  麦克风: 被系统策略限制")
    @unknown default: print("  麦克风: 未知状态")
    }

    let screenGranted = CGPreflightScreenCaptureAccess()
    print("  屏幕录制: \(screenGranted ? "已授权" : "未授权（抓系统声音/网课音频需要它；只用麦克风不需要）")")

    print("--- 系统音频捕获 (ScreenCaptureKit) ---")
    if screenGranted {
        print("  已授权，可直接用 SCStream 抓系统声音")
    } else {
        print("  需先授权：点一次「抓系统声音」会让系统弹窗，或去 系统设置 → 隐私与安全性 → 屏幕录制")
    }
    print("  ⚠️ 从终端跑本命令得到的授权状态属于终端，不代表 aula.app 已授权")
    return 0
}

/// 系统声音捕获自检（终端可跑，用终端的屏幕录制权限验证代码通路）。
/// app 内的授权要另算——TCC 认负责进程。
func runSystemAudioProbe(seconds: Double) -> Int32 {
    // SCShareableContent 要走 WindowServer：命令行进程必须先初始化图形会话，
    // 否则断言失败 CGS_REQUIRE_INIT（did_initialize）。不需要窗口，只要 shared 实例。
    let app = NSApplication.shared
    app.setActivationPolicy(.accessory)

    print("--- 系统声音捕获 ---")
    let granted = SystemAudioSource.hasPermission()
    print("  屏幕录制: \(granted ? "已授权" : "未授权")")
    if !granted {
        print("  ⚠️ 先给运行本命令的终端勾上「屏幕录制」，再重跑")
        return 2
    }
    if seconds <= 0 {
        print("  只查权限，不捕获")
        return 0
    }

    let source = SystemAudioSource()
    let lock = NSLock()
    var totalSamples = 0
    var chunks = 0
    var peak = 0
    source.onSamples = { samples in
        lock.lock()
        totalSamples += samples.count
        chunks += 1
        peak = max(peak, samples.map { abs(Int($0)) }.max() ?? 0)
        lock.unlock()
    }
    source.onFailure = { message in
        print("  FAIL: \(message)")
    }

    var done = false
    Task {
        do {
            try await source.start()
            print("  已开始捕获 \(Int(seconds))s——让电脑播放点声音（音乐/视频/TTS 都行）…")
        } catch {
            print("  start FAIL: \(error.localizedDescription)")
            done = true
            return
        }
        try? await Task.sleep(nanoseconds: UInt64(seconds * 1_000_000_000))
        source.stop()
        done = true
    }
    while !done { pump(0.1) }

    lock.lock()
    let samples = totalSamples, chunkCount = chunks, peakValue = peak
    lock.unlock()
    print("  格式: \(source.formatDescription.isEmpty ? "未收到音频" : source.formatDescription)")
    print("  samples: \(samples)（≈\(String(format: "%.1f", Double(samples) / 16000))s）  chunks: \(chunkCount)  peak: \(peakValue)")
    let ok = samples > 0 && chunkCount > 0
    print(ok ? "PROBE OK" : "PROBE FAIL: 没抓到音频（电脑当时是否在出声？）")
    if ok && peakValue == 0 {
        print("  提示：峰值 0 = 流在但全静音。两种可能——当时没声音，或运行环境拿不到系统音频（受限沙箱会这样）")
    }
    return ok ? 0 : 1
}

/// 全局快捷键自检：注册 → 报告 → 注销。
/// 真正的按键无法自动按，但「能不能注册」是这条链路唯一的失败点
/// （键位被别的应用占用时会返回 OSStatus），验证它就有意义。
func runHotKeyProbe() -> Int32 {
    let hotKey = HotKey(keyCode: kVK_ANSI_R, modifiers: HotKey.optionCommand) {
        print("  （触发了回调）")
    }
    hotKey.onRegisterFailed = { message in
        print("  FAIL: \(message)")
    }
    let ok = hotKey.register()
    print("--- 全局快捷键 ⌥⌘R ---")
    print("  register: \(ok ? "成功" : "失败")")
    hotKey.unregister()
    print("  已注销（避免占用组合键）")
    print(ok ? "PROBE OK" : "PROBE FAIL")
    return ok ? 0 : 1
}

/// 字幕浮窗渲染自检：灌 demo 数据 → 渲染成 PNG。
func runRenderCaption(_ outputPath: String) -> Int32 {
    let app = NSApplication.shared
    app.setActivationPolicy(.accessory)

    let store = ClassStore(sidecar: Sidecar())
    store.loadDemoLines(count: 3)
    let panel = CaptionPanel()
    panel.bind(to: store)
    pump(0.4)

    guard panel.snapshot(to: outputPath) else {
        print("PREVIEW FAIL: 渲染或写文件失败")
        return 1
    }
    print("PREVIEW OK: \(outputPath)")
    print("  说明：毛玻璃材质在离屏渲染里会退化成纯色，图只验布局与文字")
    return 0
}

// MARK: - 状态窗

final class StatusWindowController: NSWindowController {

    private let dot = NSView()
    private let stateLabel = NSTextField(labelWithString: "未启动")
    private let detailLabel = NSTextField(labelWithString: "")
    private let hintLabel = NSTextField(labelWithString: "")

    convenience init() {
        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 420, height: 190),
            styleMask: [.titled, .closable, .fullSizeContentView],
            backing: .buffered,
            defer: false
        )
        window.title = "aula"
        window.titlebarAppearsTransparent = true
        window.isReleasedWhenClosed = false
        window.center()
        self.init(window: window)
        build()
    }

    private func build() {
        guard let content = window?.contentView else { return }

        dot.wantsLayer = true
        dot.layer?.cornerRadius = 5
        dot.layer?.backgroundColor = NSColor.systemGray.cgColor
        dot.translatesAutoresizingMaskIntoConstraints = false

        stateLabel.font = .systemFont(ofSize: 15, weight: .semibold)
        detailLabel.font = .monospacedDigitSystemFont(ofSize: 11, weight: .regular)
        detailLabel.textColor = .secondaryLabelColor
        detailLabel.maximumNumberOfLines = 3
        detailLabel.lineBreakMode = .byTruncatingMiddle
        hintLabel.font = .systemFont(ofSize: 11)
        hintLabel.textColor = .tertiaryLabelColor
        hintLabel.maximumNumberOfLines = 2
        hintLabel.stringValue = "课堂界面与讲义工作区在后续迭代；现在这一版只保证服务随 app 起停。"

        let stack = NSStackView(views: [stateLabel, detailLabel, hintLabel])
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 6
        stack.translatesAutoresizingMaskIntoConstraints = false

        content.addSubview(dot)
        content.addSubview(stack)

        NSLayoutConstraint.activate([
            dot.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 20),
            dot.topAnchor.constraint(equalTo: content.topAnchor, constant: 24),
            dot.widthAnchor.constraint(equalToConstant: 10),
            dot.heightAnchor.constraint(equalToConstant: 10),

            stack.leadingAnchor.constraint(equalTo: dot.trailingAnchor, constant: 10),
            stack.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -20),
            stack.topAnchor.constraint(equalTo: content.topAnchor, constant: 19),
        ])
    }

    func apply(state: Sidecar.State, text: String, port: Int, coldStartMs: Int) {
        stateLabel.stringValue = text
        switch state {
        case .ready:
            dot.layer?.backgroundColor = NSColor.systemGreen.cgColor
        case .starting:
            dot.layer?.backgroundColor = NSColor.systemOrange.cgColor
        case .stopping:
            dot.layer?.backgroundColor = NSColor.systemOrange.cgColor
        case .failed:
            dot.layer?.backgroundColor = NSColor.systemRed.cgColor
        case .stopped:
            dot.layer?.backgroundColor = NSColor.systemGray.cgColor
        }
        var lines: [String] = []
        if port > 0 { lines.append("127.0.0.1:\(port)") }
        if coldStartMs > 0 { lines.append("冷启动 \(coldStartMs)ms") }
        detailLabel.stringValue = lines.joined(separator: "　·　")
    }
}

// MARK: - App

final class AppDelegate: NSObject, NSApplicationDelegate {

    private var statusItem: NSStatusItem!
    private let sidecar = Sidecar()
    private var statusWindow: StatusWindowController?
    private var classWindow: NSWindow?
    private lazy var classStore = ClassStore(sidecar: sidecar)
    private lazy var captionPanel = CaptionPanel()
    private var listenHotKey: HotKey?
    private var logItem: NSMenuItem!
    private var captionItem: NSMenuItem!
    /// 信号源要留引用，否则被释放后 handler 不再触发
    private var signalSources: [DispatchSourceSignal] = []

    func applicationDidFinishLaunching(_ notification: Notification) {
        installSignalHandlers()
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        statusItem.button?.image = NSImage(systemSymbolName: "waveform.badge.magnifyingglass",
                                          accessibilityDescription: "aula")
        statusItem.button?.image?.isTemplate = true
        statusItem.button?.toolTip = "aula · 课堂到笔记"
        buildMenu()

        sidecar.onStateChange = { [weak self] state, text in
            DispatchQueue.main.async {
                guard let self else { return }
                self.logItem.title = "服务：" + text
                self.statusWindow?.apply(state: state, text: text,
                                         port: self.sidecar.port,
                                         coldStartMs: self.sidecar.coldStartMs)
            }
        }
        sidecar.start()

        // 悬浮字幕窗：跟着 ClassStore 走，不自己维护数据
        captionPanel.bind(to: classStore)

        // 全局快捷键 ⌥⌘R：Carbon 注册，不要辅助功能权限。
        // 注册失败必须让人知道——常驻应用静默失去快捷键等于功能缺失。
        listenHotKey = HotKey(keyCode: kVK_ANSI_R, modifiers: HotKey.optionCommand) { [weak self] in
            self?.toggleListening()
        }
        listenHotKey?.onRegisterFailed = { [weak self] message in
            DispatchQueue.main.async {
                self?.logItem.title = "服务：快捷键不可用（\(message)）"
            }
        }
        listenHotKey?.register()

        // 常驻类应用必须给首启反馈——菜单栏图标可能被折叠进「≫」，静默启动会像没打开。
        // 首启直接开上课界面：这是用户双击后真正想看的东西，服务状态窗退到菜单里。
        showClass(nil)
    }

    /// 全局热键与界面按钮共用一套判断：能停就停，能开就开，都不能就把界面亮出来说明原因。
    private func toggleListening() {
        if classStore.canStop {
            classStore.stop()
        } else if classStore.canStart {
            classStore.start()
        } else {
            showClass(nil)
        }
    }

    func applicationWillTerminate(_ notification: Notification) {
        // 全局热键要注销，否则同一个组合在下次启动前一直被占
        listenHotKey?.unregister()
        // 不能省也不能异步：不留 sidecar 孤儿进程占着端口和 SQLite
        sidecar.stopAndWait()
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        false
    }

    /// SIGTERM/SIGINT 走正常退出流程，这样 applicationWillTerminate 会被调用、sidecar 被清理。
    /// 不装的话 `pkill aula` 会直接杀进程，留下孤儿 sidecar 占着端口和 SQLite。
    private func installSignalHandlers() {
        for sig in [SIGTERM, SIGINT] {
            signal(sig, SIG_IGN)
            let source = DispatchSource.makeSignalSource(signal: sig, queue: .main)
            source.setEventHandler {
                FileHandle.standardError.write("收到退出信号，正在停止服务…\n".data(using: .utf8)!)
                NSApp.terminate(nil)
            }
            source.resume()
            signalSources.append(source)
        }
    }

    private func buildMenu() {
        let menu = NSMenu()

        logItem = NSMenuItem(title: "服务：启动中…", action: nil, keyEquivalent: "")
        logItem.isEnabled = false
        menu.addItem(logItem)
        menu.addItem(.separator())

        menu.addItem(withTitle: "上课界面", action: #selector(showClass(_:)), keyEquivalent: "1")
            .target = self
        captionItem = menu.addItem(withTitle: "显示字幕浮窗", action: #selector(toggleCaptions(_:)), keyEquivalent: "2")
        captionItem.target = self
        menu.addItem(withTitle: "显示服务状态", action: #selector(showStatus(_:)), keyEquivalent: "3")
            .target = self
        menu.addItem(.separator())
        menu.addItem(withTitle: "音频诊断…", action: #selector(runAudioDiagnosis(_:)), keyEquivalent: "")
            .target = self
        menu.addItem(withTitle: "打开应用日志", action: #selector(openAppLog(_:)), keyEquivalent: "")
            .target = self
        menu.addItem(withTitle: "打开服务日志", action: #selector(openLog(_:)), keyEquivalent: "")
            .target = self
        menu.addItem(.separator())
        menu.addItem(withTitle: "重启服务", action: #selector(restartSidecar(_:)), keyEquivalent: "")
            .target = self
        menu.addItem(.separator())
        menu.addItem(withTitle: "退出 aula", action: #selector(quit(_:)), keyEquivalent: "q")
            .target = self

        statusItem.menu = menu
    }

    @objc private func showClass(_ sender: Any?) {
        if classWindow == nil {
            let window = NSWindow(
                contentRect: NSRect(x: 0, y: 0, width: 780, height: 560),
                styleMask: [.titled, .closable, .miniaturizable, .resizable],
                backing: .buffered,
                defer: false
            )
            window.title = "aula · 上课"
            window.isReleasedWhenClosed = false
            window.contentView = NSHostingView(rootView: ClassView(store: classStore))
            window.center()
            classWindow = window
        }
        classWindow?.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    @objc private func toggleCaptions(_ sender: Any?) {
        captionPanel.toggle()
        captionItem.title = captionPanel.visible ? "隐藏字幕浮窗" : "显示字幕浮窗"
    }

    /// 音频诊断：先讲清要做什么，再跑 6 秒捕获，最后按结果给出可执行的下一步。
    /// 用户报「系统声音识别不出」时，这一个入口能把三种原因分开。
    @objc private func runAudioDiagnosis(_ sender: Any?) {
        let intro = NSAlert()
        intro.messageText = "音频诊断"
        intro.informativeText = """
        接下来 6 秒会捕获系统声音。
        请让电脑播放点声音（网课、视频、音乐都行），然后点「开始」。
        """
        intro.addButton(withTitle: "开始")
        intro.addButton(withTitle: "取消")
        guard intro.runModal() == .alertFirstButtonReturn else { return }

        classStore.diagnoseSystemAudio(seconds: 6) { [weak self] report in
            let result = NSAlert()
            result.messageText = "诊断结果"
            result.informativeText = report
            result.addButton(withTitle: "好")
            result.addButton(withTitle: "打开应用日志")
            if result.runModal() == .alertSecondButtonReturn {
                self?.openAppLog(nil)
            }
        }
    }

    @objc private func openAppLog(_ sender: Any?) {
        NSWorkspace.shared.open(AppLog.fileURL)
    }

    @objc private func showStatus(_ sender: Any?) {
        if statusWindow == nil { statusWindow = StatusWindowController() }
        statusWindow?.apply(state: sidecar.state, text: sidecar.statusText,
                            port: sidecar.port, coldStartMs: sidecar.coldStartMs)
        statusWindow?.showWindow(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    @objc private func restartSidecar(_ sender: Any?) {
        sidecar.restart()
    }

    @objc private func openLog(_ sender: Any?) {
        let url = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Logs/aula/sidecar.log")
        NSWorkspace.shared.open(url)
    }

    @objc private func quit(_ sender: Any?) {
        NSApp.terminate(nil)
    }
}

// MARK: - 入口

@main
final class AulaApp {
    static func main() {
        let args = CommandLine.arguments
        if args.contains("--probe") {
            exit(runProbe())
        }
        if let i = args.firstIndex(of: "--probe-class") {
            let dir = args.count > i + 1 ? args[i + 1] : "."
            exit(runClassProbe(dir, withDigest: args.contains("digest")))
        }
        if let i = args.firstIndex(of: "--render-preview") {
            let path = args.count > i + 1 ? args[i + 1] : "/tmp/aula-preview.png"
            let count = args.count > i + 2 ? (Int(args[i + 2]) ?? 3) : 3
            let listening = !(args.count > i + 3 && args[i + 3] == "done")
            exit(runRenderPreview(path, count: count, listening: listening))
        }
        if args.contains("--probe-permissions") {
            exit(runPermissionProbe())
        }
        if args.contains("--probe-hotkey") {
            exit(runHotKeyProbe())
        }
        if let i = args.firstIndex(of: "--render-caption") {
            let path = args.count > i + 1 ? args[i + 1] : "/tmp/aula-caption.png"
            exit(runRenderCaption(path))
        }
        if let i = args.firstIndex(of: "--probe-system-audio") {
            let seconds = args.count > i + 1 ? (Double(args[i + 1]) ?? 6) : 6
            exit(runSystemAudioProbe(seconds: seconds))
        }
        let app = NSApplication.shared
        let delegate = AppDelegate()
        app.delegate = delegate
        app.setActivationPolicy(.accessory)  // 无 Dock 图标，常驻菜单栏
        app.run()
    }
}
