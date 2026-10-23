import Foundation
import Speech
import AVFoundation

/// Apple 原生语音识别（SFSpeechRecognizer，离线模式）。
///
/// 为什么值得接：AirTranslate 的实时感来自"框架自带流式分段"——它不需要像我们这样
/// 等 VAD 静音 550ms 再切句、再跑一次 whisper。Apple 的识别器边说边出候选词，
/// 首字通常在 300ms 内。代价是精度不如 whisper small，所以做成可选引擎而不是替换。
///
/// 语言包：`supportsOnDeviceRecognition` 为 false 表示本机没下西语包，
/// 系统会在首次使用时提示下载（或在 设置 → 通用 → 语言与地区 里触发）。
final class AppleSpeech {

    enum Availability {
        case ready           // 可离线识别
        case needsAsset      // 识别器在，但语言包没下
        case unsupported     // 该语言不支持
        case unauthorized    // 没授权
    }

    private var recognizer: SFSpeechRecognizer?
    private var request: SFSpeechAudioBufferRecognitionRequest?
    private var task: SFSpeechRecognitionTask?
    private var locale = "es-ES"
    private var lastPartial = ""

    var onPartial: ((String) -> Void)?
    var onFinal: ((String) -> Void)?
    var onError: ((String) -> Void)?

    static func authorizationStatus() -> SFSpeechRecognizerAuthorizationStatus {
        SFSpeechRecognizer.authorizationStatus()
    }

    /// 请求授权。**必须跑 RunLoop 而不是阻塞信号量**——TCC 弹窗要靠主线程事件循环
    /// 显示，阻塞主线程会让进程直接 abort（踩过：exit 134）。
    static func requestAuthorization() -> Bool {
        var granted = false
        var done = false
        SFSpeechRecognizer.requestAuthorization { status in
            granted = (status == .authorized)
            done = true
        }
        let deadline = Date().addingTimeInterval(30)
        while !done && Date() < deadline {
            RunLoop.current.run(mode: .default, before: Date().addingTimeInterval(0.1))
        }
        return granted
    }

    static func availability(locale: String) -> Availability {
        guard SFSpeechRecognizer.authorizationStatus() == .authorized else { return .unauthorized }
        guard let recognizer = SFSpeechRecognizer(locale: Locale(identifier: locale)) else {
            return .unsupported
        }
        guard recognizer.isAvailable else { return .unsupported }
        return recognizer.supportsOnDeviceRecognition ? .ready : .needsAsset
    }

    // MARK: - 实时识别

    func start(locale: String) -> String? {
        self.locale = locale
        lastPartial = ""
        guard SFSpeechRecognizer.authorizationStatus() == .authorized else {
            return "语音识别未授权：系统设置 → 隐私与安全性 → 语音识别，勾上 aula"
        }
        guard let recognizer = SFSpeechRecognizer(locale: Locale(identifier: locale)) else {
            return "本机不支持 \(locale) 的语音识别"
        }
        guard recognizer.isAvailable else {
            return "识别器暂不可用（系统正在准备语言资源），稍等几秒再试"
        }
        self.recognizer = recognizer

        let request = SFSpeechAudioBufferRecognitionRequest()
        request.shouldReportPartialResults = true
        // 离线优先：课堂内容不出机器，也是 AirTranslate 的做法
        request.requiresOnDeviceRecognition = recognizer.supportsOnDeviceRecognition
        if #available(macOS 13.0, *) {
            request.addsPunctuation = true
        }
        self.request = request

        task = recognizer.recognitionTask(with: request) { [weak self] result, error in
            guard let self else { return }
            if let error {
                // 取消是正常停止，不当错误报
                let nsError = error as NSError
                if nsError.code != 216 && nsError.code != 301 {
                    self.onError?("识别中断：\(error.localizedDescription)")
                }
                return
            }
            guard let result else { return }
            let text = result.bestTranscription.formattedString
            if result.isFinal {
                if !text.isEmpty { self.onFinal?(text) }
                self.lastPartial = ""
            } else if text != self.lastPartial, !text.isEmpty {
                self.lastPartial = text
                self.onPartial?(text)
            }
        }
        return nil
    }

    /// 喂 16kHz 单声道 Int16 PCM。内部转成 AVAudioPCMBuffer 交给识别器。
    func feed(int16 samples: [Int16]) {
        guard let request, !samples.isEmpty else { return }
        guard let format = AVAudioFormat(commonFormat: .pcmFormatInt16,
                                         sampleRate: 16000,
                                         channels: 1,
                                         interleaved: true),
              let buffer = AVAudioPCMBuffer(pcmFormat: format,
                                            frameCapacity: AVAudioFrameCount(samples.count)) else {
            return
        }
        buffer.frameLength = AVAudioFrameCount(samples.count)
        if let channel = buffer.int16ChannelData?[0] {
            samples.withUnsafeBufferPointer { source in
                channel.update(from: source.baseAddress!, count: samples.count)
            }
        }
        request.append(buffer)
    }

    func stop() {
        request?.endAudio()
        task?.finish()
        task = nil
        request = nil
    }

    func cancel() {
        task?.cancel()
        task = nil
        request = nil
    }

    // MARK: - 文件识别（自检与文件转写用）

    /// 同步识别一个音频文件，返回全文与耗时。用于验证语言包与精度。
    static func transcribeFile(path: String, locale: String,
                               timeout: TimeInterval = 120) -> (text: String, firstPartial: TimeInterval, total: TimeInterval)? {
        guard SFSpeechRecognizer.authorizationStatus() == .authorized,
              let recognizer = SFSpeechRecognizer(locale: Locale(identifier: locale)) else {
            return nil
        }
        let request = SFSpeechURLRecognitionRequest(url: URL(fileURLWithPath: path))
        request.shouldReportPartialResults = true
        request.requiresOnDeviceRecognition = recognizer.supportsOnDeviceRecognition

        let started = Date()
        var first: TimeInterval?
        var finalText = ""
        var finished = false

        recognizer.recognitionTask(with: request) { result, error in
            if let result {
                if first == nil, !result.bestTranscription.formattedString.isEmpty {
                    first = Date().timeIntervalSince(started)
                }
                if result.isFinal {
                    finalText = result.bestTranscription.formattedString
                    finished = true
                }
            }
            if error != nil { finished = true }
        }
        let deadline = Date().addingTimeInterval(timeout)
        while !finished && Date() < deadline {
            RunLoop.current.run(mode: .default, before: Date().addingTimeInterval(0.1))
        }
        guard !finalText.isEmpty else { return nil }
        return (finalText, first ?? 0, Date().timeIntervalSince(started))
    }
}
