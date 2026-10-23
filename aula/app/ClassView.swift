import SwiftUI

/// 上课界面（B 路线的第一个用户可见成果）。
///
/// 对应 Web 版的「上课工作区」：左边逐句双语转写，老师一句、译文一句。
/// 原生版把草稿行做成原位覆盖的呼吸行——说话过程中先出浅色斜体，定稿转正，
/// 精译到了再闪一下绿点，用户能看出「这句刚升级」。
struct ClassView: View {

    @ObservedObject var store: ClassStore

    var body: some View {
        VStack(spacing: 0) {
            header
            Divider().opacity(0.3)
            transcript
            Divider().opacity(0.3)
            footer
        }
        .frame(minWidth: 700, minHeight: 480)
        .background(background)
        .sheet(isPresented: $store.showDigestSheet) {
            DigestSheet(store: store)
        }
    }

    // MARK: - 顶栏

    private var header: some View {
        HStack(alignment: .center, spacing: 14) {
            VStack(alignment: .leading, spacing: 3) {
                Text(store.sessionTitle.isEmpty ? "aula · 课堂到笔记" : store.sessionTitle)
                    .font(.system(size: 16, weight: .semibold))
                    .lineLimit(1)
                Text(store.statusText)
                    .font(.system(size: 11))
                    .foregroundStyle(store.phase == .failed ? Color.red : Color.secondary)
                    .lineLimit(2)
            }
            Spacer(minLength: 12)

            if store.phase == .listening {
                HStack(spacing: 6) {
                    Circle()
                        .fill(Color.red)
                        .frame(width: 7, height: 7)
                        .opacity(0.9)
                    Text(store.elapsedLabel)
                        .font(.system(size: 14, weight: .medium).monospacedDigit())
                }
                Button("停止") { store.stop() }
                    .keyboardShortcut(".", modifiers: .command)
            } else {
                // 听课时不许切音源：半路换来源会让前后句子归到不同声源上，
                // 而且换源要重建流——与 Web 版「录音期间禁止切换课堂」同一口径。
                Picker("", selection: $store.sourceKind) {
                    ForEach(ClassStore.SourceKind.allCases) { kind in
                        Text(kind.label).tag(kind)
                    }
                }
                .pickerStyle(.segmented)
                .labelsHidden()
                .frame(width: 168)
                .help(store.sourceKind.hint)

                Button("开始听课") { store.start() }
                    .keyboardShortcut("r", modifiers: [.command, .option])
                    .disabled(!store.canStart)
                    .help(store.canStart ? store.sourceKind.hint : "服务未就绪")
            }
        }
        .padding(.horizontal, 18)
        .padding(.vertical, 12)
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
                    }
                    if let draft = store.draft {
                        draftRow(draft)
                    }
                    Color.clear.frame(height: 1).id("bottom")
                }
                .padding(.horizontal, 20)
                .padding(.vertical, 16)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            .onChange(of: store.lines.count) { _ in
                scrollToBottom(proxy)
            }
            .onChange(of: store.draft?.source) { _ in
                scrollToBottom(proxy)
            }
        }
    }

    private func scrollToBottom(_ proxy: ScrollViewProxy) {
        withAnimation(.easeOut(duration: 0.2)) {
            proxy.scrollTo("bottom", anchor: .bottom)
        }
    }

    private func row(_ line: ClassStore.Line) -> some View {
        VStack(alignment: .leading, spacing: 5) {
            HStack(spacing: 7) {
                Text(line.timeLabel)
                    .font(.system(size: 10).monospacedDigit())
                    .foregroundStyle(.tertiary)
                if line.latencyMs > 0 {
                    Text("\(line.latencyMs)ms")
                        .font(.system(size: 10))
                        .foregroundStyle(.tertiary)
                }
                if line.refined {
                    Circle().fill(Color.green.opacity(0.75)).frame(width: 4, height: 4)
                }
            }
            Text(line.source)
                .font(.system(size: 12))
                .foregroundStyle(.secondary)
                .textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
            Text(line.target.isEmpty ? "…" : line.target)
                .font(.system(size: 15))
                .textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    /// 草稿行：浅色斜体 + 呼吸。逐帧覆盖同一位置，不追加——否则会刷出一堆半句。
    private func draftRow(_ draft: ClassStore.Line) -> some View {
        VStack(alignment: .leading, spacing: 5) {
            HStack(spacing: 6) {
                Text("●").font(.system(size: 7)).foregroundStyle(.orange)
                Text("识别中").font(.system(size: 10)).foregroundStyle(.tertiary)
            }
            Text(draft.source)
                .font(.system(size: 12).italic())
                .foregroundStyle(.tertiary)
                .fixedSize(horizontal: false, vertical: true)
            Text(draft.target.isEmpty ? "…" : draft.target)
                .font(.system(size: 15).italic())
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .opacity(0.85)
        .animation(.easeInOut(duration: 0.9).repeatForever(autoreverses: true), value: draft.source)
    }

    private var emptyState: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("准备好了")
                .font(.system(size: 15, weight: .medium))
            Text("选好声音来源，点「开始听课」（⌥⌘R）。老师说的每句话会在这里逐句出现：\n先出原文，再出粗译，精译到了覆盖并闪一下绿点。")
                .font(.system(size: 12))
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            Text(store.sourceKind.hint)
                .font(.system(size: 11))
                .foregroundStyle(.tertiary)
            if let hint = serviceHint {
                Text(hint)
                    .font(.system(size: 11))
                    .foregroundStyle(.orange)
            }
        }
        .padding(.top, 28)
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private var serviceHint: String? {
        if store.phase == .failed { return store.lastError.isEmpty ? nil : store.lastError }
        if !store.canStart && store.lines.isEmpty { return "服务正在启动，稍等一下…" }
        return nil
    }

    // MARK: - 底栏

    private var footer: some View {
        HStack(spacing: 12) {
            Text(store.asrLabel.isEmpty ? "转写未连接" : "转写 \(store.asrLabel)")
                .font(.system(size: 10))
                .foregroundStyle(.tertiary)
            Spacer()
            Text("\(store.lines.count) 句")
                .font(.system(size: 10).monospacedDigit())
                .foregroundStyle(.tertiary)
            if store.canMakeDigest {
                Button("生成听课记录") {
                    store.showDigestSheet = true
                    store.makeDigest()
                }
                .controlSize(.small)
                .help("把整节课压成带 frontmatter 的 Markdown")
            }
        }
        .padding(.horizontal, 18)
        .padding(.vertical, 8)
    }

    private var background: some View {
        // 玻璃拟态：SwiftUI 的 material 已含 saturate，比手调 CSS 省事
        ZStack {
            Rectangle().fill(.ultraThinMaterial)
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
