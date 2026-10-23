# B 路线（原生重写 UI）可行性验证

> 日期：2026-10-23　｜　基线：aula v0.7.1（228 通过 / 2 失败，失败项为 `test_live_gemini` 需真实 Gemini key）
> 前因：`macos-app-integration-plan.md` 推荐 A 路线（WKWebView 壳），**用户拍板改走 B 路线（Swift 原生重写 UI + Python sidecar）**。
> 本文件只记录实测结论与可复现命令，不含未验证的推断。

## 0. 结论速览

| 编号 | 验证项 | 结论 | 对 B 路线的意义 |
| --- | --- | --- | --- |
| V1 | PDFKit 能否替代 pdf.js 文本层与段落切分 | **✅ 可以**，同口径移植后与 pdf.js 逐字一致 | 解除 B 路线最大死结 |
| V2 | ctranslate2 + faster-whisper 能否内嵌 pbs | **✅ 可以**，真实推理 0.90s/3.36s 音频 | ASR 不必换引擎 |
| V3 | 原生 Swift 能否直连现有 WS 协议 | **✅ 可以**，三层字幕全通，服务端零改动 | 实时链路不用重写协议 |
| V4 | 自签证书能否跨重编译保 TCC 权限 | **✅ 可以**，两次 build CDHash 不同而 DR 完全一致 | 权限只需批一次 |

四项全部通过。B 路线的技术风险从"能否做成"降级为"工作量有多大"。
原方案 §4.4 的 R2（WKWebView getUserMedia 归属）、R3（pdf.js module worker）、R8（Hardened Runtime 白屏）随 A 路线一并作废。

## 1. V1 · PDFKit 替代 pdf.js 文本层

**风险等级**：原为 B 路线唯一死结。`reader.js` 的文本层 + 段落锚点 + 圈选提问建立在 pdf.js 的 `item.transform` 精确坐标上，PDFKit 若只能吐出整页纯文本，讲义工作区就得从零设计。

**实测方法**：用同一份真实西语讲义（`MF1180_UD3_Documento_de_Apoyo1.pdf`，32 页），两条链路各自解析前 5 页：

1. pdf.js 侧：Node 加载仓库自带的 `aula/static/vendor/pdf.min.mjs`，逐行复刻 `reader.js` 的 `groupBlocks()`（`/tmp/pdfprobe/pdfjs_ref.mjs`）
2. PDFKit 侧：`PDFSelection.selectionsByLine()` 取行 + `bounds(for:)` 取字号近似值，1:1 移植同样四条规则（`/tmp/pdfprobe/pdfkit_ref.swift`）

**结果**：

| 指标（前 5 页） | pdf.js | PDFKit | 差异 |
| --- | --- | --- | --- |
| 字符数 | 8866 | 8844 | −0.25% |
| 段落数 | 63 | 62 | −1 |
| 首页段落数 | 9 | 9 | 0 |
| 首页前 6 段文本 | — | — | **逐字相同** |
| 逐页段落（0–4） | 9/11/14/12/17 | 9/11/13/12/17 | 仅第 2 页差 1 |
| 重音字符保真 | — | 196 个 áéíóúñ¿¡ 全部保留 | — |
| 锚点定位 | `getTextContent` 坐标 | `findString` + `bounds(for:)` 给出精确矩形 | 可用 |

**可复现**：`reader.js` 的四条规则必须原样搬，缺一条就退化：

1. 行聚类：`|Δy| < height × 0.5`
2. 标题识别：`height > median_height × 1.25`
3. 合并条件：`gapRatio < (isHeading ? 2.05 : 1.62)` **且** 字号变化 ≤ 25%
4. 标题折行并回：相邻两块都是标题且后块 < 34 字符

只做第 1、3 条（我第一版探针）会把标题拆成两段：`LA INSTALACION ELÉCTRICA DE LA` / `VIVIENDA.`，段落数掉到 54——这正是"只判行距会把标题粘成/拆错"的老坑。

**结论**：PDFKit 的坐标系（原点左下、y 向上）与 pdf.js 相反，转成屏幕坐标后算法可原样移植。**讲义工作区可以做，但算法要重写一遍，不是重写 UI 语言那么简单。**

## 2. V2 · pbs + ctranslate2 + faster-whisper 内嵌

**风险等级**：中高。ctranslate2 是 C++ 扩展，pbs 的 CPython 若 ABI 不匹配或缺少 macOS 系统库，ASR 只能换 whisper.cpp 或 Apple Speech，整个 sidecar 体积与质量都要重算。

**前置事实**：ctranslate2 4.8.3 有官方 `cp313-macosx_11_0_arm64` 轮子（PyPI 实测确认，cp310–cp314 全覆盖）。

**实测方法**（`/tmp/aula-probe/r1_pbs.sh`）：

1. 下载 `cpython-3.13.17+20261021-aarch64-apple-darwin-install_only.tar.gz`（24MB）
2. 用 pbs 自带解释器建 venv，`pip install ctranslate2==4.8.3 faster-whisper`
3. 复用本机已缓存的 `faster-whisper-small`（461MB）跑真实西语 PCM 推理

**结果**：

| 项 | 实测值 |
| --- | --- |
| pbs 解压后 | 66MB（relocatable，`sys.prefix` 正确指向自己） |
| venv 总体积 | 217MB |
| ctranslate2 本体 | 5.3MB |
| 依赖装齐 | av 19.0.2 / onnxruntime 1.31.0 / tokenizers 0.23.3 / huggingface_hub 2.3.0 |
| import 耗时 | 2.03s（进程冷启动，含 torch 无关，可接受） |
| 模型加载 | 0.80s（int8，CPU） |
| 推理 | 3.36s 音频 → **0.90s / 0.94s** |
| 识别结果 | `La instalación eléctrica de la vivienda tiene dos tipos de corriente.` 逐字正确 |

**体积预算修正**：原方案估 app ≤400MB。实测 pbs+venv 约 283MB，其中 onnxruntime 与 av 只在 VAD/在线解码路径用到——按 §7「不换 ASR 层」的约束先保留，若压体积可裁掉约 60MB。

**结论**：ctranslate2 在 pbs 下无坑。原方案 §4.4 的 R1 与「cp313 有无轮子」两条风险同时关闭。

## 3. V3 · 原生 Swift 直连现有 WS 协议

**风险等级**：中。B 路线要自己写 WS 客户端，若协议对原生不友好（例如依赖浏览器侧的二进制类型处理），实时链路就得改服务端。

**实测方法**（`/tmp/aula-probe/wsprobe.swift`）：

1. `say -v Mónica` 生成 3 句真实西语（3.36s / 4.09s / 3.60s），转 16k mono PCM16
2. 起探针服务（`AULA_CONFIG` 指向独立配置，whisper small + `examples/fake_llm.py` 假模型，零密钥）
3. `URLSessionWebSocketTask` 连 `/ws/live?session_id=…`，每句先发草稿帧（`flags=1`，前 65%）再发定稿帧（`flags=0`）

**结果**：

| 消息 | 数量 | 关键数据 |
| --- | --- | --- |
| `ready` | 1 | 29ms 到达，`asr=whisper sr=16000`，含 vad 参数与 `last_seq` |
| `partial`（草稿） | 3 | 边说边出字，含粗译 |
| `transcript`（定稿） | 3 | ASR 871 / 890 / 886ms，西语逐字正确 |
| `translation`（精译） | 3 | 覆盖粗译，复用同一 `seq` |
| `flushed` | 1 | 队列排空确认 |

帧格式确认：`<III` 小端 12 字节头（`start_ms` / `end_ms` / `flags`）+ PCM16LE，`flags bit0` = 草稿帧且要求 ≥0.5s。
文本帧 `{"ping"}` / `{"flush"}`。

**结论**：服务端协议**一行都不用改**。B 路线在实时链路上的工作量 = 客户端重写，不涉及协议演进。
副产物：这套客户端同时也是 §5 P1「`kill -9` sidecar 后 5s 自愈」的测试脚手架。

## 4. V4 · 自签证书保 TCC 权限

**风险等级**：中。这是原方案点名"体验提升最大单点"的一项，但 architect 只给了结论没给证据。

**实测方法**（`/tmp/aula-probe/r3_tcc.sh`）：

1. LibreSSL 生成自签代码签名证书（`extendedKeyUsage=codeSigning`），导入**独立 keychain**（不碰用户 login keychain，测完删除）
2. 造最小 `.app`（`LSMinimumSystemVersion 13.0`），改内容 → 重签名 → 读 DR，重复两次
3. ad-hoc 与证书签名各跑一轮

**结果**：

| 签名方式 | build#1 CDHash | build#2 CDHash | DR |
| --- | --- | --- | --- |
| ad-hoc (`-s -`) | `ce10080a…` | `6a868701…` | **空**（无显式 DR，隐式按 CDHash 认身份） |
| 自签证书 | `7f128b4f…` | `5448ef69…` | `identifier "com.aulaprobe.tcc" and certificate leaf = H"7dce41a3…"` **两次完全一致** |

`codesign --verify --strict` 通过。

**结论**：TCC 按 csreq 认人。ad-hoc 没有显式 DR，身份随 CDHash 漂移——这就是"每次重编译丢屏幕录制权限"的准确机理。自签证书给出 `identifier + 证书主体` 的稳定 DR，重编译不换身份，**权限只批一次**。

**踩坑记录**：macOS 自带 LibreSSL 3.3.6，`openssl pkcs12 -export` 默认就是 legacy RC2/3DES 算法；套用 OpenSSL 3 的 `-legacy -keypbe PBE-SHA1-3DES` 参数会直接报参数错误。反过来，若用 brew 装的 OpenSSL 3 生成 p12，`security import` 又会因 AES-256 PBE 报 `MAC verification failed`。

## 5. B 路线的真实改动清单

验证通过后，B 路线的成本可以算清了。以现有代码为基准（Python 5.8k + JS 2.7k + Swift 1.3k 行）：

| 现有资产 | B 路线处置 | 工作量 | 风险 |
| --- | --- | --- | --- |
| `menubar/main.swift` 781 行 | 直接留用：NSStatusItem + NSPanel 毛玻璃字幕 + SCK + VAD + 自动拉服务 | 改造 | 低 |
| `helpers/main.swift` 494 行 | 留作 CLI 第二入口；app 内改进程内 `SCStream`（避免 TCC 挂临时二进制） | 中等 | 中（原方案 R4） |
| `server.py` 700 行 | 不动，加端口协商 + READY 握手 + 父进程守护 | 小 | 低 |
| `asr.py` / `translate.py` / `mt.py` / `llm.py` / `store.py` | 不动 | 0 | 低 |
| `static/app.js` 1261 行 | **重写**为 SwiftUI 课堂界面（逐句双语列表、笔记、总结、录音控制） | 大 | 中 |
| `static/reader.js` 1416 行 | **重写**为 PDFView + PDFKit（V1 已验证算法可移植），含滚动对齐、圈选提问、大纲锚点、术语词库 | **最大** | 中 |
| 玻璃拟态 CSS | `NSVisualEffectView` / `.ultraThinMaterial` + 同样的 saturate/噪点参数 | 小 | 低 |
| AudioWorklet VAD | 已有 Swift 同口径实现（`menubar/main.swift`），直接用 | 0 | 低 |
| pdf.js vendored | 删除 | — | — |
| pytest 228 项 | 服务端不动的部分全部保留；前端测试（7 项 js）作废 | — | 低 |

**工期重估（业余投入）**：

| 阶段 | A 路线（架构师估） | B 路线（本次重估） | 说明 |
| --- | --- | --- | --- |
| P0 最小可用：双击启动 + 上课工作区 + 字幕 | 5–7 人日 | **12–16 人日** | 多出 SwiftUI 课堂界面全套 |
| P1 讲义工作区 + 设置 + 自愈 | 4–6 人日 | **10–14 人日** | 多出 PDFView 渲染/对齐/圈选 |
| P2 打磨 | 8–12 人日 | 8–14 人日 | 与 A 接近，原生 UI 打磨项更多 |
| **合计** | 17–25 人日 | **30–44 人日** | ≈ 1.5–2 倍，非架构师估的 3 倍 |

比架构师给的"3 倍工期"要乐观：V1 证明 PDFKit 能承接 pdf.js 的**算法**（不是重写设计），V3 证明实时链路零协议改动，真正新增的只有"UI 表达层"。

## 6. 第二轮验证（已补 3 项）

| 项 | 状态 | 结论 |
| --- | --- | --- |
| PDFView 圈选交互 | **✅ 通过** | `page.selection(from:to:)` 拖选拿到文本与精确矩形，`PDFViewSelectionChanged` 正常派发，`extend(atEnd:)` 可扩展。见 §8.1 |
| pbs + sidecar 冷启动总时长 | **✅ 通过** | 双击到 `/api/health` 通 **805ms**（含端口协商与进程拉起）。见 §8.2 |
| sidecar 退出清理 | **✅ 通过** | `--probe` 结束后 `pgrep` 无残留。见 §8.2 |
| AVAudioEngine 麦克风在 app bundle 内的 TCC 归属 | ⏳ 需真人一次 | `--probe-permissions` 能报状态，但**从终端跑反映的是终端的授权**（TCC 认负责进程）；只有在 app 里点一次「开始听课」才能确认 bundle 授权 |
| 进程内 `SCStream` 替代 `helpers/main.swift` | **✅ 代码通路已验证**，app 内授权待你点一次 | 8 秒真实音频 → 399 块 / 127680 样本 / peak 19149，格式 `lpcm Float32 non-interleaved @16k`。`--probe-system-audio [秒数]` 可复现；**受限沙箱里会拿到全零数据**（流在、格式对、峰值 0），别误判成代码问题 |
| SwiftUI 长列表 45 分钟课程性能 | **✅ 基本通过** | 500 条注入+布局 55ms、3000 条 60ms、绘制 33ms——几乎不随条数增长，`LazyVStack` 生效。真机滚动帧率仍需一次实际听课观察 |

## 7. 对施工顺序的建议

1. **先做 sidecar 打包与守护**（不依赖 UI）：pbs + venv + 端口协商 + 健康检查 + 退出清理。这段完成后 `aula serve` 就等于跑起来了，P0 的地基先稳。
2. **再把 `wsprobe.swift` 长成真正的课堂界面**：它已验证三层字幕、ASR 延迟、flush 语义，是最小可信起点。
3. **讲义工作区放最后**：V1 只证明了文本层可移植，渲染与交互是最大工作量，先让上课链路可用再啃。
4. **每步守住 228 项 pytest**：服务端不动是 B 路线的前提，动了就要重算回归成本。

## 8. P0 地基实测（第二批）

### 8.1 PDFView 圈选

`scripts/probes/pdfview_select.swift`：起 accessory 应用 → 建 PDFView → `page.selection(from:to:)` 程序化拖选 → 读结果。

```
text: "LA INSTALACION ELÉCTRICA D"     chars: 26
bounds: x=67.2 y=784.9 w=401.7 h=26.0
view_coords: x=205.6 y=655.0 w=425.3 h=27.6
pages_in_selection: 1
selectionChanged_delta: 1
notification_text_ok: true
extended_chars: 29
```

关键点：

- API 在 **`PDFPage`** 上（`page.selection(from:to:)`），不在 `PDFView` 上——`PDFView` 只有 `currentSelection`。第一版探针按 `view.selection(from:to:type:)` 写，编译直接报 no member。
- `PDFViewSelectionChanged` 会派发，回调里能从 `currentSelection.string` 拿到文本——「圈住就问」的交互闭环成立。
- `view.convert(_:from:)` 能把页面矩形换算到视图坐标，高亮/钉回答的位置有了。
- 视图不必上屏（`orderBack` + `layoutSubtreeIfNeeded` 即可），这对后续自动化测试友好。

### 8.2 sidecar 冷启动与退出清理

`scripts/build_app.sh --probe` 完整链路（构建 pbs 运行时 → 编译壳 → 组包 → 起服务 → 健康检查 → 建课堂 → 停 → 查残留）：

| 指标 | 实测 |
| --- | --- |
| app 体积 | **263MB**（含 pbs + 全部依赖，预算 ≤400MB） |
| 双击到 `/api/health` 通 | **795–833ms**（5 次实测；含端口协商、进程 spawn、uvicorn 就绪） |
| 端口协商 | 8780 被占时自动落到 8781 |
| 建课堂 | 成功返回 session id |
| `--probe` 退出后残留进程 | **none** |
| GUI 收到 SIGTERM 后残留 | **none**（uvicorn 打印完整 shutdown 日志） |

**注意 818ms 的准确含义**：这是**服务可用**时间。whisper 模型是懒加载的，首次转写才加载（实测 0.80s）；所以「第一句话出字」的真实时间 = 818ms + 首句 ASR（约 0.9s）+ 翻译。原方案 §6 的「启动 ≤4s」指标以服务可用为准，这个口径必须写清楚，否则会把"能打开"误当"能听课"。

### 8.3 构建产出的 app 形态

`~/Applications/aula.app`：菜单栏波形图标 + 服务状态窗（状态点、端口、冷启动耗时）+ 重启服务 / 打开日志 / 退出。
无 Dock 图标（`setActivationPolicy(.accessory)`）。课堂界面与讲义工作区尚未接入，`Sidecar.baseURL` 已经是它们的接入点。

**签名现状**：自签证书已生成并导入 login keychain（`scripts/make_sign_cert.sh`），但「设为可信」这一步会调 SecurityAgent 弹授权框，无 GUI 会话下必被系统杀（SIGTERM），**必须在 Terminal.app 或钥匙串访问里手动点一次**。在此之前 `build_app.sh` 会退回 ad-hoc 并明确警告。

### 8.4 只有真打包才会暴露的四个坑

| 坑 | 现象 | 根因 | 修法 |
| --- | --- | --- | --- |
| `FileHandle(forWritingTo:)` 不创建文件 | 日志目录空着，"日志落盘"是假承诺 | API 只在文件已存在时成功，`try?` 把错误咽了 | 先 `createFile` 再打开；管道输出也走同一个 `appendLog` |
| LaunchServices 缓存旧 bundle | 磁盘上是 `aula`，`open` 起来的是上次的 `aula-menubar` | LS 数据库按路径记了旧 `CFBundleExecutable` | 构建末尾 `lsregister -f $APP_DIR` |
| 全角标点紧跟变量名 | `set -u` 下报 `APP_DIR（: unbound variable` | bash 把全角 `（` 的字节当成变量名的一部分 | 一律 `${VAR}` 定界 |
| 状态文案与状态分离 | 状态窗闪一下旧文案 | 回调挂在 `state.didSet`，先赋 state 后赋 text | 引入 `setState(_:_:)` 一次改两样；补 `.stopping` 态 |
| app 不处理 SIGTERM | `pkill aula` 后 sidecar 变孤儿，占着端口和 SQLite | AppKit 默认不接 SIGTERM | `DispatchSource.makeSignalSource` → `NSApp.terminate` |
| 退出路径调异步 stop | 日志里没有 stop 记录，"清理成功"是碰巧 | `stop()` 是 `queue.async`，主线程走到 exit 时它还没跑完 | `stopAndWait()` 轮询进程状态，等真退出再返回 |

后两条是同一类问题：**「看起来对了」和「保证对」的差别**。实测证据是日志——修之前日志停在 `healthy`，修之后能看到
`Shutting down → Application shutdown complete → Finished server process → stopped` 的完整闭环。

前两条属于「单元测试永远测不出、只有跑真构建才现形」的类型——`--probe` 这个自检入口
就是为了让它们现形而加的。GUI 路径另有一条冒烟命令：

```bash
open ~/Applications/aula.app && sleep 7 && pkill -TERM -f "aula.app/Contents/MacOS/aula$"
sleep 3 && pgrep -fl "aula serve --host 127.0.0.1"   # 应为空
```

## 9. 课堂界面实测（第三批）

P0 的第二个交付物：`ClassView` + `ClassStore` + `Realtime` + `Vad`，把上课界面接上真实链路。

### 9.1 整条链路的实测数据

`--probe-class <pcm目录>`（无 UI，可进 CI）：起服务 → 建课堂 → 连 WS → 发 3 段真实西语 TTS（共 11 秒音频）→ 断言。

| 项 | 结果 |
| --- | --- |
| sidecar 就绪 | 775ms |
| `ready` | 1 |
| `partial`（草稿） | 3 |
| `transcript`（定稿） | 3 |
| `translation`（精译覆盖） | 3，覆盖 seq 1/2/3 |
| 识别正确性 | 3 句西语逐字正确 |
| 术语翻译 | `consignar → 挂牌上锁`、`cuadro eléctrico → 配电箱`，术语附原文 |

### 9.2 一次「错误的断言」

第一版自检按「三种消息各 ≥3 条」验收，结果 `partial` 只有 1 条、`translation` 只有 2 条，判为失败。

查完 `server.py` 才明白**错的是断言**：

- **草稿是尽力而为**：`partial_worker` 里明写「定稿优先，草稿让路（马上会被定稿覆盖）」，队列非空时草稿直接跳过。所以条数不该被要求。
- **精译只在译法变化时才推**：`if translated and translated != fast_text` —— 相同时不推是为了省一次回填闪烁。所以 `translation ≤ transcript` 是正常。

改后的验收口径：**每句必须有 `transcript` 且 `target` 非空**（粗译随原文一起到，空译文才是真丢内容），草稿与精译只做统计不做断言。

顺带发现另一个真问题：段间只等 2.5s 太短（一句的处理链是 ASR 0.9s + 精译 2.5s），下一句的草稿必被上一句的定稿挤掉。放宽到 4s 后草稿 3/3 全到——**这说明真实录音时 900ms 一次的草稿节流是够的，瓶颈在服务端串行处理**。

### 9.3 界面渲染自检

`--render-preview <png> [条数]`：用 `NSView.bitmapImageRepForCachingDisplay` 把 SwiftUI 视图渲染成 PNG，**不需要屏幕录制权限**，改完 UI 先看图再起 app。

实测长列表：

| 条数 | 注入 + 布局 | 绘制 |
| --- | --- | --- |
| 500 | 55ms | 34ms |
| 3000（45 分钟课量级） | 60ms | 33ms |

几乎不随条数增长 —— `LazyVStack` 只渲染可见部分。**注意这是「不会因数据量爆炸而卡」的证明，不是滚动帧率的证明**，后者要真机听课观察。

### 9.4 自签 DR 在真实 app 上的复现

§4 的结论用临时 app 验过，这次在真产物上复现：

| | CDHash | designated requirement |
| --- | --- | --- |
| build#1 | `431aa0e8…` | `identifier "com.chzy.aula" and certificate root = H"4510ed0b…"` |
| build#2（改了代码） | `a77ac621…` | **完全相同** |

`codesign --verify --strict` 通过。注意 `security find-identity` 会把证书标成 `CSSMERR_TP_NOT_TRUSTED` —— **不影响签名，也不影响 DR 稳定性**，信任只关系到 Gatekeeper 分发校验。自用场景不必设信任。

### 9.5 构建脚本的三个坑（都跟「复制 260MB」有关）

| 做法 | 结果 |
| --- | --- |
| `pip install` 无条件执行 | 每次都要联网查索引，二次构建白等数分钟 → 改成先 `python -c "import ..."` 探测 |
| `cp -R` 复制运行时 | site-packages 上万文件，逐文件建 inode，分钟级 → 弃用 |
| `ditto` 复制 | 在受限环境里直接挂住（8 分钟无进展）→ 弃用 |
| **`cp -Rc`（APFS 写时复制克隆）** | **258MB / 0.65 秒**，且不额外占磁盘 → 采用 |

另外 `bash` 里全角标点紧跟变量名会被吞进变量名（`set -u` 下报 unbound variable），这个坑在同一份脚本里踩了两次（`$APP_DIR（`、`$SIGN_ID）`），最后写脚本扫全仓才清干净。

### 9.6 听课记录闭环

`--probe-class <pcm目录> digest` 把最后一步也纳入自动验证：

| 项 | 结果 |
| --- | --- |
| 服务端接口 | `POST /api/sessions/{id}/digest`（分块抽取 → 全局合成） |
| 产出 | 827–958 字符 Markdown，带 YAML frontmatter |
| 结构完整性 | title / date / source / language / tags + 摘要 + 核心要点 + 术语表 |
| 耗时 | 3 句转写约 2–5 秒（课量大时分块更多，界面上有进度反馈） |

界面：footer 的「生成听课记录」→ sheet 显示 → 复制 / 保存 `.md`。失败时说明「转写还在库里，
修好网关再点一次」，不假装成功。

### 9.7 一条工具链硬约束：不能用 SwiftUI 宏

只有 Command Line Tools（没装完整 Xcode）时，`@State` / `@StateObject` / `@Bindable` 这些
**宏**会直接编译失败：

```
error: external macro implementation type 'SwiftUIMacros.StateMacro' could not be found
       for macro 'State()'; plugin for module 'SwiftUIMacros' not found
```

`@ObservedObject` / `@Published` / `@Environment` 是 propertyWrapper，不受影响。
所以 sheet 的开关放在 `ClassStore` 里用 `@Published` 承载，视图只读不持有状态。

这条约束决定了整个 app 的状态管理风格：**状态留在 store，视图保持无状态**——本来也是更好的
写法，只是这里是工具链逼出来的。
