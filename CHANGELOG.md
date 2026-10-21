# 更新日志

格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。

## [0.1.0] - 2026-10-21

首个版本：把三个独立工具合并成一套「课堂 → 笔记」的链路。

### 新增

- **上课工作区**：浏览器端 AudioWorklet 能量断句、重采样 16kHz、裁尾部静音；WebSocket 二进制帧送 PCM
- **转写四种实现**：`mock`（演示/CI）、OpenAI 兼容 `/audio/transcriptions`、本地 faster-whisper、Gemini 原生音频
- **逐句翻译**：术语表注入、LRU 缓存、专用 MT 优先、失败回退大模型
- **讲义工作区**：pdf.js 渲染、按行聚段（行距 + 字号 + 标题三条件）、双语对照、圈选提问、手写层、导出
- **听课记录**：整节课转写 → 分块抽取 → 全局合成 → 带 frontmatter 的 Markdown，含术语表与待自查问题
- **11 家服务商预设** + **7 条翻译引擎**（其中 3 条免密钥：`google_free` / `mymemory` / `libre`）
- **CLI**：`serve` / `providers` / `doctor` / `preview` / `notes` / `batch`
- **本地存储**：SQLite 四表（sessions / utterances / notes / summaries），音频不落盘
- **文档**：`docs/providers.md`（额度、切换、curl 示例、实测记录）、`docs/example-digest.md`

### 修复（合并三套代码时发现的真问题）

- 音频节点 `numberOfOutputs: 0` 若不接下游，部分浏览器不驱动 `process()`，表现是"点了录音没反应"——改为接静音 GainNode
- 触发断句的尾部静音被一起送去识别，音频白长 30%——改为按最后一个有声窗口裁尾
- 本机 `HTTP_PROXY` 会把 `localhost` 请求送进代理导致 502——对本机地址自动绕过
- 讲义段落阈值单一导致标题正文粘连、译文错位——改为三条件判定
- 听课记录在弹窗里以纯文本显示，术语表变成一堆竖线——补了表格解析与排版
- `--out 笔记` 会被当成文件名而不是目录——现在按扩展名判定

### 测试

- 154 项 Python 单测（解析 / 对齐 / 清洗 / 分块 / 存储 / ASR / 翻译 / MT / 预设 / 接口 / WebSocket）+ 7 项断句算法测试
- 端到端脚本覆盖两个工作区，不依赖真实模型
