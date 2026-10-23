# AirTranslate 复刻范围清单与验收标准

> 基准：`/Applications/AirTranslate.app` 1.16.0（`dev.appcaster.AirTranslate`，arm64）
> 证据来源：Info.plist、otool 框架依赖、Mach-O 类型符号、UI 文案字符串（英/中/日三语共 800+ 条）、
> 此前逆向记录 `docs/reverse-engineering-airtranslate.md`。**未运行程序、未反编译业务逻辑**，
> 因此凡是推断项都标了「推定」，实现时以实测为准。
> 写作日期 2026-10-23。

---

## 一、它的产品形态（一句话）

**菜单栏常驻 + 主字幕板 + 可脱拽浮窗**的三窗口结构：抓系统音频/麦克风 → 流式转写 →
可选翻译 → 字幕实时上屏 → 会话结束存档成可管理的转录文件。默认全本地（Apple 框架），
可换云服务商；额外支持把译文朗读出来。

与我们当前 aula 的形态差异，决定了复刻工作量的大头：

| 维度 | AirTranslate | aula 现状 |
| --- | --- | --- |
| 转写 | Apple Speech（框架自带流式分段） | whisper + 自研 VAD 切句 |
| 翻译 | Apple Translation 默认，云可换 | LLM（网关/DeepSeek，已流式） |
| 字幕界面 | 主窗口 + 浮窗 + 菜单栏面板，外观高度可配 | 主窗口 + 浮窗（外观固定） |
| 语音输出 | 有（TTS 朗读译文） | **无** |
| 转录库 | 有（管理/删除/打开目录） | 有听课记录，但无"库"管理 UI |
| 多语言界面 | 中/日/英 | 中文 |
| 说话人标签 | 有（Speaker A/B） | **无** |

---

## 二、模块总表与优先级

| 编号 | 模块 | 优先级 | 复刻难度 | 说明 |
| --- | --- | --- | --- | --- |
| M1 | 音频采集与输入源 | P0 | 低 | 已完成 80%（系统声音 + 麦克风） |
| M2 | 流式转写与断句 | P0 | 中 | 已有；需补「稳定性」与「多候选选优」 |
| M3 | 翻译链路 | **P0（重点）** | 中 | 已有流式；需补语言对/术语/格式保留 |
| M4 | 主字幕板（CaptionBoard） | P0 | 中 | **✅ 已做**：外观设置 + 渐隐遮罩 + 描边 |
| M5 | 浮动字幕窗 | P0 | 低 | **✅ 已做**：拖拽 + 缩放 + 位置记忆 + 透明度 |
| M6 | 语音输出（TTS） | P1 | 中 | **✅ 已做**：定稿朗读 + 音色/音量/语速 |
| M7 | 会话流程与状态机 | P0 | 中 | **部分**：新建/续用已做；录音期锁定设置待做 |
| M8 | 设置（分类/权限/Provider/密钥） | P0 | 中 | **✅ 已做**：8 类侧边栏 + 权限页 |
| M9 | 转录库与导出 | P1 | 中 | **✅ 已做**：列表/导出/继续/删除 |
| M10 | 文件转写（音频文件/URL） | P2 | 中 | 全新 |
| M11 | 菜单栏与命令/快捷键 | P0 | 低 | 部分完成 |
| M12 | 国际化 | P1 | 低 | 全新（中/英/日） |
| M13 | 错误态与降级 | P0 | 中 | 部分完成 |

---

## 三、逐模块：功能点 / 界面元素 / 交互细节 / 验收标准

### M1 音频采集与输入源

**功能点**
- 系统音频：ScreenCaptureKit `SCStream`，排除自身进程音频
- 麦克风：`AVAudioEngine`，可选输入设备
- 输入源二选一，**录音期间锁定**（证据文案：`Locked during session`、`Stop capture before changing this setting.`）
- 无可用显示器时明确报错（`No active display was available for system audio capture.`）

**界面元素**：输入源选择器（Mac Audio / Microphone）、麦克风设备下拉（`MicrophoneInputDevice`）、音量显示

**交互细节**
- 选择麦克风时才请求麦克风权限（`Required only when Microphone is selected as the audio input.`）
- 启动中改设置 → 报错并要求重来（`Capture settings changed while starting. Start again.`）

**验收标准**

| 指标 | 阈值 | 测量方法 |
| --- | --- | --- |
| 系统音频捕获通路 | 峰值 > 0，样本率 16k±1 | 内置诊断，播 TTS 抓 6 秒 |
| 麦克风捕获通路 | 同上 | 同上 |
| 设备枚举延迟 | ≤ 300ms | 打开输入源选择器计时 |
| 录音期间切换输入源 | 被禁止且提示可见 | 手动 |
| 采集启动失败的可读文案 | 100% 场景有具体原因（非"失败"二字） | 断网/拒权/无显示器 3 例 |

---

### M2 流式转写与断句

**功能点**
- 边说边出字（草稿行），说完转定稿
- **字幕稳定性**（`Caption Stability`）：抑制同句频繁改写
- **多候选选优**（二进制内 prompt：`Choose exactly one supplied full ASR candidate for translation... Choose abstain when uncertain`）——ASR 给 n-best，LLM 选一条再翻
- 段落静音阈值可调（`Paragraph Break Silence`）
- 语言自动检测（`Automatic language detection`），新语言提示（`New input language detected`）

**界面元素**：草稿行（浅色/斜体）、行内光标、语言检测徽标、稳定性设置项

**交互细节**
- 草稿原位覆盖，不追加半句（aula 已实现）
- 用户上滚阅读时不抢滚动，提供「跳回最新」（`Jump to latest captions`）

**验收标准**

| 指标 | 阈值 | 测量方法 |
| --- | --- | --- |
| 草稿首字延迟（说完半句内） | P50 ≤ 1.0s，P95 ≤ 1.8s | 30 句回归集，记录帧时间戳 |
| 定稿相对草稿的文本变更幅度 | 相似度 ≥ 0.8（Levenshtein） | 同批语料自动比对 |
| 同一句被整段替换次数 | **0**（只允许追加/微调） | UI 渲染日志 |
| 断句边界与人工标注一致率 | ≥ 90% | 20 分钟课堂音频 + 人工标注 |
| 语言检测准确率 | ≥ 95% | 30 段混合语料（es/en/zh/fr/de） |

---

### M3 翻译链路（重点验收项，详见第四节）

**功能点**
- 三种输出模式：仅转写 / 仅译文 / **译文在上原文在下**（`Translation above original`）
- 源语言：自动检测或手选；目标语言手选
- 默认 Apple Translation，可切云 provider
- 术语保护（`Keep these terms unchanged`）
- 转录清理（`Foundation Clean`，本地模型润色）
- 翻译关闭时明确提示（`Translation is off in Transcribe Only mode.`）
- 同语言对时提示（`Choose different source and target languages to translate.`）

**界面元素**：输出模式选择器、语言选择（源/目标）、术语表编辑区、清理开关

**交互细节**
- 语言对相同 → 禁用翻译并说明
- 云 provider 缺密钥 → 启动前拦截并给出"去设置"入口（`Save your Qwen API key before starting.`）

**验收标准**：见第四节（翻译效果专项）

---

### M4 主字幕板（CaptionBoard）

**类型清单（证据）**：`CaptionBoardView`、`CaptionLineView`、`CaptionTranscriptFeed`、
`CaptionFeedViewportState`、`CaptionFeedFollowState`、`CaptionFeedScrollMetrics`、
`CaptionFeedReadingAnchor`、`TranscriptScrollFadeMask`、`CaptionFeedLineFramesKey`、
`StageHeaderView`、`StageEmptyStateView`、`ConsoleBarView`、`ToastMessageView`

**功能点**
- 逐句双语列表，最新在下（课堂阅读方向）
- 自动跟随到底；用户上滚时暂停跟随，显示「跳回最新」
- 顶部/底部渐隐遮罩（`TranscriptScrollFadeMask`）
- 空态引导（`Captions will appear here.`）
- 顶部状态条（`StageHeaderView`）：会话标题/时长/状态
- 底部控制条（`ConsoleBarView`）：开始/停止/清空
- Toast 提示（`ToastMessageView`）

**界面元素**
- 行：时间戳、延迟徽标、原文（小/次要色）、译文（大/主色）、精译完成标记、说话人标签（可选）
- 外观设置：字号（`CaptionTextSizeButtons`）、文字颜色（`CaptionHexColorRow` + 预设 `CaptionPresetButton`，如 `Bold yellow with a dark outline` / `Dark text for bright content`）、对齐（`Caption Alignment`）、描边效果（`CaptionTextEffectModifier`）

**交互细节**
- 上滚 → 出现「跳回最新」浮标；点击回到底部并恢复跟随
- 字号调节即时生效（不打断录音）
- 颜色预设一键切换，也可自定义 HEX

**验收标准**

| 指标 | 阈值 | 测量方法 |
| --- | --- | --- |
| 3000 行（45 分钟课）注入+首屏布局 | ≤ 120ms | 内置 `--render-preview N` |
| 新句上屏后自动滚到底 | 100% 成功，无跳动 | 手动 + 录屏 |
| 上滚后是否被抢滚动 | 0 次 | 手动（滚动中持续出字幕） |
| 渐隐遮罩 | 顶部/底部各 24–40px 渐变 | 截图比对 |
| 字号/颜色修改生效延迟 | ≤ 100ms | 计时 |
| 空态文案 | 存在且可读 | 截图 |

---

### M5 浮动字幕窗

**类型清单（证据）**：`FloatingCaptionWindowController`、`FloatingCaptionWindowView`、
`FloatingCaptionDragSurface`（`DragView`）、`FloatingCaptionResizeHandle`（`ResizeView`）、
`FloatingCaptionSettingsView`、`FloatingCaptionModeChoiceView`、`MainCaptionVisibilityView`

**功能点**
- 无边框浮窗，置顶（`Keep above other windows`）
- 整面可拖拽（`DragView`）+ 右下角缩放（`ResizeView`）
- 记忆尺寸/位置，菜单可重置（`Reset Window Size`）
- 浮窗独立外观（`Floating Text Size`、`Floating Display`）
- 显示模式独立（`FloatingCaptionDisplayMode`）
- 与主窗口联动显示（`Show Both Windows` / `Show Floating Captions` / `Show captions only`）

**验收标准**

| 指标 | 阈值 | 测量方法 |
| --- | --- | --- |
| 拖拽响应 | 跟手无延迟（≤ 16ms/帧） | 手动 |
| 缩放最小尺寸 | 有下限且内容不溢出 | 手动缩到最小 |
| 位置记忆 | 重启后恢复上次位置 | 重启验证 |
| 置顶 | 覆盖其他 app 全屏窗口之上 | 手动（全屏视频上） |
| 点击浮窗是否抢焦点 | 不抢（`.nonactivatingPanel`） | 点击后主窗口仍可输入 |
| 保留行数 | ≥ 3 句 | 截图 |

---

### M6 语音输出（TTS）

**功能点（证据文案）**
- 朗读稳定后的译文（`Speak translated output when a stable translated segment is available.`）
- 开关（`Turn Voice Output On/Off`）、音量（`SettingsVolumeSlider`）
- 音色选择（`Text Translation Voice`、`Apple System Voice`、`Loading available voices`）
- 语音模型（`Translated Speech Model`）
- 关闭时音量项禁用并说明（`Turn on Voice Output to adjust translated speech volume.`）

**界面元素**：语音输出分组（开关 / 音色下拉 / 音量滑块）

**交互细节**
- 只在「译文稳定」后朗读，避免逐字朗读的噪音
- 朗读期间不阻塞字幕上屏

**验收标准**

| 指标 | 阈值 | 测量方法 |
| --- | --- | --- |
| 朗读触发条件 | 仅定稿译文，不读草稿 | 日志 |
| 首音延迟 | ≤ 400ms（从定稿到出声） | 录音分析 |
| 朗读与字幕顺序一致 | 100% | 手动 |
| 音量调节生效 | 即时 | 手动 |
| 关闭后零音频输出 | 100% | 波形检测 |

---

### M7 会话流程与状态机

**状态**（由文案与类型推断）：`Ready to start listening` → `Checking ... permission` →
`Starting ... capture` → `Listening to Mac audio, waiting for speech...` →
`Preparing to listen` → 录音中 → `Finishing the last ... segment` → `Transcript saved`

**关键规则**
- 启动前三项权限检查，缺哪项说哪项（`Checking screen recording permission...` 等）
- 启动失败有专门界面（`CaptureStartFailureView`），含 `Dismiss start failure`、`Check in Settings`
- 新会话 vs 保留当前会话（`Start New Session` / `Keep Current Session`）
- 会话时长模式（`SessionDurationMode`）：短会即时更新 vs 长会批处理
- 设置项在会话中锁定（`Stop the session to edit keys`）
- 中途断流：保留已收到的字幕（`Captions already received are preserved.`）

**验收标准**

| 指标 | 阈值 | 测量方法 |
| --- | --- | --- |
| 从点击开始到"可说话" | ≤ 1.5s（本地链路） | 计时打点 |
| 权限缺失时的引导 | 3 种权限各自有文案 + 直达系统设置按钮 | 逐项关权限验证 |
| 启动失败可恢复 | 关掉失败态后可立即重试 | 手动 |
| 断流后已收字幕 | 100% 保留 | 拔网/杀服务验证 |
| 会话中改设置 | 被阻止且提示原因 | 手动 |
| 结束收尾 | 最后一句不丢（flush 等排空） | 说完立刻停止，检查入库 |

---

### M8 设置

**类型清单（证据）**：`SettingsView`、`SettingsSidebar`、`SettingsSidebarRow`、
`SettingsCategory`、`SettingsPageHeader`、`SettingsGroup`、`SettingsControlRow`、
`SettingsToggleRow`、`SettingsValueRow`、`SettingsNoticeRow`、`SettingsNoticeActionRow`、
`SettingsPermissionRow`、`SettingsAssetAvailabilityRow`、`SettingsVolumeSlider`、
`APIKeySettingsView`、`ProviderCredentialRow`、`AppleTranslationSettingsView`

**分类（由文案归纳）**
1. Session Workflow（会话流程）— "Set the default translation mode and language behavior."
2. Transcription（转写）— "Tune paragraphing, long sessions, and optional transcript file saving."
3. Translation（翻译）— 输出模式、语言、术语保护
4. Caption（字幕外观）— 字号、颜色、对齐、稳定性
5. Floating Captions（浮窗）— "Configure the detachable floating caption window."
6. Voice Output（语音输出）
7. Content and Reading（内容与阅读）
8. API Keys — "Manage your cloud providers in one place."
9. Permissions — 三项权限状态行 + `Open Privacy Settings`
10. About — 版本号、许可

**交互细节**
- 侧边栏分页，右侧带页头说明（每页一句说明它管什么）
- 权限行显示真实状态（`SettingsPermissionState`）并可跳转系统设置
- 本地模型资产状态（`ModelAvailability`）+ 下载入口（`TranslationAssetDownloadView`）
- 密钥存 Keychain，保存后只显示"已保存"（`Saved in Keychain.`），可替换/移除

**验收标准**

| 指标 | 阈值 | 测量方法 |
| --- | --- | --- |
| 分类数量与命名 | ≥ 8 类，每类有页头说明 | 截图清点 |
| 权限状态准确 | 与实际 TCC 状态一致 | 开关系统权限后对比 |
| 密钥存储 | Keychain，不落明文文件 | `security find-generic-password` 验证 |
| 密钥保存后回显 | 仅显示"已保存"，不回显明文 | 截图 |
| 设置持久化 | 重启后全部保留 | 重启验证 |
| 首次进入设置耗时 | ≤ 300ms | 计时 |

---

### M9 转录库与导出

**类型清单（证据）**：`TranscriptLibraryView`、`TranscriptLibraryRow`、
`SavedTranscript`、`SavedTranscriptPart`、`SavedTranscriptFile`、`SavedTranscriptContentMode`、
`Manage Saved Transcripts`、`Open Save Folder`、`Delete Transcript`、`Save Transcript Files`

**功能点**
- 会话结束自动存档（`Transcript saved`）
- 库列表：标题、时间、时长、句数；未命名显示 `Untitled Transcript`
- 内容范围可选（`SavedTranscriptContentMode`：原文 / 双语 / 仅译文）
- 打开所在文件夹、删除单条/全部（`Delete every saved transcript file.`）
- 超大转录的保护（`The transcript is too large to display in AirTranslate.`）

**验收标准**

| 指标 | 阈值 | 测量方法 |
| --- | --- | --- |
| 存档完整性 | 句数与会话内计数一致，误差 0 | 结束会话后比对 |
| 三种保存内容的字段正确性 | 100% | 各存一份检查 |
| 删除单条不影响其他 | 100% | 手动 |
| 打开目录 | 正确定位到文件 | 手动 |
| 超大转录（>5MB） | 不崩溃，给出提示 | 构造大文件 |

---

### M10 文件转写

**功能点**：选择本地音频 / 粘贴公开 HTTPS URL（`Transcribe an audio file`、`Public HTTPS audio URL`、`QwenAudioFileTranscriptionView`）

**验收标准**

| 指标 | 阈值 | 测量方法 |
| --- | --- | --- |
| 本地文件支持格式 | ≥ wav/mp3/m4a | 各测一个 |
| URL 转写 | 公开 https 可下载即可 | 测 1 个公开样例 |
| 进度可见 | 有进度或状态文字 | 手动 |
| 失败原因可读 | 具体到"格式不支持/下载失败/超长" | 3 例 |

---

### M11 菜单栏与命令/快捷键

**类型清单（证据）**：`MenuBarPanelController`、`MenuBarStatusView`、`CaptionCommands`、
`CaptureCommands`、`Open Main Window`、`Show Captions only`、`includesKeyboardShortcuts`

**功能点**
- 菜单栏图标 + 弹出面板（状态、开始/停止、显示主窗口/浮窗）
- 全局快捷键开始/停止（aula 已用 ⌥⌘R）
- 菜单栏状态随会话变化（空闲/录音中/错误）

**验收标准**

| 指标 | 阈值 | 测量方法 |
| --- | --- | --- |
| 全局快捷键 | ⌥⌘R 在任何前台 app 下生效 | 手动（Safari 前台按） |
| 与其他 app 冲突时 | 明确提示不可用 | 占用后启动 |
| 菜单栏图标状态 | 3 态（空闲/录音/错误）可区分 | 截图 |
| 弹出面板响应 | ≤ 150ms | 计时 |

---

### M12 国际化

**证据**：二进制内含中文（692 条）、日文、英文文案 → 界面多语言。

**验收标准**

| 指标 | 阈值 | 测量方法 |
| --- | --- | --- |
| 语言覆盖 | 中/英/日 三语 | 切换系统语言后重启 |
| 文案完整度 | 无英文残留（中文界面下） | 全界面巡检 |
| 布局不溢出 | 三语下按钮文字均不截断 | 截图巡检 |
| 翻译语言名本地化 | 中文界面显示「简体中文」等 | 截图 |

---

### M13 错误态与降级

**证据文案**：`CaptureStartFailureView`、各类 provider 专属错误（如 `Grok setup timed out. Check your network and API key.`）、`Realtime fallback`、`Temporarily unavailable while automatic detection is improved.`

**原则（沿用本项目既有纪律）**
- 静默丢弃必须留原因（aula 已在此处踩过坑：`Translator` 吞掉 `LLMError` 导致"字幕少一句"无迹可查）
- 失败态给可执行下一步，不给"未知错误"

**验收标准**

| 指标 | 阈值 | 测量方法 |
| --- | --- | --- |
| 错误文案含原因 + 下一步 | 100% | 巡检所有 alert/notice |
| 网络中断 | 已收字幕保留 + 明确提示 | 断网验证 |
| 无静默丢句 | 每条丢弃都有日志 | 日志审计 |
| 失败后可重试 | 不重启 app 即可重试 | 手动 |

---

## 四、翻译效果专项验收（重点）

这一节是全套清单的核心。所有指标都基于**固定回归集**，每次改动都跑，不允许"目测还行"。

### 4.1 测试集

| 集合 | 规模 | 用途 |
| --- | --- | --- |
| A. 西语课堂（本专业） | 60 句 / 约 15 分钟音频 | 主战场，含电气装置术语 |
| B. 英语课堂 | 40 句 | 第二高频语言 |
| C. 边界用例 | 30 句 | 数字、单位、公式、URL、代码、专名、半句、语气词 |
| D. 多语言扩展 | 各 15 句：fr / de / it / pt / ja | 覆盖声明支持的语言对 |
| E. 混合语言 | 20 段（西英混说） | 语言检测与切换 |

每句都有**人工标准译文**，人员盲评时不知道来自哪个实现。

### 4.2 准确性指标

| 指标 | 阈值 | 测量方法 |
| --- | --- | --- |
| 术语命中率 | ≥ 95% | 100 词对照表（`instalación eléctrica` / `diferencial` / `consignar` …），自动比对标准译法 |
| 数字/单位保留 | **100%** | 正则抽取原文数字+单位，检查译文全在 |
| 公式/化学式保留 | 100% | 同上 |
| URL / 邮箱 / 代码片段 | 100% 原样 | 精确匹配 |
| 专名首次出现附原文 | ≥ 90% | 人工计数 |
| 语义等价（人工） | 平均 ≥ 4.3 / 5（5 分制） | 50 句抽样，双人盲评，Kappa ≥ 0.7 |
| 语义等价（自动，有参考译时） | COMET ≥ 0.82 或 BLEU ≥ 0.45 | 脚本 |
| 显著误译率（改变原意） | ≤ 1% | 人工标注 |
| 漏译/空译文 | **≤ 0.5%** | 脚本统计（aula 曾在推理模型上出过 6/9 空译，此指标专门守它） |
| 过度意译（添加原文没有的信息） | ≤ 2% | 人工标注 |

### 4.3 响应速度指标

从「音频段定稿」到「屏幕可见」的端到端计时，每项取 P50 / P95：

| 指标 | 阈值 | 测量方法 |
| --- | --- | --- |
| 草稿首字（边说边出） | P50 ≤ 1.0s / P95 ≤ 1.8s | 帧级打点 |
| 译文首字 | P50 ≤ 1.2s / P95 ≤ 2.5s | WS delta 首条时间戳 |
| 译文整句完成 | P50 ≤ 2.5s / P95 ≤ 4.5s | 最后一条 delta |
| 端到端（说完 → 完整译文可读） | P50 ≤ 3.5s / P95 ≤ 6.0s | 音频结束时间戳 → 渲染完成 |
| 首字前屏幕内容 | 100% 有原文（不允许空白等待） | 录屏抽查 |
| 同一句渲染的"整段替换"次数 | **0** | 渲染日志（当前 aula 已达成：70 delta / 3 句） |
| 连续 30 分钟延迟漂移 | 第 30 分钟 P50 ≤ 第 1 分钟 P50 × 1.5 | 长跑测试 |

### 4.4 多语言支持

| 指标 | 阈值 | 测量方法 |
| --- | --- | --- |
| 声明支持的语言对 | es→zh, en→zh, es→en, zh→en, fr→zh, de→zh, it→zh, pt→zh, ja→zh | 逐对跑集合 D |
| 每对语义等价 | ≥ 4.0 / 5 | 人工抽评 |
| 自动语言检测准确率 | ≥ 95% | 集合 E |
| 检测到语言切换的响应 | ≤ 1 句内提示并切换 | 集合 E + 日志 |
| 同语言对（源=目标） | 明确禁用翻译并说明 | 手动 |
| 不支持的源语言 | 启动前拦截，说明原因 | 手动（选一个不在列表的） |

### 4.5 格式保留

| 项 | 阈值 | 例子 |
| --- | --- | --- |
| 数字与小数点 | 100% 保留 | `1.500 V` → `1500 V` 不许串成 `1.5` |
| 单位与符号 | 100% 保留 | `mm²`、`kWh`、`%`、`°C` |
| 西语特有标点 | 正确映射 | `¿...?` `¡...!` → 中文问号/感叹号，不残留反向符号 |
| 缩写与首字母词 | 保留原文 + 必要解释 | `PIA`、`RCD`、`IGBT` |
| 术语括号注原文 | 首次出现附原文，同句不重复 | 已有行为，需补"同段不重复" |
| 列表/编号 | 保留结构 | `1.` `2.` `a)` |
| 换行与段落 | 不合并跨段 | 与 `Paragraph Break Silence` 联动 |

### 4.6 稳定性（防抖动）

| 指标 | 阈值 | 测量方法 |
| --- | --- | --- |
| 已显示的译文被整段替换 | **0 次** | 渲染日志 |
| 已显示的原文被整段替换 | ≤ 1 次/句，且相似度 ≥ 0.8 | 同批语料 |
| 术语在同一会话内漂移 | **0**（同一术语前后译法一致） | 日志比对 |

### 4.7 复现方式

```bash
# 固定回归集跑分（脚本待建，随实现一起交付）
bash scripts/bench_translate.sh --set A --provider deepseek --effort low
# 输出：逐句延迟 / 空译数 / 术语命中 / 数字保留 / 抖动次数
```

---

## 五、与 aula 现状的差距（做完这份清单要补的东西）

| 模块 | 已有 | 要补 |
| --- | --- | --- |
| M1 采集 | 系统声音 + 麦克风 + 诊断 | 麦克风设备选择、录音期锁定提示 |
| M2 转写 | VAD 切句 + 草稿流式 | 稳定性策略、多候选选优、段落静音阈值可调 |
| M3 翻译 | 流式 + 术语表（自动回收） | 三种输出模式 UI、手动术语表、格式保留专项 |
| M4 字幕板 | 逐句双语 + 草稿行 + 精译标记 | 外观设置（字号/颜色/对齐/描边）、渐隐遮罩、跳回最新 |
| M5 浮窗 | 置顶 + 保留 3 句 | 拖拽移动、缩放、位置记忆、独立外观 |
| M6 TTS | **无** | 全部 |
| M7 会话 | 状态机 + 失败态 | 会话时长模式、新会话/保留会话、锁定规则 |
| M8 设置 | 菜单里零散 | 完整设置窗口（侧边栏 8+ 类）、Keychain 密钥管理、权限页、资产下载页 |
| M9 转录库 | 听课记录导出 | 库列表 UI、保存内容范围、删除/打开目录、超大保护 |
| M10 文件转写 | 无 | 本地文件 + URL |
| M11 菜单栏 | 菜单 + ⌥⌘R | 菜单栏弹出面板、状态三态 |
| M12 i18n | 中文 | 中/英/日 |
| M13 错误 | 有日志与部分文案 | 全量错误文案审计 |

---

## 六、明确不复刻的部分（理由）

| 项 | 理由 |
| --- | --- |
| Apple Speech / Apple Translation / Foundation Models | 系统私有能力，只能通过同款框架调用；aula 用 whisper + LLM，**效果对标、实现不同**。验收标准按"效果"而非"实现"衡量 |
| 12 家云服务商接入 | 逐家复刻价值低（每家 1–2 天且需账号）；保留 3 家（DeepSeek 官方 / 网关 / 一家海外）已覆盖使用场景 |
| Azure Foundry / Nari / TypeSafe / Muse 等专有后端 | 上游产品自身在 Beta，接口可能变，接进来是维护负担 |
| 免费公测的许可证体系 | 与自用无关 |

---

## 七、分期建议

| 期 | 内容 | 产出 |
| --- | --- | --- |
| **P0（先做）** | M4 外观设置 + M5 浮窗拖拽缩放 + M8 设置窗口骨架 + M13 文案审计 | 界面一致度高，用户第一眼就觉得"一样" |
| **P1** | M3 翻译专项（格式保留/术语表 UI/三模式）+ M7 会话流程 + M9 转录库 + M6 TTS | 功能一致 |
| **P2** | M10 文件转写 + M12 i18n + M2 多候选选优 | 完整性 |

---

## 八、需要你确认的三件事

1. **TTS 要不要做**（M6）？它会让"听课"变成"听译"，但打断老师原声，课堂场景是否合适你更清楚。
2. **多语言支持到哪里**（4.4 那 9 个语言对）？还是先只保 es→zh / en→zh？
3. **云服务商保留几家**？我建议 3 家（DeepSeek 官方 / 本地网关 / 一家海外兜底）。

确认后我按 P0 开始，每完成一个模块给你可验收的构建，不一次性铺开。
