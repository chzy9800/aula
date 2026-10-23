# 更新日志

格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。

## [未发布]

### 新增：音频诊断——把「识别不出」拆成三种可分辨的故障

用户报「系统声音识别不出」时，背后可能是三种完全不同的原因，修法互不相通：

| 现象 | 原因 | 修法 |
| --- | --- | --- |
| 一个样本都没收到 | 屏幕录制权限没生效 | 授权后**退出并重开 app**（macOS 要求） |
| 有数据但峰值 0 | 权限给了、音频未放行 | 系统设置里把 aula 关掉再打开，重启 app |
| 有声音但 VAD 不切句 | 音量低于触发阈值 | 调大系统音量，或改用麦克风 |

菜单里加了「音频诊断…」：跑 6 秒捕获 → 用**独立的 VAD 实例**统计（不污染正式链路的
切句状态）→ 直接给出上面哪一种，以及下一步该做什么。

配套 `aula/app/AppLog.swift`：壳层日志落盘到 `~/Library/Logs/aula/app.log`。
`open` 启动的 app 没有终端，stderr 进系统日志，用户看不到也贴不出来——没有这个文件，
「识别不出」就只能靠猜。麦克风路径加了同口径统计（每 2 秒一行 chunks/peak/srcRate），
静音键、选错输入设备这类问题同样能一眼看出。

实测：7 秒捕获 349 块 / 111680 样本 / peak 18975，日志逐条落盘。


### 新增：全局快捷键 ⌥⌘R 与悬浮字幕窗——上课不用切窗口

`aula/app/HotKey.swift`：Carbon `RegisterEventHotKey` 注册全局热键，**不需要辅助功能
权限**（`NSEvent.addGlobalMonitorForEvents` 需要，还得重启 app）。注意⌥⌘R 在界面聚焦
时也能用，但事件被 Carbon 先吃掉，不会双触发。注册失败是这条链路唯一的失败点（键位被
别的应用占用），所以失败会写在菜单栏标题上，不静默。

`aula/app/CaptionPanel.swift`：右上角悬浮双语字幕，`.nonactivatingPanel`（点它不抢焦点）、
`.canJoinAllSpaces`（切 Space 跟着走）、`.hudWindow` 毛玻璃、保留 3 句、最新在上。
从 `ClassStore` 的 Combine 流被动渲染——原菜单栏版自己维护 lines，这里改成单一数据源，
免得字幕窗和主界面显示不一致。听课时自动浮出，停止后保留让人回看最后几句。

两个自检入口：`--probe-hotkey`（注册→报告→注销，验证键位可用）、`--render-caption <png>`
（字幕窗渲染成图，改完布局先看图再起 app）。


### 修复：推理模型的思考 token 吃掉译文预算（表现为译文空白）

接 DeepSeek 官方 API 时暴露：9 句课堂西语有 6 句译成空字符串，课堂上就是「字幕突然
少一句」。根因是 `max_tokens` 把 `reasoning_token` 也算在里面，而预算按句子字符数给
（69 字符 → 169），实测单句思考量却在 69–207 之间波动、与句长无关——思考吃光预算，
`finish_reason=length`，content 溢出为空。

修法：`client.reasoning_effort` 非空时预算加 400 余量（上限 1200）。**影响所有推理模型**，
不只是 DeepSeek——wbmanager 网关的 GLM-5 / DeepSeek-V4 是同一类。修复后同一组 9 句
空译文 0 条，平均 2.35s/句。

顺手补上 `Translator` 失败时的日志：原来 `except LLMError: return ""` 静默丢句子，
排查时完全看不出原因。现在会记 `翻译失败（本句留空）：<原因>`。

新增 `tests/test_translate_budget.py`（3 项）守两件事：推理模型有余量、非推理模型
不被顺带放大。

### 新增：DeepSeek 官方 API 预设

`aula --provider deepseek` 直连 `api.deepseek.com`，预设走 `deepseek-flash`
（V4.1-Flash，1M 上下文，`low` 思考档保实时性）。实测 low 档 2.35s/句、术语质量
扎实（`diferencial → 剩余电流保护装置`、`caída de tensión → 电压降`）；`v4-pro`
3.94s/句，不建议做逐句翻译。数据与踩坑见 docs/providers.md 第八节。

### 新增：`build_app.sh --sync-code`——只同步后端源码

app 跑的是 bundle 里的 Python 副本，改完仓库源码不重新构建就等于没改（为这个白排查过
一次「译文为什么还是空」）。现在 `bash scripts/build_app.sh --sync-code` 只更新源码并
重签，秒级完成，不用等整套构建。


### 新增：听课记录导出——上课到笔记的闭环

下课后 footer 出现「生成听课记录」，点开是一个 sheet：生成中给进度，成功后显示带 YAML
frontmatter 的 Markdown，能一键复制或保存成 `.md`（直接扔进 Obsidian 就能用）。走服务端
`POST /api/sessions/{id}/digest`——分块抽取再全局合成，和「课上总结」的区别是这是课后
正式产出，篇幅和结构都更完整。失败时明确说「转写还在库里，修好网关再点一次」，不假装成功。

`--probe-class <pcm目录> digest` 把这一步也纳入自动验证，实测 3 句转写 → 827–958 字符
记录，标题、摘要、核心要点、术语表齐全。

**踩到一个工具链硬约束**：`@State` 是 SwiftUI 宏，只有 Command Line Tools（没有完整
Xcode）时直接编译失败——`plugin for module 'SwiftUIMacros' not found`。改用 `@Published`
承载 sheet 开关，它是 propertyWrapper、不需要宏插件。`@ObservedObject` / `@Published`
都能用，只有 `@State` / `@StateObject` / `@Bindable` 这类宏不行。


### 新增：系统声音采集接入原生 app——网课不用再装 BlackHole

`aula/app/SystemAudio.swift`：进程内 ScreenCaptureKit 抓系统混音，参数从 CLI 助手原样
搬来（16kHz 单声道、压掉视频流只为拿音频、关掉回声消除/降噪/AGC）。上课界面加了
「麦克风 / 系统声音」二选一——网课声音从电脑播出来，麦克风根本收不到，这条选择必须
在界面上；听课时锁定不可切，换源要重建流，半路切会让前后句子归到不同声源上。

**为什么必须进程内**：CLI 助手是服务端 `swiftc` 现场编译后拉起的临时二进制，屏幕录制
权限会挂到那个临时路径上，重编译一次路径就变、权限白批（"批过还是黑屏"）。放进 app
后权限记在 `com.chzy.aula` 上，配合自签证书的稳定 DR，一次授权永久有效。

实测：终端授权下抓 8 秒真实音频（TTS 播放）→ 399 块 / 127680 样本 / peak 19149，
格式 `lpcm Float32 non-interleaved @16kHz`。**注意：在受限沙箱里跑同一条命令会拿到全零
数据**（流在、格式对、峰值 0）——排查时先确认是不是环境限制，别急着改代码。


### 新增：原生上课界面——逐句双语，草稿原位转正

`aula/app/ClassView.swift` + `ClassStore.swift` + `Realtime.swift` + `Vad.swift`：SwiftUI
上课界面接上真实链路。顶部标题/计时/开始停止，主体逐句双语（原文小字、译文大字），
草稿行用浅色斜体原位覆盖（每 0.9 秒刷新，不追加半句），定稿转正，精译到了闪一个绿点。
菜单栏加「上课界面 ⌘1」，首启直接打开它——双击后该看到的是这个，不是服务状态窗。

`Vad.swift` 从 `menubar/main.swift` 抽成共享切句器，参数与浏览器 AudioWorklet
**逐行同口径**（三端切出的句子边界必须一致，否则同一段录音在不同入口得到不同分段）。
`Realtime.swift` 封装 `/ws/live`：12 字节帧上行、8 种下行 JSON、事件回主线程派发。

两个自检入口替代人肉点按钮：`--probe-class <pcm目录>` 无 UI 跑通整条链路
（起服务→建课堂→连 WS→发真实西语 PCM→断言三层消息），`--render-preview <png>`
把界面用视图缓存渲染成图（不需要屏幕录制权限），改完 UI 先看图再起 app。

实测（3 段西语 TTS，共 11 秒音频）：sidecar 804ms 就绪，草稿 3 条 / 定稿 3 条 /
精译 3 条，识别与术语翻译均正确（`consignar → 挂牌上锁`）。长列表 3000 条（45 分钟
课量级）注入+布局 60ms，几乎不随条数增长。另加 `--probe-permissions` 报权限与运行
环境——它会提醒：从终端跑得到的授权属于终端，TCC 认的是负责进程。

**验收口径按服务端真实语义定**，不按直觉：草稿是尽力而为（`server.py` 明写
「定稿优先、草稿让路」），所以不要求条数；精译只在译法变化时才推（相同时不推是
为了省一次回填闪烁），因此 `translation` 条数 ≤ `transcript` 是正常现象。第一版自检
按「三种消息各 ≥3 条」断言，误判成失败——错的是断言不是代码。


### 新增：原生应用地基——内嵌 Python 运行时 + sidecar 生命周期

B 路线（Swift 原生 UI + Python sidecar）拍板后的 P0 第一步，只做地基不做界面：
`aula/app/Sidecar.swift` 负责端口协商（8780 起试到 8840，真 bind 而非查端口表）、
拉起内嵌 Python、轮询 `/api/health`、崩溃自动重启（3 次退避）、退出时 SIGTERM→SIGKILL
清理；`aula/app/main.swift` 是菜单栏壳 + 服务状态窗，另带 `--probe` 命令行自检模式
（无 GUI，可进 CI）。

`scripts/build_app.sh` 一键构建：pbs（python-build-standalone）运行时 + 依赖 +
Swift 壳 → 263MB 的 `.app`。两个关键取舍：**不用 venv**（pyvenv.cfg 记录构建机绝对
路径，搬进 bundle 就失效；pbs 本身 relocatable，依赖装进它自己的 prefix 即可）、
**不用 PyInstaller**（ctranslate2 的 C++ 扩展常被漏收，onedir + 逐二进制签名更稳）。

实测：双击到 `/api/health` 通 818ms（口径是"服务可用"，whisper 懒加载，首句转写另加
约 0.9s）；退出后无残留进程。另加 `scripts/make_sign_cert.sh` 生成自签证书——ad-hoc
签名没有显式 designated requirement，TCC 按 CDHash 认人，每次重编译都丢屏幕录制权限；
自签给出稳定的 `identifier + certificate leaf`。证书「设为可信」会调 SecurityAgent
弹授权框，必须在图形会话里手动点一次，脚本会检测并提示。

顺手修几个只有真打包才暴露的坑：`FileHandle(forWritingTo:)` **不创建文件**，之前用
`try?` 兜着，日志其实一直在静默丢弃；LaunchServices 缓存旧 bundle 的可执行文件名，
磁盘上明明是 `aula`，`open` 起来的却是上一次的 `aula-menubar`（构建脚本已加
`lsregister -f`）；bash 里全角标点紧跟变量名会被当成变量名的一部分，`set -u` 下直接
报 unbound variable；app 不处理 SIGTERM 导致 `pkill` 留下孤儿 sidecar，且退出路径调用
异步 `stop()` 只是"碰巧"跑完——现在改为 `stopAndWait()` 同步等到进程真退出，日志能看到
`Shutting down → shutdown complete → stopped` 的完整闭环。

可行性依据与实测数据见 `docs/b-route-validation.md`，探针脚本在 `scripts/probes/`。


### 新增：流式字幕——边说边出字，不等一句话说完

YouTube 式实时字幕：说话过程中每 0.9 秒发一次草稿音频（只带最近 8 秒尾巴），
服务端立即转写并随带 google_free 粗译广播 `partial` 消息；说完后定稿（带完整
上下文）升级替换，精译再覆盖。草稿不入库。WS 二进制帧头扩到 12 字节
（start/end/flags，旧 8 字节帧兼容）；本地 whisper 模型加并发锁（草稿与定稿
串行推理）。网页端草稿浅色斜体 + 呼吸省略号；菜单栏字幕窗草稿行原位转正。

顺手修一个严重 bug：`resolved_asr_model()` 用原始 provider（auto）查默认模型
表，auto 查不到落 "whisper-1"，本地 whisper 会拿它当 HuggingFace 模型名去
下载不存在的仓库——首次转写永久挂起。改用解析后的 provider 查表。


### 新增：翻译接力——粗译先到，精译覆盖

实测网关 LLM 精译 2.5 秒/句，google_free 粗译 0.2 秒/句（差 10 倍）。实时课堂里
"0.2 秒看懂大意"远比"干等 2.5 秒"重要。现在原文一出，google_free 粗译随原文
一起推送；LLM 精译到了再覆盖，前端闪一下提示"这句刚升级"，笔记插入取精译。
`fast_translation` 配置项可关（默认开）。精译的 max_tokens 改为按句长动态给，
短句省约 0.3 秒。字幕窗同步：精译直接覆盖粗译。


## [0.7.1] - 2026-10-22

### 新增：Mac 菜单栏应用 aula.app

- `aula/menubar/main.swift`（AppKit，单文件）+ `scripts/build_menubar.sh`（编译 → 组装 bundle → ad-hoc 签名 → 装到 ~/Applications）
- 菜单栏常驻：开始/结束听课、听课时长计时、显示/隐藏悬浮字幕、打开工作台、服务状态与自动拉起（健康检查失败时 spawn `python -m aula serve`）
- **悬浮字幕窗**：屏幕右下角毛玻璃浮层，最新一句译文大字 + 原文小字，保留最近 3 句——上课时叠在任何窗口上都看得见，不必切回浏览器
- **权限归 app 自身**：内嵌同款 ScreenCaptureKit 捕获 + VAD，bundle id 固定（com.chzy.aula.menubar），「屏幕录制」批一次永久有效——比 CLI 助手挂在终端上干净
- LSUIElement 无 Dock 图标；转写与翻译仍由 aula 服务端完成，应用只是遥控器 + 显示器


## [0.7.0] - 2026-10-22

### 新增：系统声音·本机助手（macOS 网课的正解）

逆向了 AirTranslate（见 `docs/reverse-engineering-airtranslate.md`，静态取证：ScreenCaptureKit 捕获 + Apple Speech 识别 + Translation 翻译），把同款方案搬进 aula：

- **`aula/helpers/main.swift`**：单文件 Swift 助手（约 470 行，零第三方依赖）。ScreenCaptureKit 抓系统播放的音频 → 采样率/声道以实际 ASBD 为准（16k 单声道直通，不符则线性重采样兜底）→ 与浏览器 worklet **逐行同口径**的 VAD 切句（自适应噪声底 0.97/0.03、阈值 3x/2.5x、硬下限 0.010/0.008、尾部裁剪留 100ms）→ 按 aula 的 WS 二进制帧协议送回
- **服务端拉起**：`/api/capture/start|stop|status`。助手二进制不存在时**当场编译**（需 Xcode Command Line Tools，`swiftc -O -swift-version 5`）；源码以 main.swift 名义编译（顶层 await 的要求）。日志落在 `~/.aula/logs/capture-<session>.log`，助手异常退出时 status 接口带日志尾部，前端 toast 说人话
- **会话广播**：`/ws/live` 从"发给发送者"改为按 session 广播——助手送音频、浏览器看结果，两个标签页也能同看一节课。掉线连接在广播处摘除，不连累其他

### 修复

- 助手首版崩在 `dispatchMain()`：顶层 async 代码跑在主队列上，libdispatch 直接 trap（BUG IN CLIENT OF LIBDISPATCH）——换成信号量常驻。这是逆向文档之外的意外收获：崩一次才学到的平台规则

### 真实验证（端到端）

- 系统播放西语 TTS 音频 → ScreenCaptureKit 实测交付 **16000 Hz × 1 声道**（配置被尊重，无需重采样）→ VAD 切句 → 本地转写 → 网关翻译，3 句全部入库带译文
- 广播实测：两个 WS 客户端同连一节课，浏览器侧完整收到 transcript + translation
- 228 项测试通过（新增广播 ×2、捕获端点 ×4、术语回收与残句上下文沿用）

### 已知边界

- 助手由服务端拉起，**屏幕录制权限挂在宿主进程**（终端/启动器）上，首次要批一次
- TestClient 给每个 WS 连接建独立 portal（独立事件循环），跨连接广播只能在真服务上测——测试基建的边界，已在测试注释里说明

## [0.6.0] - 2026-10-22

### 新增

- **`examples/youtube_check.py`：用真实讲座音频测试**。`--search` 先列候选，`--start/--seconds` 取指定区间，按与浏览器一致的静音口径切句，逐段跑「本地转写 → 翻译」并给出延迟统计与双语对照 markdown。只下音频，音频在内存里处理
- **原文先推、译文后补**（WebSocket 协议新增 `translation` 消息）：ASR 一完成就先落库并推送，用户约 1 秒就能看到"听到了什么"，译文随后回填。译文位置先显示一条呼吸微光，"正在翻"这件事看得见
- **滚动术语表**：提示词本来就要求"首次出现的术语在译文后附原文"，现在直接从译文里回收「外文 → 中文」术语对，**零额外请求**地攒进 glossary，后续句子的系统提示里带"固定译名（必须遵守）"

### 修复

- **残句单独翻译会崩**（真实 YouTube 讲座实测）：静音落在句子中间（换气、犹豫），断句把一句话切成两半，`de nuestro diferencial.` 这样的残句脱离语境被译成"我们的差值"。现在识别残句（强延续词开头 / 功能词结尾 / 弱词开头且无句末标点，三档信号）并带上上一句作参考，译文修正为"我们的**差动开关**（diferencial）的功能"、"保护装置上的**电压**"
- 同一术语一节课里前后译法不一致（实测 `consignación` 出现"挂牌上锁"与"安全隔离挂牌"两种）；术语表锁定后一致
- 翻译失败不再连累原文：译文崩了只记一条日志，原文已经推出去了

### 权衡（写下来免得以后忘了）

- 术语表锁定的是**一致**，不保证**正确**：首次译错会被整节课沿用。之所以仍要锁——同一术语两种译法比偶尔一个错译更难读；要改，讲义页的「术语词库」是更好的来源（带释义、可人工修正）
- 术语回收刻意保守：只在「中文词组紧邻括号 + 括号里是纯外文」时才学。解释性括号（`（diferencial：价差/差额）`）与长描述串（`按照规程测量电源电压（tensión…）`）都会跳过——宁可少学，也不要把残缺译名当"固定译名"灌给后续句子

### 真实验证（YouTube 真实音频）

- 西班牙语技术讲座 90 秒：本地转写 0.98 秒（4.2 倍实时），ASR 准确识别 polímetro / diferencial / neutro / tensión de alimentación
- WebSocket 端到端：说完话 3.4 秒 → **1.7 秒看到原文** → 4.3 秒看到译文
- 术语一致性：`consignación` 首次学到译名后，后续句子按"固定译名"执行

## [0.5.0] - 2026-10-22

### 新增

- **WorkBuddy Manager 本地网关预设**（`--provider wbmanager`）：复用本机账号池的 OpenAI 兼容接口（默认 `http://127.0.0.1:7864/v1`），密钥在面板「密钥」页创建；预设带 `cn:glm-5.3-flash` / `cn:deepseek-v4.1-flash` 两个模型
- **思考档 `llm_reasoning_effort`**（low / high / max）：推理模型（GLM-5 / DeepSeek-V4 这类 `only_reasoning`）默认档会为每句翻译先烧几百个思考 token，实时场景等不起。预设默认 low，可在设置里改
- `llm.client_for(settings, ...)` 单点构造客户端：调用点有十几处，逐个传参会漏——漏一处的症状是"同一个配置，大纲走 low 档、翻译走 high 档"
- 设置面板新增「思考档」下拉；状态栏新增实时连接指示灯（已连接 / 断开重连中）

### 修复

- **JSON 解析只试第一个 `{`**：推理模型偶尔把带花括号的推敲混进正文，第一个花括号属于推敲而不是答案。改为按顺序尝试文本里每一个完整对象
- **截断的 JSON 直接报废**：输出到一半被切断时整块内容丢失。改为先尝试补上未闭合的字符串与括号
- 解析失败时带一句纠偏重试一次（实测本地网关约 1/20 概率需要），并在报错里带上首次返回的现场
- `providers.apply()` 会清空思考档：从推理模型切回 Gemini 还带着那个参数，接口会直接 400

### 界面：玻璃拟态全面改版

- 两套工作区（上课 / 讲义）共用一套设计系统：深色极光背景 + 毛玻璃层级 + 柔和阴影 + 圆角
- 生产级玻璃三件套：`saturate(170%)` 防模糊发灰、SVG `feTurbulence` 4% 噪点消色带、`@supports` 不支持 `backdrop-filter` 时回退到不透明填充
- 尊重系统偏好：`prefers-reduced-transparency` 退化为实色面，`prefers-reduced-motion` 关闭全部动效与极光
- 动效只动 `transform`/`opacity`（GPU 合成），不动画 `backdrop-filter`（每帧重算背景，代价极高）
- 微交互：录音中按钮脉冲圈、状态徽标呼吸点、面板错峰入场、转写段落滑入、页签/模式滑动药丸、按钮悬停抬升与按压缩放
- 布局重排：顶栏分组更清晰、面板圆角与间距统一、状态栏成为独立玻璃条；讲义页舞台做成凹陷底，白纸浮在上面

### 真实验证（2026-10-22，本地网关）

- 30 句西语课堂转写（MF1180_3 电气装置施工组织，含口语填充词）走完整链路：逐句翻译约 2.2 秒/句且术语自动注原文
- 术语词库抽出 10 条、`cociente incremental → 增量比（差商）`、`consignación → 验电挂接地` 均准确
- 连续追问两轮上下文连贯；被问"提前多久备料"时明确回答"讲义里没有写具体天数，讲义没给，我不能编"——**幻觉防护按设计工作**
- 听课记录：8 个核心要点 + 15 条术语 + 强调点 + 作业 + 待查问题，结构完整
- AI 大纲：4 个主题的原文锚点全部在原文命中，页码与难度分正确

## [0.4.1] - 2026-10-22

### 新增

- **LLM 限流冷却**（`LLMClient.cooldown_seconds`，默认 60 秒）：429/503 重试耗尽后进入冷却，期间直接快速失败，不再每句都去撞一次限流——课堂上每句话都白等一个来回是不可接受的
- 独立异常 `LLMRateLimited`：调用方可以区分"接口被限流（值得降级）"和"接口配置错误（该报出来）"
- 前端降级提示：模型不可用而本页已切换免密钥翻译时弹一次提示（不每页刷）
- `docs/providers.md` 记录 6 个 Google 端点的逐个实测结果，含"网页地址不是接口"的说明

### 修复

- `google_free` 的备用路径指向已废弃主机，等于没有备用——改为同主机的 `/single` 路径（实测可用），废弃主机降为垫底
- `endpoint` 字段存的是完整 `/t` 路径，直接拼 `/single` 会生成 `/t/single` 这种错 URL——改为显式 base 并归一化
- 联调测试靠错误文本判断"限流就跳过"，冷却期的错误信息没有 429 字样会被误判成未知错误——改为按异常类型判断

### 真实验证（2026-10-22）

- 用真实的西语课转写（64 句）生成讲义 PDF，走完整链路：4 页 / 43 段全部译出，段落切分正确
- Gemini 免费层配额耗尽返回真实 429 → 重试 → 冷却 → 自动切免密钥 Google → 徽标变为「免密钥翻译」，**降级链在真实限流下按设计工作**
- 译文质量与 AirTranslate 基线相当或更干净（基线把 "30 de junio de 2015" 译成 "2002年6月30日"，aula 译法正确）
- LLM 类功能（大纲/术语/追问/错题总结/听课记录）因配额用尽未验证——待额度恢复后补测

## [0.4.0] - 2026-10-21

### 新增

- **连续追问（卡片对话）**（`aula/chat.py` + `POST /api/reader/chat`）：同一段圈选可以连续追问；历史由前端按讲义指纹保存在 localStorage，服务端无状态（不建会话表、不存聊天），只带最近 8 轮、总长 6000 字符
- **错题本**：追问后「记为错题」，按讲义位置去重；侧栏可切换掌握状态、删除、导出 Markdown
- **错题总结**（`POST /api/reader/mistakes`）：从错题记录里分析共性盲区（为什么反复错）、复习顺序（先补前者才能懂后者）、可执行的练习；记录为空或全失败时报 502
- 教学策略写进提示词：先分类学生意图再回答、先解释后追问、每次最多一个可选问题、绝不逼学生先答才讲
- 测试增至 197 项（新增 15 项：历史裁剪、字符预算、注入防护、错题规整、两个接口的失败语义）

### 修复

- **bind() 在缺失元素上挂 onclick 会静默毁掉整个讲义页**：本次新增的错题本面板因替换串没匹配上而没进 HTML，`$("mistakeSummaryBtn").onclick` 直接抛 `Cannot set properties of null`，导致 `openBuffer` 之后的初始化全断——这是"用脚本批量改文件不校验"的典型事故，已补面板并加 id 完整性检查

## [0.3.0] - 2026-10-21

### 新增

- **术语词库**（`aula/study.py` + `POST /api/reader/terms`）：逐页抽取学科术语，术语必须是原文逐字出现的外文词（禁止造词），跨页去重、附页码；侧栏收藏进词库，「遮住释义」自测，导出 Markdown 表格
- **讲师提问卡**（`POST /api/reader/lecturer-card`）：圈选 + 学生一句话 → 整理成 module / topic / current_understanding / attempt / stuck_point / lecturer_question 六字段卡片，可复制或下载；提示词带注入防护
- 测试增至 182 项（新增 16 项：术语去重、跨页合并、字段收敛、单页失败隔离、提示词防护、两个接口的缓存与失败语义）

### 修复

- 同一术语出现在两页会在词库里重复——抽取时跨页去重（保留首次出现的页码）
- 全部页面都抽取失败时返回 502 而不是空列表（空列表会被当成"没有术语"）


## [0.2.0] - 2026-10-21

### 新增

- **讲义 AI 大纲**（`aula/outline.py` + `POST /api/reader/outline`）：`display_title`（译成界面语言）与 `search_anchor`（强制原文逐字）双字段；页码取自预埋的 `[[PAGE_n]]` 标记；1–100 难度分
- 侧栏大纲面板：生成、按内容指纹缓存、点击跳原文（逐级放宽匹配，原页与译文块同时闪烁高亮）
- 长文档按字符预算分块生成、按锚点去重、按页码排序；单块失败只丢一块，全部失败明确报 502
- 圈选提问提示词加入注入防护：PDF 原文与学生输入被声明为"数据而非指令"
- 测试增至 166 项（新增 12 项大纲测试）；端到端脚本覆盖大纲

### 修复

- 大纲分块时 `[[PAGE_n]]` 标记可能和正文被切到两个块，导致该页页码丢失——标记现在与正文绑定成单元，新块开头补最近的页标记
- 模块级大纲缓存会把"全部块失败"的空结果缓存住，之后永远拿到空大纲——空结果不再缓存
- 讲义页引擎徽标读的是旧版 health 字段（`health.llm.model` / `llm.configured`）
- `--out 笔记` 之类不带扩展名的参数被当成文件——现在按扩展名判定目录
- CLI 把 PDF 当转写解析会产出上千条乱码句子——现在直接拒绝并提示走 `/reader`

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
