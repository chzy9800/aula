# macOS 26+ 的 SpeechTranscriber：AirTranslate 快的真正原因

## 结论

AirTranslate 用的是 **macOS 26 引入的 `SpeechAnalyzer` + `SpeechTranscriber`**，
不是我们之前在用的 `SFSpeechRecognizer`（2019 年的老 API）。

实测（macOS 27.0.1，es-ES，同一段 TTS 音频）：

| 引擎 | 首字 | 定稿 |
| --- | --- | --- |
| `SpeechTranscriber`（新） | **0.10 s** | **0.24 s** |
| `SFSpeechRecognizer`（旧） | ~0.3 s | ~0.5 s |
| whisper small（本地） | 0.8 s+ | 1.2 s+ |

**这就是 5 倍差距的来源。** 不是参数问题，是 API 代际问题。

## 环境验证

```
系统: macOS 27.0.1
SpeechTranscriber 支持语言数: 45
西语变体: es_MX, es_ES, es_CL, es_US
已安装西语: es_CL, es_US, es_MX, es_ES   ← 全部已安装，不用下载
```

## 可用 API

```swift
let transcriber = SpeechTranscriber(
    locale: Locale(identifier: "es-ES"),
    transcriptionOptions: [],
    reportingOptions: [.volatileResults, .fastResults],
    attributeOptions: []
)
let analyzer = SpeechAnalyzer(modules: [transcriber])

// 消费结果（volatile = 边说边出，final = 定稿）
for try await result in transcriber.results {
    let text = String(result.text.characters)
    // result.isFinal 区分草稿/定稿
}

// 方式 A：整个文件（已验证可用，首字 0.1s）
try await analyzer.analyzeSequence(from: audioFile)

// 方式 B：实时流（需继续调试）
let (stream, cont) = AsyncStream<AnalyzerInput>.makeStream()
try await analyzer.start(inputSequence: stream)
cont.yield(AnalyzerInput(buffer: pcmBuffer))
```

## 还没解决的

1. `analyzeSequence(from:)` 只出了第一句（"La instalación"），后续没结果——
   可能是结果流提前结束，或音频被当作单段
2. `start(inputSequence:)` 实时路径静默无输出，需排查 buffer 生命周期与时序

## 为什么值得继续

新 API 的 `volatileResults` 就是"边说边出字"的原生实现，不需要 VAD 切句、
不需要等静音、不需要 whisper 推理。首字 0.1 秒意味着**说话同时字幕就出来**，
这才是 AirTranslate 的体感来源。

## 参考

`docs/probes/speech-transcriber-probe.swift` —— 可直接跑的探针（analyzeSequence 路径）。

---

## 第二轮调试记录（2026-10-28）

### 已确认

1. **音频格式要求**：必须是 **Float32 非交错**。`AVAudioFile(forReading:)` 默认给 Int16 交错，
   analyzer 拿到会**静默无输出**（和 SFSpeech 同一个坑）。正确写法：
   ```swift
   let file = try AVAudioFile(forReading: url, commonFormat: .pcmFormatFloat32, interleaved: false)
   ```
2. `SpeechAnalyzer.bestAvailableAudioFormat` 是静态属性，但**有歧义重载**，直接调用编译不过。
3. 去掉 `.fastResults` 不影响截断问题。

### 仍卡住

`analyzer.start(inputSequence:)` + `cont.yield(AnalyzerInput(buffer:))` 路径：
- start 正常返回
- 第 1 块读到 4096 frames
- **`cont.yield` 阻塞**，第 2 块再没开始
- 即使 `makeStream(bufferingPolicy: .unbounded)` 也一样

推测：analyzer 对 inputSequence 的消费是延迟/拉取式的，yield 在它开始消费前会阻塞；
或者需要先 await 某个「就绪」状态（如 `analyzer.state == .running`）。

### 可行的替代路径

`analyzeSequence(from: AVAudioFile)` **能工作**（首字 0.10s / 定稿 0.24s），
但只返回第一句。如果继续用这条路，需要按 3–5 秒切分音频、逐段调用——
代价是文件 IO 和段间延迟，失去"边说边出字"的优势。

### 探针

- `docs/probes/speech-transcriber-probe.swift` —— analyzeSequence 路径（可用，会截断）
- `docs/probes/speech-transcriber-stream-probe.swift` —— inputSequence 路径（卡在 yield）
