import Foundation
import AVFoundation

/// 全局设置（转写参数 / 会话策略 / 语音输出）。对应 AirTranslate 的
/// Session Workflow + Transcription + Voice Output 三个设置页。
///
/// 与 `CaptionStyle`（外观）分开：外观是"看起来怎样"，这里是"怎么跑"。
final class AppSettings: ObservableObject {

    /// 输出模式。对应 AirTranslate 的 Transcribe-only / 双语 / 仅译文。
    enum OutputMode: String, CaseIterable, Identifiable {
        case bilingual, sourceOnly, translationOnly
        var id: String { rawValue }
        var label: String {
            switch self {
            case .bilingual: return "原文 + 译文"
            case .sourceOnly: return "只要原文"
            case .translationOnly: return "只要译文"
            }
        }
        var note: String {
            switch self {
            case .bilingual: return "上课默认。原文小字在上，译文大字在下。"
            case .sourceOnly: return "纯转写。服务端完全不做翻译，省掉整条模型调用。"
            case .translationOnly: return "只看译文。适合已经听懂大意、只想确认中文。"
            }
        }
        /// 传给服务端的值：只有「只要原文」需要让服务端省掉翻译
        var serverValue: String { self == .sourceOnly ? "source_only" : "bilingual" }
    }

    /// 翻译优先级：quality = 只等 LLM（准但 2.3 秒）；speed = 专用 MT 先出（约 0.13 秒）
    /// 再由 LLM 精修，两者差得远才替换。这就是 AirTranslate 的体感来源。
    enum TranslationPriority: String, CaseIterable, Identifiable {
        case quality, speed
        var id: String { rawValue }
        var label: String { self == .quality ? "质量优先" : "速度优先" }
        var note: String {
            switch self {
            case .quality:
                return "只等大模型，译文一步到位，约 2.3 秒/句。不会出现二次替换。"
            case .speed:
                return "专用翻译接口先出（实测 0.13 秒/句），大模型随后精修；两者差得远才替换。"
            }
        }
    }

    /// 转写速度：控制"草稿多久出一次、草稿音频多长"。
    /// 窗口越短 ASR 越快，但上下文越少、残句越多——这就是实时感与准确度的旋钮。
    enum TranscriptionSpeed: String, CaseIterable, Identifiable {
        case fast, balanced, accurate
        var id: String { rawValue }
        var label: String {
            switch self {
            case .fast: return "快速"
            case .balanced: return "均衡"
            case .accurate: return "准确"
            }
        }
        var note: String {
            switch self {
            case .fast: return "草稿每 0.5 秒刷一次、只看最近 4 秒音频。首字最快，残句略多。"
            case .balanced: return "草稿每 0.9 秒、最近 8 秒音频。日常上课用这档。"
            case .accurate: return "草稿每 1.2 秒、最近 12 秒音频。上下文最全，首字最慢。"
            }
        }
        var partialMs: Double {
            switch self {
            case .fast: return 500
            case .balanced: return 900
            case .accurate: return 1200
            }
        }
        var partialWindowSeconds: Double {
            switch self {
            case .fast: return 4
            case .balanced: return 8
            case .accurate: return 12
            }
        }
    }

    /// 界面语言。目前覆盖设置界面与主要按钮，主界面运行时文案仍是中文。
    @Published var uiLanguage: L10n.Lang { didSet { persist() } }

    /// 识别引擎。Apple 用系统框架（框架自带流式分段，首字约 0.3s）；
    /// whisper 用本地模型（更准，但要等 VAD 切句 + 推理）。
    enum EngineKind: String, CaseIterable, Identifiable {
        case whisper, apple
        var id: String { rawValue }
        var label: String {
            switch self {
            case .whisper: return "whisper（准）"
            case .apple: return "Apple 语音（快）"
            }
        }
        var note: String {
            switch self {
            case .whisper:
                return "本地 whisper，精度最好；等静音切句后整句识别。"
            case .apple:
                return "系统语音识别，边说边出字（首字约 0.3 秒）。需要首次授权并下载西语语言包；精度略低于 whisper。"
            }
        }
    }

    /// 翻译引擎。Apple 本地（TranslationSession，语言包已装即用）/ 服务端（LLM 或 MT）
    enum TranslationEngine: String, CaseIterable, Identifiable {
        case server, apple
        var id: String { rawValue }
        var label: String {
            switch self {
            case .server: return "服务端（LLM/MT）"
            case .apple: return "Apple 本地"
            }
        }
        var note: String {
            switch self {
            case .server:
                return "走服务端翻译，术语与上下文更好；需要网络与 API 额度。"
            case .apple:
                return "系统本地翻译，无网络、无额度、延迟低。需要已安装「西班牙语 → 简体中文」翻译语言包（AirTranslate 装过就现成）。"
            }
        }
    }

    /// 译文语气（DeepL 的 formality 思路）。课堂默认口语化，讲座/答辩可切书面。
    enum ToneStyle: String, CaseIterable, Identifiable {
        case casual, formal, neutral
        var id: String { rawValue }
        var label: String {
            switch self {
            case .casual: return "口语化"
            case .formal: return "书面"
            case .neutral: return "中性"
            }
        }
        var note: String {
            switch self {
            case .casual: return "像同声传译在讲台上说的话：短句、少用「的」字堆叠。课堂默认。"
            case .formal: return "规范书面语，术语完整、句式严谨，像正式讲义。适合讲座、答辩。"
            case .neutral: return "清楚准确，不刻意口语化，也不过度正式。"
            }
        }
    }

    @Published var engineKind: EngineKind { didSet { persist() } }
    @Published var toneStyle: ToneStyle { didSet { persist() } }
    @Published var translationEngine: TranslationEngine { didSet { persist() } }
    @Published var outputMode: OutputMode { didSet { persist() } }
    @Published var transcriptionSpeed: TranscriptionSpeed { didSet { persist() } }
    @Published var translationPriority: TranslationPriority { didSet { persist() } }

    // —— 转写 ——
    /// 段落断句静音阈值（毫秒）。对应 AirTranslate 的 `Paragraph Break Silence`
    @Published var paragraphSilenceMs: Double { didSet { persist() } }
    @Published var minSpeechMs: Double { didSet { persist() } }
    @Published var maxSegmentMs: Double { didSet { persist() } }

    // —— 会话 ——
    /// true = 每次开始新建课堂；false = 续用上一次的课堂（对应 Start New Session / Keep Current Session）
    @Published var newSessionEachStart: Bool { didSet { persist() } }
    /// 会话结束是否把转录写进库（对应 Save Transcript Files）
    @Published var saveTranscripts: Bool { didSet { persist() } }

    // —— 语音输出 ——
    @Published var speechEnabled: Bool { didSet { persist() } }
    @Published var speechVoiceID: String { didSet { persist() } }
    @Published var speechVolume: Double { didSet { persist() } }
    @Published var speechRate: Double { didSet { persist() } }

    private static let prefix = "aula.settings."

    init() {
        let defaults = UserDefaults.standard
        engineKind = EngineKind(rawValue: defaults.string(forKey: Self.prefix + "engine") ?? "") ?? .whisper
        translationEngine = TranslationEngine(rawValue: defaults.string(forKey: Self.prefix + "transEngine") ?? "") ?? .server
        toneStyle = ToneStyle(rawValue: defaults.string(forKey: Self.prefix + "tone") ?? "") ?? .casual
        outputMode = OutputMode(rawValue: defaults.string(forKey: Self.prefix + "outputMode") ?? "") ?? .bilingual
        translationPriority = TranslationPriority(rawValue: defaults.string(forKey: Self.prefix + "transPriority") ?? "") ?? .quality
        transcriptionSpeed = TranscriptionSpeed(rawValue: defaults.string(forKey: Self.prefix + "asrSpeed") ?? "") ?? .balanced
        uiLanguage = L10n.Lang(rawValue: defaults.string(forKey: Self.prefix + "uiLanguage") ?? "") ?? .zh
        paragraphSilenceMs = defaults.object(forKey: Self.prefix + "silenceMs") as? Double ?? 550
        minSpeechMs = defaults.object(forKey: Self.prefix + "minSpeechMs") as? Double ?? 350
        maxSegmentMs = defaults.object(forKey: Self.prefix + "maxSegmentMs") as? Double ?? 15000
        newSessionEachStart = defaults.object(forKey: Self.prefix + "newSession") as? Bool ?? true
        saveTranscripts = defaults.object(forKey: Self.prefix + "saveTranscripts") as? Bool ?? true
        speechEnabled = defaults.object(forKey: Self.prefix + "speechEnabled") as? Bool ?? false
        speechVoiceID = defaults.string(forKey: Self.prefix + "speechVoice") ?? ""
        speechVolume = defaults.object(forKey: Self.prefix + "speechVolume") as? Double ?? 1.0
        speechRate = defaults.object(forKey: Self.prefix + "speechRate") as? Double ?? 0.5
    }

    private func persist() {
        let defaults = UserDefaults.standard
        defaults.set(engineKind.rawValue, forKey: Self.prefix + "engine")
        defaults.set(translationEngine.rawValue, forKey: Self.prefix + "transEngine")
        defaults.set(toneStyle.rawValue, forKey: Self.prefix + "tone")
        defaults.set(outputMode.rawValue, forKey: Self.prefix + "outputMode")
        defaults.set(translationPriority.rawValue, forKey: Self.prefix + "transPriority")
        defaults.set(transcriptionSpeed.rawValue, forKey: Self.prefix + "asrSpeed")
        defaults.set(uiLanguage.rawValue, forKey: Self.prefix + "uiLanguage")
        defaults.set(paragraphSilenceMs, forKey: Self.prefix + "silenceMs")
        defaults.set(minSpeechMs, forKey: Self.prefix + "minSpeechMs")
        defaults.set(maxSegmentMs, forKey: Self.prefix + "maxSegmentMs")
        defaults.set(newSessionEachStart, forKey: Self.prefix + "newSession")
        defaults.set(saveTranscripts, forKey: Self.prefix + "saveTranscripts")
        defaults.set(speechEnabled, forKey: Self.prefix + "speechEnabled")
        defaults.set(speechVoiceID, forKey: Self.prefix + "speechVoice")
        defaults.set(speechVolume, forKey: Self.prefix + "speechVolume")
        defaults.set(speechRate, forKey: Self.prefix + "speechRate")
    }
}

/// 语音输出：把定稿译文朗读出来。对应 AirTranslate 的 Voice Output。
///
/// 只在**定稿**译文上朗读，不读草稿、不读流式增量——逐字朗读会把一句话
/// 读成几十段碎片，课堂上纯属噪音。
final class SpeechOutput {

    private let synthesizer = AVSpeechSynthesizer()
    private var lastSpoken: String = ""

    var isSpeaking: Bool { synthesizer.isSpeaking }

    static func availableVoices() -> [(id: String, name: String, lang: String)] {
        AVSpeechSynthesisVoice.speechVoices()
            .map { (id: $0.identifier, name: $0.name, lang: $0.language) }
            .sorted { lhs, rhs in
                // 中文音色优先排前面，这是中文目标语言的场景
                let leftCN = lhs.lang.hasPrefix("zh")
                let rightCN = rhs.lang.hasPrefix("zh")
                if leftCN != rightCN { return leftCN }
                return lhs.lang < rhs.lang
            }
    }

    func speak(_ text: String, settings: AppSettings) {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard settings.speechEnabled, !trimmed.isEmpty else { return }
        guard trimmed != lastSpoken else { return }
        lastSpoken = trimmed

        let utterance = AVSpeechUtterance(string: trimmed)
        if !settings.speechVoiceID.isEmpty,
           let voice = AVSpeechSynthesisVoice(identifier: settings.speechVoiceID) {
            utterance.voice = voice
        } else {
            utterance.voice = AVSpeechSynthesisVoice(language: "zh-CN")
        }
        utterance.volume = Float(max(0, min(1, settings.speechVolume)))
        utterance.rate = AVSpeechUtteranceDefaultSpeechRate * Float(max(0.3, min(1.2, settings.speechRate * 1.2)))
        synthesizer.speak(utterance)
    }

    func stop() {
        if synthesizer.isSpeaking { synthesizer.stopSpeaking(at: .immediate) }
    }

    /// 新会话开始时清掉去重记录，否则同一句话在下一节课里不会再读
    func resetSession() {
        lastSpoken = ""
        stop()
    }
}
