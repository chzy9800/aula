import Speech
import Foundation
import AVFoundation

func log(_ s: String) { FileHandle.standardError.write((s + "\n").data(using: .utf8)!) }

@main
struct Probe {
    static func main() async {
        setbuf(stdout, nil)
        guard #available(macOS 26.0, *) else { return }
        let url = URL(fileURLWithPath: CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "/tmp/aula-probe/s1.wav")
        log("1. 开始")
        do {
            let transcriber = SpeechTranscriber(locale: Locale(identifier: "es-ES"),
                                                transcriptionOptions: [],
                                                reportingOptions: [.volatileResults],
                                                attributeOptions: [])
            log("2. transcriber 建好")
            let analyzer = SpeechAnalyzer(modules: [transcriber])
            log("3. analyzer 建好")

            let consumer = Task {
                log("4. consumer 启动")
                do {
                    for try await r in transcriber.results {
                        print(String(format: "%@ %.2fs「%@」", r.isFinal ? "定稿" : "草稿",
                                     Date().timeIntervalSince1970, String(r.text.characters)))
                    }
                    log("5. 结果流结束")
                } catch { log("5b. 流错误: \(error)") }
            }

            let file = try AVAudioFile(forReading: url, commonFormat: .pcmFormatFloat32, interleaved: false)
            let format = file.processingFormat
            log("6. 文件打开 \(Int(format.sampleRate))Hz Float32")

            let (stream, cont) = AsyncStream<AnalyzerInput>.makeStream(bufferingPolicy: .unbounded)
            log("7. 准备 start…")
            try await analyzer.start(inputSequence: stream)
            log("8. start 返回，开始投喂")

            let chunk: AVAudioFrameCount = 4096
            var fed = 0
            var round = 0
            while true {
                round += 1
                if round <= 3 || round % 10 == 0 { log("9. 第 \(round) 块开始") }
                guard let buf = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: chunk) else { log("9a. buffer 失败"); break }
                try file.read(into: buf)
                if round <= 3 { log("9. 第 \(round) 块读到 \(buf.frameLength) frames") }
                if buf.frameLength == 0 { log("9b. 读完了"); break }
                let result = cont.yield(AnalyzerInput(buffer: buf))
                if case .terminated = result { log("9c. 流已终止，停止投喂"); break }
                fed += Int(buf.frameLength)
            }
            log("10. 投喂完成 \(fed) frames")
            cont.finish()
            log("11. 流已关闭，等结果")
            try? await analyzer.finalizeAndFinishThroughEndOfInput()
            log("12. finalize 完成")
            _ = await consumer.value
            log("13. consumer 结束")
            print("PROBE OK")
        } catch { log("失败: \(error)") }
    }
}
