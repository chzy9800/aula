import Foundation
import SwiftUI
import Translation
import AppKit

/// Apple 本地翻译（`TranslationSession`）。
///
/// 为什么必须这么绕：`TranslationSession` 只能从 SwiftUI 的 `.translationTask` 修饰符
/// 拿到，没有 AppKit 入口。所以用一个 1×1 的隐藏窗口承载一个空视图，靠它触发
/// `.translationTask` 把 session 递出来，再存起来给普通代码用。
///
/// 语言包：设置里显示 `Translation Language Pack: 西班牙语 to 简体中文 已安装` 才可用
/// （AirTranslate 装过，所以本机现成）。
@available(macOS 15.0, *)
final class AppleTranslator: ObservableObject {

    /// 触发 `.translationTask` 的配置；变化即重新建立会话
    @Published fileprivate var configuration: TranslationSession.Configuration?

    private var session: TranslationSession?
    private var bridgeWindow: NSWindow?
    private var pending: [(text: String, completion: (String?) -> Void)] = []
    private var sourceLang = "es"
    private var targetLang = "zh-Hans"
    /// 诊断用：最近一次耗时与累计计数
    private(set) var lastLatency: TimeInterval = 0
    private(set) var successCount = 0
    private(set) var failureCount = 0
    private(set) var lastError: String = ""

    /// 语言对是否可用（不下载、只查询）
    static func availability(source: String, target: String) async -> Bool {
        let status = await LanguageAvailability().status(
            from: Locale.Language(identifier: source),
            to: Locale.Language(identifier: target)
        )
        switch status {
        case .installed, .supported:
            return true
        default:
            return false
        }
    }

    /// 建桥：必须在主线程调用，且窗口要"在屏幕上"（哪怕是屏幕外坐标）才会触发修饰符
    func activate(source: String, target: String) {
        sourceLang = source
        targetLang = target
        if bridgeWindow == nil {
            // 不能用 alpha=0 且放到屏幕外：那种窗口系统可能判定"不可见"，
            // SwiftUI 的 .translationTask 就不会触发。放到屏幕角落 + 0.01 更稳。
            let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1, height: 1),
                                  styleMask: [.borderless],
                                  backing: .buffered, defer: false)
            window.isOpaque = false
            window.alphaValue = 0.01
            window.ignoresMouseEvents = true
            window.level = .normal
            window.contentView = NSHostingView(rootView: TranslationBridge(translator: self))
            bridgeWindow = window
        }
        bridgeWindow?.orderFrontRegardless()
        // 换语言对就重建会话
        configuration = TranslationSession.Configuration(
            source: Locale.Language(identifier: source),
            target: Locale.Language(identifier: target)
        )
    }

    func deactivate() {
        bridgeWindow?.orderOut(nil)
        session = nil
        pending.removeAll()
    }

    /// 翻译一句。会话没就绪时排队，就绪后统一放行。
    func translate(_ text: String, completion: @escaping (String?) -> Void) {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { completion(nil); return }
        guard let session else {
            pending.append((trimmed, completion))
            // 会话还没到：重新触发一次
            if configuration == nil {
                activate(source: sourceLang, target: targetLang)
            }
            return
        }
        run(trimmed, session: session, completion: completion)
    }

    private func run(_ text: String, session: TranslationSession, completion: @escaping (String?) -> Void) {
        let started = Date()
        Task {
            do {
                let response = try await session.translate(text)
                let translated = response.targetText
                let elapsed = Date().timeIntervalSince(started)
                await MainActor.run {
                    self.lastLatency = elapsed
                    self.successCount += 1
                    AppLog.write(String(format: "apple-translate: %.2fs「%@」→「%@」",
                                        elapsed,
                                        String(text.prefix(24)),
                                        String(translated.prefix(24))))
                    completion(translated.isEmpty ? nil : translated)
                }
            } catch {
                await MainActor.run {
                    self.failureCount += 1
                    self.lastError = error.localizedDescription
                    AppLog.write("apple-translate: 失败 \(error.localizedDescription)")
                    completion(nil)
                }
            }
        }
    }

    func attach(_ session: TranslationSession) {
        self.session = session
        let waiting = pending
        pending.removeAll()
        for item in waiting {
            run(item.text, session: session, completion: item.completion)
        }
    }
}

// MARK: - SwiftUI 桥

@available(macOS 15.0, *)
private struct TranslationBridge: View {
    @ObservedObject var translator: AppleTranslator

    var body: some View {
        Color.clear
            .frame(width: 1, height: 1)
            .translationTask(translator.configuration) { session in
                translator.attach(session)
            }
    }
}
