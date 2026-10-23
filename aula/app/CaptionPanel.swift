import AppKit
import Combine

/// 悬浮双语字幕窗：上课时浮在任何窗口上面，不用切回主界面就能看见老师说了什么。
///
/// 数据来自 `ClassStore`（Combine 订阅），不自己维护一份——单一数据源，避免字幕窗和
/// 主界面对不上。原菜单栏版的 CaptionWindow 自己维护 lines，这里改成被动渲染。
///
/// 参数从 `menubar/main.swift` 沿用：`.nonactivatingPanel`（点它不会抢焦点）、
/// `.canJoinAllSpaces`（切 Space 也跟着）、`.hudWindow` 毛玻璃、保留 3 句。
final class CaptionPanel {

    private let panel: NSPanel
    private let stack: NSStackView
    private var cancellables: Set<AnyCancellable> = []
    private weak var store: ClassStore?

    private let panelSize = NSSize(width: 468, height: 156)

    init() {
        panel = NSPanel(contentRect: NSRect(origin: .zero, size: panelSize),
                        styleMask: [.borderless, .nonactivatingPanel],
                        backing: .buffered, defer: false)
        panel.level = .floating
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = true
        panel.collectionBehavior = [.canJoinAllSpaces, .ignoresCycle]
        panel.isMovableByWindowBackground = true
        panel.hidesOnDeactivate = false
        // 不参与窗口循环，也不该出现在截图以外的地方
        panel.isReleasedWhenClosed = false

        let effect = NSVisualEffectView(frame: NSRect(origin: .zero, size: panelSize))
        effect.material = .hudWindow
        effect.state = .active
        effect.blendingMode = .behindWindow
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
    }

    // MARK: - 绑定

    func bind(to store: ClassStore) {
        self.store = store
        // 列表、草稿、阶段任一变化都重绘；字幕窗自己不做状态判断
        store.$lines
            .receive(on: RunLoop.main)
            .sink { [weak self] _ in self?.render() }
            .store(in: &cancellables)
        store.$draft
            .receive(on: RunLoop.main)
            .sink { [weak self] _ in self?.render() }
            .store(in: &cancellables)
        store.$phase
            .receive(on: RunLoop.main)
            .sink { [weak self] phase in
                guard let self else { return }
                if phase == .listening || phase == .starting {
                    self.show()
                } else if phase == .failed {
                    self.render()          // 失败也留着，让人看见原因
                }
            }
            .store(in: &cancellables)
        render()
    }

    // MARK: - 显隐

    var visible: Bool { panel.isVisible }

    func show() {
        repositionIfNeeded()
        render()
        panel.orderFrontRegardless()
    }

    func hide() {
        panel.orderOut(nil)
    }

    func toggle() {
        visible ? hide() : show()
    }

    private func reposition() {
        guard let screen = NSScreen.main else { return }
        let visible = screen.visibleFrame
        panel.setFrameOrigin(NSPoint(x: visible.maxX - panelSize.width - 24,
                                     y: visible.maxY - panelSize.height - 24))
    }

    /// 只在"没在屏幕上"时重新定位：用户拖动过就别再抢回去
    private func repositionIfNeeded() {
        guard let screen = NSScreen.main else { return }
        let allowed = screen.visibleFrame.insetBy(dx: -40, dy: -40)
        if !allowed.contains(panel.frame) { reposition() }
    }

    // MARK: - 渲染

    private func render() {
        stack.views.forEach { stack.removeView($0) }
        guard let store else { return }

        let tail = Array(store.lines.suffix(3))
        if tail.isEmpty && store.draft == nil {
            let hint = store.phase == .failed && !store.lastError.isEmpty
                ? store.lastError
                : "✓ aula 已在菜单栏运行\n⌥⌘R 开始听课，双语字幕会浮在这里。"
            stack.addArrangedSubview(label(hint, size: 12, dim: true))
            return
        }

        // 最新在上：课堂上眼睛盯着底部，最新的那句应该在视线落点上
        if let draft = store.draft {
            if !draft.target.isEmpty {
                stack.addArrangedSubview(label(draft.target, size: 15, dim: false, bold: true, italic: true))
            }
            stack.addArrangedSubview(label(draft.source, size: 11, dim: true, italic: true))
        }
        for line in tail.reversed() {
            if !line.target.isEmpty {
                let text = line.streaming ? line.target + "▌" : line.target
                stack.addArrangedSubview(label(text, size: 15, dim: false, bold: true))
            }
            stack.addArrangedSubview(label(line.source, size: 11, dim: true))
        }
    }

    /// 把字幕窗渲染成 PNG（供 `--render-caption` 自检用）。
    /// 注意：`.hudWindow` 毛玻璃在离屏渲染里会退化成纯色——图能验布局和文字，验不了材质。
    func snapshot(to path: String) -> Bool {
        render()
        guard let view = panel.contentView else { return false }
        view.layoutSubtreeIfNeeded()
        guard let rep = view.bitmapImageRepForCachingDisplay(in: view.bounds) else { return false }
        view.cacheDisplay(in: view.bounds, to: rep)
        guard let data = rep.representation(using: .png, properties: [:]) else { return false }
        return (try? data.write(to: URL(fileURLWithPath: path))) != nil
    }

    private func label(_ text: String, size: CGFloat, dim: Bool, bold: Bool = false, italic: Bool = false) -> NSTextField {
        let field = NSTextField(wrappingLabelWithString: text)
        var font = NSFont.systemFont(ofSize: size, weight: bold ? .semibold : .regular)
        if italic {
            let descriptor = font.fontDescriptor.withSymbolicTraits(.italic)
            font = NSFont(descriptor: descriptor, size: size) ?? font
        }
        field.font = font
        field.textColor = dim ? .secondaryLabelColor : .labelColor
        field.maximumNumberOfLines = 3
        field.lineBreakMode = .byTruncatingTail
        return field
    }
}
