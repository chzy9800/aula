import SwiftUI
import AppKit

/// 上课界面。布局与交互对照 AirTranslate 的主窗口重做过：
///
///   ┌──────────────────────────────────────────────┐
///   │ ● 已停止    ⌘ 西班牙语 to 中文        ⌘ 转写  │  ← 工具条
///   ├──────────────────────────────────────────────┤
///   │              ▮▮▮▮▮▮▮▮▮▮                     │
///   │            已准备好开始聆听                   │  ← 空态
///   │          西班牙语 to 中文 · whisper           │
///   │              (▶ 开始聆听)                     │
///   │                                              │
///   │  ┌────────────────────────────────────────┐  │
///   │  │ ▶开始 │ Mac音频 麦克风 │ ⋯更多控制  00:12│  │  ← 悬浮控制条
///   │  └────────────────────────────────────────┘  │
///   └──────────────────────────────────────────────┘
///
/// 三处关键差异（相对旧版）：底部控制条改成**浮在内容上**的胶囊、空态居中且有
/// 音频可视化、主操作统一成青色胶囊按钮。工具条取代了原来的大标题 header——
/// 上课时视线在字幕上，标题占地方。
struct ClassView: View {

    @ObservedObject var store: ClassStore
    /// 必须单独观察：嵌套在 store 里的 ObservableObject 变更不会自动传播到视图
    @ObservedObject var style: CaptionStyle
    @ObservedObject var settings: AppSettings

    /// 菜单动作由外层注入：ClassView 不该知道窗口怎么开
    var onOpenSettings: () -> Void = {}
    var onToggleCaptions: () -> Void = {}
    var onDiagnose: () -> Void = {}

    var body: some View {
        VStack(spacing: 0) {
            toolbar
            Divider().opacity(0.22)
            ZStack(alignment: .bottom) {
                transcript
                controlBar
                    .padding(.horizontal, 22)
                    .padding(.bottom, 18)
            }
        }
        .frame(minWidth: 760, minHeight: 540)
        .background(background)
        .sheet(isPresented: $store.showDigestSheet) {
            DigestSheet(store: store)
        }
    }

    // MARK: - 工具条

    private var toolbar: some View {
        HStack(spacing: 14) {
            HStack(spacing: 6) {
                Circle()
                    .fill(statusColor)
                    .frame(width: 7, height: 7)
                Text(statusLabel)
                    .font(.system(size: 12, weight: .medium))
            }
            .padding(.horizontal, 11)
            .padding(.vertical, 5)
            .background(Capsule().fill(Color.primary.opacity(0.055)))
            .overlay(Capsule().strokeBorder(Color.primary.opacity(0.07)))

            HStack(spacing: 5) {
                Image(systemName: "character.book.closed")
                    .font(.system(size: 11))
                    .foregroundStyle(.secondary)
                Text(store.languagePair)
                    .font(.system(size: 12, weight: .medium))
            }

            Spacer(minLength: 8)

            if store.phase == .listening {
                Text(store.elapsedLabel)
                    .font(.system(size: 12, weight: .medium).monospacedDigit())
                    .foregroundStyle(.secondary)
            }

            HStack(spacing: 5) {
                Image(systemName: "waveform")
                    .font(.system(size: 10))
                Text(providerLabel)
                    .font(.system(size: 11.5, weight: .medium))
            }
            .foregroundStyle(Theme.teal)
            .padding(.horizontal, 11)
            .padding(.vertical, 5)
            .background(Capsule().fill(Theme.teal.opacity(0.12)))
        }
        .padding(.horizontal, 18)
        .padding(.vertical, 10)
    }

    /// 优先用 WebSocket 的 asrLabel；还没连上时用 health 里的值，别显示"未连接"
    private var providerLabel: String {
        if !store.asrLabel.isEmpty { return store.asrLabel }
        if !store.healthAsr.isEmpty { return store.healthAsr }
        return store.serviceReady ? "未连接" : "启动中"
    }

    private var statusLabel: String {
        switch store.phase {
        case .listening: return "正在聆听"
        case .starting: return "启动中"
        case .stopping: return "收尾中"
        case .failed: return "出错了"
        case .idle: return store.serviceReady ? "已停止" : "服务启动中"
        }
    }

    private var statusColor: Color {
        switch store.phase {
        case .listening: return .red
        case .starting, .stopping: return .orange
        case .failed: return .orange
        case .idle: return store.serviceReady ? Color.secondary : Color.orange
        }
    }

    // MARK: - 悬浮控制条

    private var controlBar: some View {
        HStack(spacing: 10) {
            Button {
                if store.canStop { store.stop() } else if store.canStart { store.start() }
            } label: {
                HStack(spacing: 6) {
                    Image(systemName: store.canStop ? "stop.fill" : "play.fill")
                        .font(.system(size: 10.5, weight: .bold))
                    Text(store.canStop ? "停止" : "开始")
                        .font(.system(size: 12.5, weight: .semibold))
                }
                .padding(.horizontal, 17)
                .padding(.vertical, 8)
                .foregroundStyle(.white)
                .background(Capsule().fill(store.canStart || store.canStop
                                           ? Theme.teal : Theme.teal.opacity(0.4)))
            }
            .buttonStyle(.plain)
            .disabled(!store.canStart && !store.canStop)
            .keyboardShortcut("r", modifiers: [.command, .option])

            sourceSegment

            Menu {
                Button("生成听课记录") {
                    store.showDigestSheet = true
                    store.makeDigest()
                }
                .disabled(!store.canMakeDigest)
                Button("字幕浮窗") { onToggleCaptions() }
                Button("音频诊断…") { onDiagnose() }
                Divider()
                Button("设置…") { onOpenSettings() }
            } label: {
                HStack(spacing: 5) {
                    Image(systemName: "ellipsis").font(.system(size: 11))
                    Text("更多控制").font(.system(size: 12))
                }
                .padding(.horizontal, 11)
                .padding(.vertical, 7)
            }
            .menuStyle(.borderlessButton)
            .menuIndicator(.hidden)
            .fixedSize()

            Spacer(minLength: 6)

            Text("\(store.lines.count) 句")
                .font(.system(size: 11.5).monospacedDigit())
                .foregroundStyle(.secondary)
                .padding(.trailing, 6)
        }
        .padding(7)
        .background(
            Capsule()
                .fill(.regularMaterial)
                .shadow(color: .black.opacity(0.09), radius: 12, y: 4)
        )
        .overlay(Capsule().strokeBorder(Color.white.opacity(0.16)))
    }

    /// 音源分段：选中项用不透明底色凸起，未选中透明——和 AirTranslate 底栏一致
    private var sourceSegment: some View {
        HStack(spacing: 1) {
            ForEach([ClassStore.SourceKind.systemAudio, .microphone]) { kind in
                Button {
                    if store.phase == .idle { store.sourceKind = kind }
                } label: {
                    Text(kind.shortLabel)
                        .font(.system(size: 12, weight: store.sourceKind == kind ? .semibold : .regular))
                        .foregroundStyle(store.sourceKind == kind ? Color.primary : Color.secondary)
                        .padding(.horizontal, 11)
                        .padding(.vertical, 6)
                        .background(
                            Capsule().fill(store.sourceKind == kind
                                           ? Theme.segmentSelected : Color.clear)
                        )
                }
                .buttonStyle(.plain)
                .disabled(store.phase != .idle)
            }
        }
        .padding(3)
        .background(Capsule().fill(Color.primary.opacity(0.06)))
        .help(store.sourceKind.hint)
    }

    // MARK: - 逐句列表

    private var transcript: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 16) {
                    if store.lines.isEmpty && store.draft == nil {
                        emptyState
                    }
                    ForEach(store.lines) { line in
                        row(line)
                            .id(line.id)
                    }
                    if let draft = store.draft {
                        draftRow(draft)
                            .id(-1)
                    }
                }
                .padding(.horizontal, 22)
                .padding(.top, 22)
                .padding(.bottom, 96)      // 给悬浮控制条留出空间
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            .onChange(of: store.lines.count) { _ in
                scrollToBottom(proxy)
            }
            .onChange(of: store.draft?.source) { _ in
                scrollToBottom(proxy)
            }
            .mask(alignment: .center) {
                if style.fadeMask {
                    VStack(spacing: 0) {
                        LinearGradient(colors: [.clear, .black], startPoint: .top, endPoint: .bottom)
                            .frame(height: 22)
                        Rectangle().fill(.black)
                        LinearGradient(colors: [.black, .clear], startPoint: .top, endPoint: .bottom)
                            .frame(height: 22)
                    }
                } else {
                    Rectangle().fill(.black)
                }
            }
        }
    }

    private func scrollToBottom(_ proxy: ScrollViewProxy) {
        withAnimation(.easeOut(duration: 0.18)) {
            if let last = store.lines.last {
                proxy.scrollTo(last.id, anchor: .bottom)
            } else {
                proxy.scrollTo(-1, anchor: .bottom)
            }
        }
    }

    private func row(_ line: ClassStore.Line) -> some View {
        VStack(alignment: style.alignment.horizontal, spacing: 5) {
            HStack(spacing: 7) {
                Text(stamp(line.at))
                    .font(.system(size: 9.5).monospacedDigit())
                    .foregroundStyle(.tertiary)
                if line.latencyMs > 0 {
                    Text("\(line.latencyMs)ms")
                        .font(.system(size: 9.5).monospacedDigit())
                        .foregroundStyle(.quaternary)
                }
                if line.refined {
                    Circle()
                        .fill(Color.green.opacity(0.55))
                        .frame(width: 4, height: 4)
                        .help("这句已由大模型精译")
                }
            }
            if settings.outputMode != .translationOnly {
            Text(line.source)
                .font(.system(size: style.sourceFontSize))
                .foregroundStyle(style.sourceColor)
                .multilineTextAlignment(style.alignment.swiftUI)
                .textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
            }
            if settings.outputMode != .sourceOnly {
            HStack(alignment: .firstTextBaseline, spacing: 2) {
                Text(line.target.isEmpty && line.streaming ? "…" : line.target)
                    .font(.system(size: style.fontSize))
                    .foregroundStyle(style.textColor)
                    .multilineTextAlignment(style.alignment.swiftUI)
                    .outlinedIfNeeded(style.outline)
                    .textSelection(.enabled)
                    .fixedSize(horizontal: false, vertical: true)
                if line.streaming {
                    // 光标表示"这句还没写完"——流式到达时比一个空格更能说明状态
                    Text("▌")
                        .font(.system(size: style.fontSize - 1))
                        .foregroundStyle(style.textColor.opacity(0.5))
                }
            }
            }
        }
        .frame(maxWidth: .infinity, alignment: style.alignment.frameAlignment)
    }

    /// 草稿行：浅色斜体，表示"还在说，别当真"
    private func draftRow(_ draft: ClassStore.Line) -> some View {
        VStack(alignment: style.alignment.horizontal, spacing: 5) {
            if settings.outputMode != .translationOnly {
                Text(draft.source)
                    .font(.system(size: style.sourceFontSize))
                    .italic()
                    .foregroundStyle(style.sourceColor.opacity(0.75))
                    .multilineTextAlignment(style.alignment.swiftUI)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if settings.outputMode != .sourceOnly, !draft.target.isEmpty {
                Text(draft.target)
                    .font(.system(size: style.fontSize))
                    .italic()
                    .foregroundStyle(style.textColor.opacity(0.8))
                    .multilineTextAlignment(style.alignment.swiftUI)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .frame(maxWidth: .infinity, alignment: style.alignment.frameAlignment)
        .opacity(0.85)
        .animation(.easeInOut(duration: 0.9).repeatForever(autoreverses: true), value: draft.source)
    }

    // MARK: - 空态

    private var emptyState: some View {
        VStack(spacing: 15) {
            AudioBars()

            Text("已准备好开始聆听")
                .font(.system(size: 28, weight: .semibold))

            Text(store.languagePair + (providerLabel.isEmpty ? "" : " · \(providerLabel)"))
                .font(.system(size: 12.5))
                .foregroundStyle(.secondary)

            Button {
                store.start()
            } label: {
                HStack(spacing: 7) {
                    Image(systemName: "play.fill").font(.system(size: 11, weight: .bold))
                    Text("开始聆听").font(.system(size: 13.5, weight: .semibold))
                }
                .padding(.horizontal, 24)
                .padding(.vertical, 11)
                .foregroundStyle(.white)
                .background(Capsule().fill(store.canStart ? Theme.teal : Theme.teal.opacity(0.4)))
            }
            .buttonStyle(.plain)
            .disabled(!store.canStart)
            .padding(.top, 2)

            Text(store.sourceKind.hint)
                .font(.system(size: 11))
                .foregroundStyle(.tertiary)
                .multilineTextAlignment(.center)
                .frame(maxWidth: 440)

            if let hint = serviceHint {
                Text(hint)
                    .font(.system(size: 11))
                    .foregroundStyle(.orange)
                    .multilineTextAlignment(.center)
                    .frame(maxWidth: 440)
            }
        }
        .frame(maxWidth: .infinity)
        .frame(minHeight: 420)
        .padding(.bottom, 40)
    }

    private var serviceHint: String? {
        if store.phase == .failed { return store.lastError.isEmpty ? nil : store.lastError }
        if !store.canStart && store.lines.isEmpty && !store.serviceReady { return "服务正在启动，稍等一下…" }
        return nil
    }

    private var background: some View {
        ZStack {
            Rectangle().fill(.ultraThinMaterial)
        }
    }

    private func stamp(_ date: Date) -> String {
        let formatter = DateFormatter()
        formatter.dateFormat = "HH:mm:ss"
        return formatter.string(from: date)
    }
}

// MARK: - 主题与装饰

enum Theme {
    /// 主色取自 AirTranslate 的主按钮（青绿），整站只用这一个强调色。
    /// 深色模式要提亮一档——原色在深背景上会糊成一团，对比度不够。
    static let teal = Color(nsColor: NSColor(name: nil) { appearance in
        let isDark = appearance.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua
        return isDark
            ? NSColor(srgbRed: 0.42, green: 0.76, blue: 0.78, alpha: 1)
            : NSColor(srgbRed: 0.29, green: 0.60, blue: 0.62, alpha: 1)
    })

    /// 分段控件选中项的底色。浅色下是白色凸起（比背景亮），
    /// 深色下 `controlBackgroundColor` 会和背景糊在一起，改用亮白半透明。
    static let segmentSelected = Color(nsColor: NSColor(name: nil) { appearance in
        appearance.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua
            ? NSColor(white: 1.0, alpha: 0.18)
            : NSColor.white
    })
}

/// 空态的音频可视化：一排会呼吸的竖条。
/// 用 TimelineView 驱动，不需要 @State（本机没有 SwiftUI 宏插件）。
private struct AudioBars: View {
    var body: some View {
        TimelineView(.animation(minimumInterval: 1.0 / 12.0)) { context in
            let t = context.date.timeIntervalSinceReferenceDate
            HStack(alignment: .center, spacing: 3) {
                ForEach(0..<14, id: \.self) { index in
                    let phase = t * 2.1 + Double(index) * 0.55
                    let height = 6 + abs(sin(phase)) * 22
                    Capsule()
                        .fill(Theme.teal.opacity(0.5))
                        .frame(width: 3, height: height)
                }
            }
            .frame(height: 30)
        }
    }
}

/// 听课记录结果窗：生成中给进度，成功后能复制或落盘。
/// 服务端产出是带 YAML frontmatter 的 Markdown，直接扔进 Obsidian 就能用。
struct DigestSheet: View {

    @ObservedObject var store: ClassStore

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 10) {
                Text("听课记录")
                    .font(.system(size: 14, weight: .semibold))
                if store.digestBusy {
                    ProgressView().controlSize(.small)
                }
                Spacer()
                if store.digest != nil {
                    Button("复制") { copyToPasteboard() }
                    Button("保存…") { saveToFile() }
                }
                Button("关闭") { store.showDigestSheet = false }
                    .keyboardShortcut(.cancelAction)
            }
            .padding(.horizontal, 16)
            .padding(.vertical, 12)

            Divider().opacity(0.4)

            content
        }
        .frame(width: 740, height: 560)
    }

    @ViewBuilder
    private var content: some View {
        if let digest = store.digest {
            ScrollView {
                Text(digest)
                    .font(.system(size: 11.5, design: .monospaced))
                    .textSelection(.enabled)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(16)
            }
        } else if let error = store.digestError {
            VStack(alignment: .leading, spacing: 8) {
                Text(error)
                    .font(.system(size: 12))
                    .foregroundStyle(.red)
                Text("这节课的转写仍然在库里，不会丢。修好网关后重新点一次「生成听课记录」即可。")
                    .font(.system(size: 11))
                    .foregroundStyle(.secondary)
            }
            .padding(20)
            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        } else {
            VStack(spacing: 10) {
                ProgressView()
                Text("正在分块抽取再全局合成，通常十几秒…")
                    .font(.system(size: 11))
                    .foregroundStyle(.secondary)
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
    }

    private func copyToPasteboard() {
        guard let digest = store.digest else { return }
        let pasteboard = NSPasteboard.general
        pasteboard.clearContents()
        pasteboard.setString(digest, forType: .string)
    }

    private func saveToFile() {
        guard let digest = store.digest else { return }
        let panel = NSSavePanel()
        panel.allowedContentTypes = [.plainText]
        panel.nameFieldStringValue = "听课记录.md"
        panel.canCreateDirectories = true
        if panel.runModal() == .OK, let url = panel.url {
            try? digest.write(to: url, atomically: true, encoding: .utf8)
        }
    }
}


/// 文字描边。SwiftUI 没有真正的描边 API，用四向零半径阴影近似——
/// 在亮背景（幻灯片、白底 PDF）上，这比换字色更可靠。
private struct OutlineModifier: ViewModifier {
    let enabled: Bool

    func body(content: Content) -> some View {
        if enabled {
            content
                .shadow(color: .black.opacity(0.85), radius: 0, x: 1.2, y: 0)
                .shadow(color: .black.opacity(0.85), radius: 0, x: -1.2, y: 0)
                .shadow(color: .black.opacity(0.85), radius: 0, x: 0, y: 1.2)
                .shadow(color: .black.opacity(0.85), radius: 0, x: 0, y: -1.2)
        } else {
            content
        }
    }
}

extension View {
    func outlinedIfNeeded(_ enabled: Bool) -> some View {
        modifier(OutlineModifier(enabled: enabled))
    }
}
