import SwiftUI
import AppKit

/// 字幕外观。对应 AirTranslate 的 Caption 设置页。
///
/// 全部状态放在这里而不是视图里的 `@State`：本机只有 Command Line Tools，
/// SwiftUI 宏（`@State` / `@StateObject`）会编译失败（`SwiftUIMacros` 插件缺失）。
/// 顺带的好处是外观设置天然成为单一数据源，主界面和浮窗共用同一份。
final class CaptionStyle: ObservableObject {

    enum TextAlignmentOption: String, CaseIterable, Identifiable {
        case leading, center
        var id: String { rawValue }
        var label: String { self == .leading ? "左对齐" : "居中" }
        var swiftUI: TextAlignment { self == .leading ? .leading : .center }
        var horizontal: HorizontalAlignment { self == .leading ? .leading : .center }
        /// frame(alignment:) 收的是 Alignment，与 VStack 的 HorizontalAlignment 不是一回事
        var frameAlignment: Alignment { self == .leading ? .leading : .center }
        var ns: NSTextAlignment { self == .leading ? .left : .center }
    }

    struct Preset: Identifiable {
        let id: String
        let name: String
        let textHex: String
        let sourceHex: String
        let outline: Bool

        /// 色块按钮上的短名（手写，不用截断——中文按字数截会断在词中间）
        var shortName: String {
            switch id {
            case "default": return "系统"
            case "yellow": return "亮黄"
            case "dark": return "深字"
            case "mint": return "薄荷"
            case "amber": return "琥珀"
            default: return String(name.prefix(2))
            }
        }
    }

    static let presets: [Preset] = [
        Preset(id: "default", name: "跟随系统", textHex: "", sourceHex: "", outline: false),
        Preset(id: "yellow", name: "亮黄 + 深色描边", textHex: "#FFD60A", sourceHex: "#D1D1D6", outline: true),
        Preset(id: "dark", name: "深字（亮背景用）", textHex: "#1C1C1E", sourceHex: "#48484A", outline: false),
        Preset(id: "mint", name: "薄荷绿（护眼）", textHex: "#66E3A8", sourceHex: "#9BB8AC", outline: false),
        Preset(id: "amber", name: "琥珀（夜间）", textHex: "#FFB340", sourceHex: "#B08D5B", outline: false),
    ]

    // MARK: - 可调项

    @Published var fontSize: Double { didSet { persist() } }
    @Published var sourceFontSize: Double { didSet { persist() } }
    @Published var textHex: String { didSet { persist() } }
    @Published var sourceHex: String { didSet { persist() } }
    @Published var alignment: TextAlignmentOption { didSet { persist() } }
    @Published var outline: Bool { didSet { persist() } }
    @Published var fadeMask: Bool { didSet { persist() } }
    @Published var activePresetID: String { didSet { persist() } }
    /// 浮窗独立外观：字号缩放与不透明度
    @Published var floatingFontScale: Double { didSet { persist() } }
    @Published var floatingOpacity: Double { didSet { persist() } }
    @Published var floatingKeepLines: Int { didSet { persist() } }

    private static let prefix = "aula.caption."

    init() {
        let defaults = UserDefaults.standard
        fontSize = defaults.object(forKey: Self.prefix + "fontSize") as? Double ?? 15
        sourceFontSize = defaults.object(forKey: Self.prefix + "sourceFontSize") as? Double ?? 12
        // 空字符串 = 跟随系统外观（亮色模式用深字，暗色模式用浅字）
        textHex = defaults.string(forKey: Self.prefix + "textHex") ?? ""
        sourceHex = defaults.string(forKey: Self.prefix + "sourceHex") ?? ""
        alignment = TextAlignmentOption(rawValue: defaults.string(forKey: Self.prefix + "alignment") ?? "") ?? .leading
        outline = defaults.object(forKey: Self.prefix + "outline") as? Bool ?? false
        fadeMask = defaults.object(forKey: Self.prefix + "fadeMask") as? Bool ?? true
        activePresetID = defaults.string(forKey: Self.prefix + "preset") ?? "default"
        floatingFontScale = defaults.object(forKey: Self.prefix + "floatScale") as? Double ?? 1.0
        floatingOpacity = defaults.object(forKey: Self.prefix + "floatOpacity") as? Double ?? 1.0
        floatingKeepLines = defaults.object(forKey: Self.prefix + "keepLines") as? Int ?? 3
    }

    private func persist() {
        let defaults = UserDefaults.standard
        defaults.set(fontSize, forKey: Self.prefix + "fontSize")
        defaults.set(sourceFontSize, forKey: Self.prefix + "sourceFontSize")
        defaults.set(textHex, forKey: Self.prefix + "textHex")
        defaults.set(sourceHex, forKey: Self.prefix + "sourceHex")
        defaults.set(alignment.rawValue, forKey: Self.prefix + "alignment")
        defaults.set(outline, forKey: Self.prefix + "outline")
        defaults.set(fadeMask, forKey: Self.prefix + "fadeMask")
        defaults.set(activePresetID, forKey: Self.prefix + "preset")
        defaults.set(floatingFontScale, forKey: Self.prefix + "floatScale")
        defaults.set(floatingOpacity, forKey: Self.prefix + "floatOpacity")
        defaults.set(floatingKeepLines, forKey: Self.prefix + "keepLines")
    }

    // MARK: - 应用

    func apply(preset: Preset) {
        activePresetID = preset.id
        textHex = preset.textHex
        sourceHex = preset.sourceHex
        outline = preset.outline
    }

    func resetToDefault() {
        apply(preset: Self.presets[0])
        fontSize = 15
        sourceFontSize = 12
        alignment = .leading
        fadeMask = true
        floatingFontScale = 1.0
        floatingOpacity = 1.0
        floatingKeepLines = 3
    }

    /// 空值表示跟随系统：硬编码白色在亮色背景上会看不见（踩过）
    var textColor: Color { textHex.isEmpty ? .primary : Color(hex: textHex) }
    var sourceColor: Color { sourceHex.isEmpty ? Color.secondary : Color(hex: sourceHex) }

    /// 浮窗用：字号按缩放系数算，并给一个下限避免缩到看不见
    var floatingTextSize: CGFloat { CGFloat(max(11, fontSize * floatingFontScale)) }
    var floatingSourceSize: CGFloat { CGFloat(max(9, sourceFontSize * floatingFontScale)) }
}

// MARK: - 颜色

extension Color {
    init(hex: String) {
        let cleaned = hex.trimmingCharacters(in: CharacterSet(charactersIn: "#"))
        var value: UInt64 = 0
        Scanner(string: cleaned).scanHexInt64(&value)
        let r = Double((value >> 16) & 0xFF) / 255
        let g = Double((value >> 8) & 0xFF) / 255
        let b = Double(value & 0xFF) / 255
        self.init(red: r, green: g, blue: b)
    }
}

extension NSColor {
    convenience init?(hex: String) {
        let cleaned = hex.trimmingCharacters(in: CharacterSet(charactersIn: "#"))
        guard cleaned.count == 6 || cleaned.count == 8 else { return nil }
        var value: UInt64 = 0
        guard Scanner(string: cleaned).scanHexInt64(&value) else { return nil }
        if cleaned.count == 6 {
            self.init(srgbRed: CGFloat((value >> 16) & 0xFF) / 255,
                      green: CGFloat((value >> 8) & 0xFF) / 255,
                      blue: CGFloat(value & 0xFF) / 255,
                      alpha: 1)
        } else {
            self.init(srgbRed: CGFloat((value >> 24) & 0xFF) / 255,
                      green: CGFloat((value >> 16) & 0xFF) / 255,
                      blue: CGFloat((value >> 8) & 0xFF) / 255,
                      alpha: CGFloat(value & 0xFF) / 255)
        }
    }
}
