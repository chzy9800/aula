import Foundation

/// 极简本地化。
///
/// 为什么不用 `Localizable.strings` + `NSLocalizedString`：那套要求把界面文案全部
/// 抽成 key，这个项目的文案还带着大量解释性长句（"停顿多久算一句说完了"这类），
/// 抽 key 反而让代码难读。这里直接用「中文 / English」双参形式，缺哪句一眼能看出来。
///
/// 覆盖范围是**设置界面 + 主要按钮**。主界面的状态文案（"转录中 · 3 句"）仍是中文——
/// 那些是运行时拼出来的，等真正需要英文界面时再补，不为一个没人用的场景先付出复杂度。
enum L10n {

    enum Lang: String, CaseIterable, Identifiable {
        case zh, en
        var id: String { rawValue }
        var label: String { self == .zh ? "中文" : "English" }
    }

    static func t(_ zh: String, _ en: String, lang: Lang) -> String {
        lang == .zh ? zh : en
    }

    /// 便捷形式：跟随当前设置
    static func t(_ zh: String, _ en: String) -> String {
        let raw = UserDefaults.standard.string(forKey: "aula.settings.uiLanguage") ?? "zh"
        return t(zh, en, lang: Lang(rawValue: raw) ?? .zh)
    }
}
