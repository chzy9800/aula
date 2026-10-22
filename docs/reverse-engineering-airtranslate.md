# AirTranslate 逆向记录（静态分析）

> 对象：`/Applications/AirTranslate.app`（`dev.appcaster.AirTranslate`，1.16.0，arm64 Mach-O，10.5MB）
> 方法：静态分析——框架依赖（otool）、Swift 符号与字符串（strings）、授权（codesign）。未运行、未脱壳、未触及其服务。
> 目的：回答"它为什么能转录电脑播放的声音、aula 能不能搬"。写作日期 2026-10-22。

## 一、一句话结论

它是一个**原生 macOS 应用**：用 **ScreenCaptureKit** 抓系统播放的音频，用 **Apple Speech** 流式识别，用 **Apple Translation**（或用户自配的云服务商）翻译，全程无需虚拟声卡。这三样都是系统框架，**只有原生 App 拿得到**——浏览器页面（aula 的形态）在 macOS 上只能拿到"标签页音频"，这是平台权限边界，不是代码差异。

## 二、证据

### 1. 框架依赖（otool -L）

| 框架 | 用途 |
| --- | --- |
| **ScreenCaptureKit** | 抓取屏幕/系统音频（SCStream 系） |
| **Speech** | Apple 设端语音识别（SFSpeechRecognizer 系） |
| **Translation** + `_Translation_SwiftUI` | Apple 系统翻译（TranslationSession，macOS 15+） |
| AVFAudio / CoreAudio | 音频管线（AVAudioEngine → PCMBuffer） |

沙盒授权里有 `com.apple.security.device.audio-input`（麦克风）。

### 2. 自有 Swift 类型（符号还原，模块名 12 字符）

```
SystemAudioCaptureDelegate        ← ScreenCaptureKit 系统音频捕获
MicrophoneAudioCaptureDelegate    ← 麦克风捕获
LiveSpeechTranscriberDelegate     ← Apple Speech 流式转写
SpeechCaptionerDelegate           ← 字幕协调层
SpeechAnalyzerInputAdapting       ← 输入源适配（系统音频/麦克风二选一）
```

框架 API 足迹：`SCStream` / `SCStreamOutput` / `SCStreamDelegate` / `SCShareableContent`（捕获侧）；
`SFSpeechAudioBufferRecognitionRequest` / `SFSpeechRecognitionTask`（识别侧，喂 PCMBuffer 的流式接口）；
`TranslationSession` ×17（翻译侧）；`AVAudioEngine` / `AVAudioPCMBuffer` / `AVAudioFormat`（管线）。

### 3. 它不是纯离线——多服务商密钥模式

字符串里有一整套云连接类型与端点：`GrokWebSocketConnection`、`QwenWebSocketConnection`、
`NariWebSocketConnection`、`AzureMAIWebSocketConnection`、`GeminiLiveTranslationServiceDelegate`、
`MetaVoiceTranscribeServiceDelegate`、`QwenFileTranscriptionHTTPClient`；端点含
`api.openai.com/v1/responses`、`generativelanguage.googleapis.com/v1beta/interactions`、
`maas.qwencloudapi.com/.../asr/transcription`、`openrouter.ai/api/v1/...`、`api.typesafe.ai`。
Apple 本地链路是默认档，云服务商是用户自带 key 的增强档——**aula 不需要**（已有本地 whisper + 自有网关）。

### 4. 音频参数

字符串里出现 `sampleRate` 与 `16000`；`SCShareableContent` 里有 `displays` / `windows` 字样
（说明它做了可选范围）。识别配置（如 `requiresOnDeviceRecognition`）没有以明文出现——可能内联或被
编译器优化，此点标记为**未证实**。

## 三、对 aula 的三条可迁移结论

1. **采集**：ScreenCaptureKit 音频捕获 + `excludesCurrentProcessAudio`（排除自己）是 macOS 系统音频的正解，
   与我们的判断一致。aula 走 **Swift CLI 助手**复刻这条路：SCStream → AVAudioConverter 转 16k 单声道
   → 与浏览器 VAD 同口径切句 → 按 aula 的 WS 二进制帧协议（uint32 start_ms + uint32 end_ms + PCM16）推送。
   零第三方依赖，`swiftc` 单文件编译（CommandLineTools 即可，无需 Xcode）。
2. **分段策略不同**：它用 Apple Speech，**框架自带流式分段**，不需要自研 VAD；aula 用 whisper，
   必须先切句——所以 helper 里必须带 VAD（参数与浏览器 worklet 一致：静音 700ms / 最短语音 350ms / 单句上限 15s）。
3. **权限**：屏幕录制权限（TCC）挂在**发起进程**上。助手由 aula 服务端拉起，首次会触发系统弹窗，
   批准一次即可。这是绕不开的系统交互，AirTranslate 同样要过这一关。

## 四、没有搬的东西

- 它的多服务商云转写/翻译（aula 已有本地 whisper + WorkBuddy 网关，成本结构与它不同）
- 它的字幕 UI 体系（CaptionBoard / CaptionFeed 等，aula 有自己的玻璃拟态界面）
- Soniox（BBnotes 那边的付费方案）同理不需要
