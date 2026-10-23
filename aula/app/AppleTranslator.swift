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
            let window = NSWindow(contentRect: NSRect(x: -20000, y: -20000, width: 1, height: 1),
                                  styleMask: [.borderless],
                                  backing: .buffered, defer: false)
            window.isOpaque = false
            window.alphaValue = 0
            window.ignoresMouseEvents = true
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
        Task {
            do {
                let response = try await session.translate(text)
                let translated = response.targetText
                await MainActor.run { completion(translated.isEmpty ? nil : translated) }
            } catch {
                await MainActor.run {
                    AppLog.write("apple-translate: 失败 \(error.localizedDescription)")
                    completion(nil)
                }
            }
        }
    }

    fileprivate func attach(_ session: TranslationSession) {
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
