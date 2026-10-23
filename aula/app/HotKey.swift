import Foundation
import Carbon.HIToolbox

/// 全局快捷键（Carbon `RegisterEventHotKey`）。
///
/// 为什么不用 `NSEvent.addGlobalMonitorForEvents`：那需要「辅助功能」权限，用户要去系统
/// 设置里勾选，还得重启 app。Carbon 的热键注册不碰这些权限——架构方案里就是按这条选的。
/// Carbon 在 macOS 27 上标了 deprecated，但仍是唯一免权限的路径，且行为稳定。
///
/// 用法：`let hotKey = HotKey(keyCode: kVK_ANSI_R, modifiers: cmdKey | optionKey) { ... }`
/// 注册失败（键位被别的 app 占用）要如实告诉用户，不能静默——这是常驻应用的可用性底线。
final class HotKey {

    private let keyCode: UInt32
    private let modifiers: UInt32
    private let action: () -> Void
    private var hotKeyRef: EventHotKeyRef?
    private var handlerRef: EventHandlerRef?
    private(set) var isRegistered = false
    var onRegisterFailed: ((String) -> Void)?

    init(keyCode: Int, modifiers: Int, action: @escaping () -> Void) {
        self.keyCode = UInt32(keyCode)
        self.modifiers = UInt32(modifiers)
        self.action = action
    }

    @discardableResult
    func register() -> Bool {
        guard !isRegistered else { return true }

        // 事件回调是 C 函数指针，只能靠 userData 把 self 带过去
        var eventType = EventTypeSpec(eventClass: OSType(kEventClassKeyboard),
                                      eventKind: UInt32(kEventHotKeyPressed))
        let callback: EventHandlerUPP = { _, _, userData in
            guard let userData else { return OSStatus(eventNotHandledErr) }
            Unmanaged<HotKey>.fromOpaque(userData).takeUnretainedValue().action()
            return noErr
        }
        let installStatus = InstallEventHandler(GetApplicationEventTarget(), callback, 1, &eventType,
                                                Unmanaged.passUnretained(self).toOpaque(), &handlerRef)
        guard installStatus == noErr else {
            onRegisterFailed?("安装事件处理器失败（OSStatus \(installStatus)）")
            return false
        }

        let hotKeyID = EventHotKeyID(signature: OSType(0x41554C41), id: 1)   // 'AULA'
        let registerStatus = RegisterEventHotKey(keyCode, modifiers, hotKeyID,
                                                 GetApplicationEventTarget(), 0, &hotKeyRef)
        guard registerStatus == noErr else {
            onRegisterFailed?("快捷键被占用（OSStatus \(registerStatus)）——多半是别的应用先注册了同一组合")
            if let handlerRef { RemoveEventHandler(handlerRef) }
            self.handlerRef = nil
            return false
        }
        isRegistered = true
        return true
    }

    func unregister() {
        if let hotKeyRef { UnregisterEventHotKey(hotKeyRef) }
        hotKeyRef = nil
        if let handlerRef { RemoveEventHandler(handlerRef) }
        handlerRef = nil
        isRegistered = false
    }

    deinit { unregister() }

    /// ⌥⌘ + 字母 的修饰符组合
    static var optionCommand: Int { optionKey | cmdKey }
}
