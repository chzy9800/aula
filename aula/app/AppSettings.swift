import Foundation
import AVFoundation

/// 全局设置（转写参数 / 会话策略 / 语音输出）。对应 AirTranslate 的
/// Session Workflow + Transcription + Voice Output 三个设置页。
///
/// 与 `CaptionStyle`（外观）分开：外观是"看起来怎样"，这里是"怎么跑"。
final class AppSettings: ObservableObject {

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
        paragraphSilenceMs = defaults.object(forKey: Self.prefix + "silenceMs") as? Double ?? 700
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
