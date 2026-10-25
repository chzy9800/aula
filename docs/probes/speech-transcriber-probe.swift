import Speech
import Foundation
import AVFoundation

@main
struct Probe {
    static func main() async {
        setbuf(stdout, nil)
        guard #available(macOS 26.0, *) else { return }
        let url = URL(fileURLWithPath: CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "/tmp/aula-probe/s1.wav")
        let locale = Locale(identifier: "es-ES")
        do {
            let transcriber = SpeechTranscriber(locale: locale,
                                                transcriptionOptions: [],
                                                reportingOptions: [.volatileResults, .fastResults],
                                                attributeOptions: [])
            let analyzer = SpeechAnalyzer(modules: [transcriber])
            let started = Date()

            let consumer = Task {
                do {
                    for try await r in transcriber.results {
                        let t = String(r.text.characters)
                        print(String(format: "%@ %.2fs「%@」", r.isFinal ? "定稿" : "草稿",
                                     Date().timeIntervalSince(started), t))
                    }
                } catch { print("流错误: \(error)") }
            }

            let file = try AVAudioFile(forReading: url)
            print("开始 analyzeSequence…")
            try await analyzer.analyzeSequence(from: file)
            print("analyzeSequence 返回，等结果…")
            try await Task.sleep(nanoseconds: 3_000_000_000)
            try? await analyzer.finalizeAndFinishThroughEndOfInput()
            _ = await consumer.value
            print("PROBE OK")
        } catch {
            print("失败: \(error)")
        }
    }
}
