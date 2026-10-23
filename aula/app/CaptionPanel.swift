import AppKit
import Combine

/// 悬浮双语字幕窗：上课时浮在任何窗口上面，不用切回主界面就能看见老师说了什么。
///
/// 数据来自 `ClassStore`（Combine 订阅），外观来自 `CaptionStyle`，与主界面同一套设置。
/// 交互对齐 AirTranslate 的 Floating Captions：整面拖动、右下角手柄缩放、位置尺寸记忆、
/// 不抢焦点（`.nonactivatingPanel`）、跨 Space 跟随（`.canJoinAllSpaces`）。
final class CaptionPanel: NSObject, NSWindowDelegate {

    private let panel: NSPanel
    private let stack: NSStackView
    private var cancellables: Set<AnyCancellable> = []
    private weak var store: ClassStore?
    private let style: CaptionStyle

    private static let defaultSize = NSSize(width: 468, height: 156)
    private static let minSize = NSSize(width: 320, height: 96)
    private static let frameKey = "aula.floating.frame"

    init(style: CaptionStyle) {
        self.style = style
        let saved = Self.loadFrame()
        let rect = saved ?? NSRect(origin: .zero, size: Self.defaultSize)

        panel = NSPanel(contentRect: rect,
                        styleMask: [.borderless, .nonactivatingPanel, .resizable],
                        backing: .buffered, defer: false)
        panel.level = .floating
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = true
        panel.collectionBehavior = [.canJoinAllSpaces, .ignoresCycle]
        panel.isMovableByWindowBackground = true
        panel.hidesOnDeactivate = false
        panel.isReleasedWhenClosed = false
        panel.minSize = Self.minSize

        let effect = NSVisualEffectView(frame: NSRect(origin: .zero, size: rect.size))
        effect.material = .hudWindow
        effect.state = .active
        effect.blendingMode = .behindWindow
        effect.wantsLayer = true
        effect.layer?.cornerRadius = 16
        effect.layer?.masksToBounds = true
        effect.autoresizingMask = [.width, .height]

        stack = NSStackView()
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 6
        stack.translatesAutoresizingMaskIntoConstraints = false
        effect.addSubview(stack)

        // borderless 窗口拖不了边角，右下角自己画一个缩放手柄
        let handle = ResizeHandle(frame: NSRect(x: rect.width - 18, y: 0, width: 18, height: 18))
        handle.autoresizingMask = [.minXMargin, .maxYMargin]
        effect.addSubview(handle)

        panel.contentView = effect
        super.init()
        panel.delegate = self

        NSLayoutConstraint.activate([
            stack.topAnchor.constraint(equalTo: effect.topAnchor, constant: 12),
            stack.leadingAnchor.constraint(equalTo: effect.leadingAnchor, constant: 16),
            stack.trailingAnchor.constraint(lessThanOrEqualTo: effect.trailingAnchor, constant: -16),
            stack.bottomAnchor.constraint(lessThanOrEqualTo: effect.bottomAnchor, constant: -12),
        ])

        if saved == nil { reposition() }
        applyStyle()
        observeStyle()
    }

    // MARK: - 绑定

    func bind(to store: ClassStore) {
        self.store = store
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

    private func observeStyle() {
        // 任意外观项变化都重绘。objectWillChange 在写入前触发，下一轮 runloop 再读新值
        style.objectWillChange
            .receive(on: RunLoop.main)
            .sink { [weak self] _ in
                DispatchQueue.main.async {
                    self?.applyStyle()
                    self?.render()
                }
            }
            .store(in: &cancellables)
    }

    private func applyStyle() {
        panel.alphaValue = CGFloat(max(0.25, min(1, style.floatingOpacity)))
    }

    // MARK: - 显隐

    var visible: Bool { panel.isVisible }

    func show() {
        render()
        panel.orderFrontRegardless()
    }

    func hide() { panel.orderOut(nil) }

    func toggle() { visible ? hide() : show() }

    /// 菜单里的「重置窗口位置与尺寸」
    func resetFrame() {
        var frame = panel.frame
        frame.size = Self.defaultSize
        panel.setFrame(frame, display: true)
        reposition()
    }

    private func reposition() {
        guard let screen = NSScreen.main else { return }
        let visible = screen.visibleFrame
        panel.setFrameOrigin(NSPoint(x: visible.maxX - panel.frame.width - 24,
                                     y: visible.maxY - panel.frame.height - 24))
        saveFrame()
    }

    // MARK: - 位置记忆

    func windowDidMove(_ notification: Notification) { saveFrame() }
    func windowDidEndLiveResize(_ notification: Notification) { saveFrame() }

    private func saveFrame() {
        let frame = panel.frame
        UserDefaults.standard.set([frame.origin.x, frame.origin.y, frame.width, frame.height],
                                  forKey: Self.frameKey)
    }

    private static func loadFrame() -> NSRect? {
        guard let values = UserDefaults.standard.array(forKey: frameKey) as? [Double],
              values.count == 4, values[2] >= 320, values[3] >= 96 else { return nil }
        let rect = NSRect(x: values[0], y: values[1], width: values[2], height: values[3])
        // 屏幕可能变了（拔掉外接显示器）：落在屏幕外就退回默认位置
        guard let screen = NSScreen.main else { return rect }
        let allowed = screen.visibleFrame.insetBy(dx: -200, dy: -200)
        return allowed.intersects(rect) ? rect : nil
    }

    // MARK: - 渲染

    private func render() {
        stack.views.forEach { stack.removeView($0) }
        guard let store else { return }

        let keep = max(1, min(6, style.floatingKeepLines))
        let tail = Array(store.lines.suffix(keep))
        if tail.isEmpty && store.draft == nil {
            let hint = store.phase == .failed && !store.lastError.isEmpty
                ? store.lastError
                : "✓ aula 已在菜单栏运行\n⌥⌘R 开始听课，双语字幕会浮在这里。"
            stack.addArrangedSubview(label(hint, size: style.floatingSourceSize + 1, dim: true))
            return
        }

        // 最新在上：课堂上眼睛盯着底部，最新的那句应该在视线落点上
        if let draft = store.draft {
            if !draft.target.isEmpty {
                stack.addArrangedSubview(label(draft.target, size: style.floatingTextSize,
                                               dim: false, bold: true, italic: true))
            }
            stack.addArrangedSubview(label(draft.source, size: style.floatingSourceSize,
                                           dim: true, italic: true))
        }
        for line in tail.reversed() {
            if !line.target.isEmpty {
                let text = line.streaming ? line.target + "▌" : line.target
                stack.addArrangedSubview(label(text, size: style.floatingTextSize, dim: false, bold: true))
            }
            stack.addArrangedSubview(label(line.source, size: style.floatingSourceSize, dim: true))
        }
    }

    private func label(_ text: String, size: CGFloat, dim: Bool,
                       bold: Bool = false, italic: Bool = false) -> NSTextField {
        let field = NSTextField(wrappingLabelWithString: text)
        var font = NSFont.systemFont(ofSize: size, weight: bold ? .semibold : .regular)
        if italic {
            let descriptor = font.fontDescriptor.withSymbolicTraits(.italic)
            font = NSFont(descriptor: descriptor, size: size) ?? font
        }
        field.font = font
        let hex = dim ? style.sourceHex : style.textHex
        field.textColor = NSColor(hex: hex) ?? (dim ? .secondaryLabelColor : .labelColor)
        field.alignment = style.alignment.ns
        field.maximumNumberOfLines = 3
        field.lineBreakMode = .byTruncatingTail
        if style.outline && !dim {
            // AppKit 有真描边（NSShadow 模拟更接近原版观感）
            let shadow = NSShadow()
            shadow.shadowColor = NSColor.black.withAlphaComponent(0.9)
            shadow.shadowBlurRadius = 0
            shadow.shadowOffset = NSSize(width: 0.6, height: -0.6)
            field.shadow = shadow
        }
        return field
    }

    /// 导出自检用：把浮窗当前内容渲染成 PNG
    func snapshot(to path: String) -> Bool {
        render()
        guard let view = panel.contentView else { return false }
        view.layoutSubtreeIfNeeded()
        guard let rep = view.bitmapImageRepForCachingDisplay(in: view.bounds) else { return false }
        view.cacheDisplay(in: view.bounds, to: rep)
        guard let data = rep.representation(using: .png, properties: [:]) else { return false }
        return (try? data.write(to: URL(fileURLWithPath: path))) != nil
    }
}

/// 右下角缩放手柄
private final class ResizeHandle: NSView {
    override func resetCursorRects() {
        addCursorRect(bounds, cursor: .crosshair)
    }

    override func mouseDragged(with event: NSEvent) {
        guard let window else { return }
        var frame = window.frame
        // y 轴与鼠标方向相反：往下拖是变矮，所以高度减、原点跟着降
        frame.size.width = max(320, frame.width + event.deltaX)
        frame.size.height = max(96, frame.height - event.deltaY)
        frame.origin.y -= event.deltaY
        window.setFrame(frame, display: true)
    }
}
