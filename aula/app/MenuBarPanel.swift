import SwiftUI
import AppKit
import Combine

/// 菜单栏弹出面板。对应 AirTranslate 的 `MenuBarPanelController` + `MenuBarStatusView`。
///
/// 为什么值得单独做：下拉菜单只能放命令，放不了"现在在录什么、刚才那句是什么"。
/// 面板能让人不打开主窗口就确认状态——上课前扫一眼就知道服务通不通。
///
/// 交互：左键点图标弹面板（`.transient`，点外面自动收），右键仍是原菜单。
final class MenuBarPanel: NSObject, NSPopoverDelegate {

    private let popover = NSPopover()
    private var cancellables: Set<AnyCancellable> = []
    private weak var store: ClassStore?

    var onOpenClass: () -> Void = {}
    var onOpenLibrary: () -> Void = {}
    var onOpenSettings: () -> Void = {}
    var onToggleCaptions: () -> Void = {}

    override init() {
        super.init()
        popover.behavior = .transient
        popover.animates = true
        popover.delegate = self
    }

    func bind(to store: ClassStore) {
        self.store = store
        popover.contentViewController = NSHostingController(rootView: MenuBarPanelView(
            store: store,
            onOpenClass: { [weak self] in self?.perform { $0.onOpenClass() } },
            onOpenLibrary: { [weak self] in self?.perform { $0.onOpenLibrary() } },
            onOpenSettings: { [weak self] in self?.perform { $0.onOpenSettings() } },
            onToggleCaptions: { [weak self] in self?.perform { $0.onToggleCaptions() } },
            onClose: { [weak self] in self?.close() }
        ))
        popover.contentSize = NSSize(width: 320, height: 300)
    }

    private func perform(_ action: (MenuBarPanel) -> Void) {
        close()
        action(self)
    }

    func toggle(relativeTo button: NSStatusBarButton) {
        if popover.isShown {
            close()
        } else {
            popover.show(relativeTo: button.bounds, of: button, preferredEdge: .minY)
            popover.contentViewController?.view.window?.makeKey()
        }
    }

    func close() {
        if popover.isShown { popover.performClose(nil) }
    }

    var isShown: Bool { popover.isShown }
}

// MARK: - 面板内容

struct MenuBarPanelView: View {

    @ObservedObject var store: ClassStore
    var onOpenClass: () -> Void
    var onOpenLibrary: () -> Void
    var onOpenSettings: () -> Void
    var onToggleCaptions: () -> Void
    var onClose: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            header
            Divider().opacity(0.2)
            body_
            Divider().opacity(0.2)
            actions
        }
        .frame(width: 320)
        .background(Color(nsColor: .windowBackgroundColor))
    }

    // MARK: 头部

    private var header: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 7) {
                Circle().fill(statusColor).frame(width: 7, height: 7)
                Text(statusLabel).font(.system(size: 12.5, weight: .semibold))
                Spacer()
                if store.phase == .listening {
                    Text(store.elapsedLabel)
                        .font(.system(size: 11.5, weight: .medium).monospacedDigit())
                        .foregroundStyle(.secondary)
                }
            }
            Text(store.languagePair)
                .font(.system(size: 11))
                .foregroundStyle(.secondary)

            Button {
                if store.canStop { store.stop() } else if store.canStart { store.start() }
            } label: {
                HStack(spacing: 6) {
                    Image(systemName: store.canStop ? "stop.fill" : "play.fill")
                        .font(.system(size: 10, weight: .bold))
                    Text(store.canStop ? "停止听课" : "开始听课")
                        .font(.system(size: 12.5, weight: .semibold))
                    Spacer()
                    Text(store.canStop || store.canStart ? "⌥⌘R" : "")
                        .font(.system(size: 10).monospaced())
                        .opacity(0.7)
                }
                .padding(.horizontal, 12)
                .padding(.vertical, 8)
                .foregroundStyle(.white)
                .background(RoundedRectangle(cornerRadius: 8)
                    .fill(store.canStart || store.canStop ? Theme.teal : Theme.teal.opacity(0.4)))
            }
            .buttonStyle(.plain)
            .disabled(!store.canStart && !store.canStop)
        }
        .padding(14)
    }

    // MARK: 最近内容

    @ViewBuilder
    private var body_: some View {
        VStack(alignment: .leading, spacing: 8) {
            if let last = store.lines.last {
                Text("最近一句").font(.system(size: 10)).foregroundStyle(.tertiary)
                Text(last.target.isEmpty ? last.source : last.target)
                    .font(.system(size: 12.5))
                    .lineLimit(2)
                    .fixedSize(horizontal: false, vertical: true)
                Text(last.source)
                    .font(.system(size: 10.5))
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
            } else if let draft = store.draft {
                Text("正在识别").font(.system(size: 10)).foregroundStyle(.tertiary)
                Text(draft.source)
                    .font(.system(size: 12).italic())
                    .lineLimit(2)
                    .foregroundStyle(.secondary)
            } else {
                Text(store.serviceReady
                     ? "还没有内容。选好声音来源后按 ⌥⌘R 开始。"
                     : store.serviceStatus)
                    .font(.system(size: 11.5))
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if !store.lines.isEmpty {
                Text("\(store.lines.count) 句 · \(providerLabel)")
                    .font(.system(size: 10).monospacedDigit())
                    .foregroundStyle(.tertiary)
            }
        }
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    // MARK: 快捷入口

    private var actions: some View {
        HStack(spacing: 6) {
            shortcut("上课界面", symbol: "rectangle.on.rectangle", action: onOpenClass)
            shortcut("转录库", symbol: "tray.full", action: onOpenLibrary)
            shortcut("浮窗", symbol: "captions.bubble", action: onToggleCaptions)
            shortcut("设置", symbol: "gearshape", action: onOpenSettings)
        }
        .padding(10)
    }

    private func shortcut(_ title: String, symbol: String, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            VStack(spacing: 4) {
                Image(systemName: symbol).font(.system(size: 14))
                Text(title).font(.system(size: 9.5))
            }
            .frame(maxWidth: .infinity)
            .padding(.vertical, 7)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .foregroundStyle(.secondary)
    }

    // MARK: 状态

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
        case .starting, .stopping, .failed: return .orange
        case .idle: return store.serviceReady ? Color.secondary : Color.orange
        }
    }

    private var providerLabel: String {
        if !store.asrLabel.isEmpty { return store.asrLabel }
        if !store.healthAsr.isEmpty { return store.healthAsr }
        return "未连接"
    }
}
