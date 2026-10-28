import Foundation
import Speech
import AVFoundation

/// Apple 语音识别（macOS 26+ 的 `SpeechAnalyzer` + `SpeechTranscriber`）。
///
/// 这是 AirTranslate 用的同一套 API，也是"边说边出字"的真正来源：
/// 框架自己分段、自己出 volatile 结果，不需要我们做 VAD 切句。
///
/// 踩过的两个关键坑（都表现为"静默无输出"，不报错）：
///  1. 音频格式必须是 `SpeechAnalyzer.bestAvailableAudioFormat(compatibleWith:)`
///     返回的那个（实测是 16kHz / 1ch / **Float32 / interleaved = true**）。
///     给 Int16 或非交错格式，`continuation.yield` 会直接阻塞。
///  2. 投喂完不能立刻 `finalizeAndFinishThroughEndOfInput`，框架还在处理缓冲，
///     要给它一点时间，否则尾部内容丢失。
///
/// 识别质量说明：实测同一段 TTS 音频，它不如 whisper beam=5 准确
/// （"producida por generadores" 被听成 "con sin apoyo, yduras"）。
/// 换来的是首字 0.1s 的延迟。所以做成可选引擎，不替换 whisper。
@available(macOS 26.0, *)
final class AppleSpeechV2 {

    enum Availability {
        case ready
        case unsupported
        case unauthorized
    }

    private var transcriber: SpeechTranscriber?
    private var analyzer: SpeechAnalyzer?
    private var continuation: AsyncStream<AnalyzerInput>.Continuation?
    private var consumer: Task<Void, Never>?
    private var targetFormat: AVAudioFormat?
    private var converter: AVAudioConverter?
    private var sourceFormat: AVAudioFormat?
    private var lastPartial = ""

    var onPartial: ((String) -> Void)?
    var onFinal: ((String) -> Void)?
    var onError: ((String) -> Void)?

    static func authorizationStatus() -> SFSpeechRecognizerAuthorizationStatus {
        SFSpeechRecognizer.authorizationStatus()
    }

    /// 只判授权：语言是否支持交给框架在 start 时报错
    /// （`supportedLocales` 是 async，没法在同步方法里查）
    static func availability(locale: String) -> Availability {
        SFSpeechRecognizer.authorizationStatus() == .authorized ? .ready : .unauthorized
    }

    // MARK: - 生命周期

    func start(locale: String) -> String? {
        guard #available(macOS 26.0, *) else { return "需要 macOS 26 或更新系统" }
        if SFSpeechRecognizer.authorizationStatus() == .notDetermined {
            _ = AppleSpeech.requestAuthorization()
        }
        guard SFSpeechRecognizer.authorizationStatus() == .authorized else {
            let raw = SFSpeechRecognizer.authorizationStatus().rawValue
            return "语音识别未授权（状态 \(raw)）：设置 → 权限 → 请求授权"
        }

        lastPartial = ""
        let identifier = locale.replacingOccurrences(of: "-", with: "_")
        let language = Locale(identifier: identifier)

        let transcriber = SpeechTranscriber(
            locale: language,
            transcriptionOptions: [],
            reportingOptions: [.volatileResults],
            attributeOptions: []
        )
        self.transcriber = transcriber

        let analyzer = SpeechAnalyzer(modules: [transcriber])
        self.analyzer = analyzer

        // 起结果消费
        consumer = Task { [weak self] in
            guard let transcriber = self?.transcriber else { return }
            do {
                for try await result in transcriber.results {
                    let text = String(result.text.characters)
                    guard !text.isEmpty else { continue }
                    await MainActor.run {
                        if result.isFinal {
                            self?.lastPartial = ""
                            self?.onFinal?(text)
                        } else if text != self?.lastPartial {
                            self?.lastPartial = text
                            self?.onPartial?(text)
                        }
                    }
                }
            } catch {
                await MainActor.run {
                    self?.onError?("识别流中断：\(error.localizedDescription)")
                }
            }
        }

        // 异步启动：targetFormat 要 await 拿，start(inputSequence:) 也是 async
        Task { [weak self] in
            guard let self, let analyzer = self.analyzer else { return }
            let format = await SpeechAnalyzer.bestAvailableAudioFormat(compatibleWith: [transcriber])
                ?? AVAudioFormat(commonFormat: .pcmFormatFloat32,
                                 sampleRate: 16000, channels: 1, interleaved: true)!
            await MainActor.run { self.targetFormat = format }

            let (stream, continuation) = AsyncStream<AnalyzerInput>.makeStream(bufferingPolicy: .unbounded)
            await MainActor.run { self.continuation = continuation }
            do {
                try await analyzer.start(inputSequence: stream)
            } catch {
                await MainActor.run { self.onError?("启动识别器失败：\(error.localizedDescription)") }
            }
        }
        return nil
    }

    /// 喂 16kHz 单声道 Int16 PCM。
    func feed(int16 samples: [Int16]) {
        guard !samples.isEmpty,
              let target = targetFormat,
              let continuation else { return }

        let source = AVAudioFormat(commonFormat: .pcmFormatFloat32,
                                   sampleRate: 16000, channels: 1, interleaved: true)!
        if converter == nil || sourceFormat != source {
            converter = AVAudioConverter(from: source, to: target)
            sourceFormat = source
        }
        guard let buffer = AVAudioPCMBuffer(pcmFormat: source,
                                            frameCapacity: AVAudioFrameCount(samples.count)),
              let channel = buffer.floatChannelData?[0] else { return }
        buffer.frameLength = AVAudioFrameCount(samples.count)
        let scale: Float = 1.0 / 32768.0
        for index in 0..<samples.count {
            channel[index] = Float(samples[index]) * scale
        }

        // 格式一致就直接投，不一致先转（实测两者的 commonFormat/interleaved 常常不同）
        if source == target {
            continuation.yield(AnalyzerInput(buffer: buffer))
            return
        }
        guard let converter,
              let out = AVAudioPCMBuffer(pcmFormat: target,
                                         frameCapacity: AVAudioFrameCount(samples.count) + 512) else { return }
        var error: NSError?
        var supplied = false
        converter.convert(to: out, error: &error) { _, status in
            if supplied {
                status.pointee = .noDataNow
                return nil
            }
            supplied = true
            status.pointee = .haveData
            return buffer
        }
        if error == nil, out.frameLength > 0 {
            continuation.yield(AnalyzerInput(buffer: out))
        }
    }

    func stop() {
        continuation?.finish()
        continuation = nil
        consumer?.cancel()
        consumer = nil
        transcriber = nil
        analyzer = nil
    }
}
