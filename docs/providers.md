# 内置服务商与免费额度

`aula` 内置了一批"零成本能跑起来"的服务商预设。选一个预设，接口地址、模型名、
密钥环境变量就都填好了，你只要拿到密钥。

```bash
aula providers              # 看全部（终端表格）
aula providers --id gemini  # 看某一家详情
aula providers --json       # 给脚本用
```

> **额度会变。** 下表数字是各家官方文档在 2026-10 的标注值，免费层调整很频繁
> （Google 在 2025-12 就下调过一轮）。实际能用的上限以控制台为准，每条都附了文档地址。
> 程序不会因为超限崩溃：遇到 429 只当作"这一句识别/翻译失败"，链路继续。

## 一、总表

| 预设 id | 代表模型 | 能力 | 上下文 | 免费额度 / 限速（官方标注） | 需要密钥 |
| --- | --- | --- | --- | --- | --- |
| `ollama` | `qwen3:8b` | 对话 + 转写 | 取决于模型（3B≈128K，8B≈32K） | 无限（吃本机内存/显存），完全离线 | **不需要** |
| `gemini` | `gemini-3.8-flash` | 对话 + 转写 | 约 100 万 token | 约 10–15 RPM / 250–1500 RPD（按模型与地区调整） | 需要 `GEMINI_API_KEY`，无需信用卡 |
| `groq` | `llama-3.3-70b-versatile` | 对话 + 转写 | 约 128K | 约 30 RPM / 1000 RPD / 1.2 万 TPM（按模型） | 需要 `GROQ_API_KEY`，无需信用卡 |
| `openrouter` | `deepseek/deepseek-chat-v3.1:free` | 对话 | 视模型，约 32K–128K | 免费模型约 20 RPM；每日 50 次（余额 <10 美元）或 1000 次 | 需要 `OPENROUTER_API_KEY` |
| `cerebras` | `llama3.3-70b` | 对话 | 约 32K–128K | 约 30 RPM / 100 万 token 每天 | 需要 `CEREBRAS_API_KEY` |
| `mistral` | `mistral-small-latest` | 对话 + 转写 | 约 128K | 实验层约 1 请求/秒、50 万 TPM、每月 10 亿 token | 需要 `MISTRAL_API_KEY`（手机验证） |
| `zhipu` | `glm-4-flash` | 对话 | 约 128K | Flash 系列免费；限制并发（约 5–20）与 RPM | 需要 `ZHIPU_API_KEY` |
| `siliconflow` | `Qwen/Qwen2.5-7B-Instruct` | 对话 + 转写 | 约 32K–128K | 免费模型约 10–20 RPM；部分模型需实名 | 需要 `SILICONFLOW_API_KEY` |
| `github` | `gpt-4o-mini` | 对话 | 约 128K | 按模型分级，低档约 15 RPM / 150 RPD | 需要 `GITHUB_TOKEN`（PAT 当密钥） |
| `dashscope` | `qwen-plus` | 对话 | 约 128K | 新用户各模型 100 万 token 额度（180 天） | 需要 `DASHSCOPE_API_KEY` |
| `openai` | `gpt-4o-mini` | 对话 + 转写 | 约 128K | 无免费层，按 token 计费（仅作基准参考） | 需要 `OPENAI_API_KEY` |

### 转写能力说明

只有标了"转写"的预设能把音频变成文字，其余只能做翻译与总结（纯对话）。

| 预设 | 转写实现 | 说明 |
| --- | --- | --- |
| `gemini` | Gemini 原生 `generateContent` 内联音频 | 一把钥匙同时干转写 + 翻译，最省事 |
| `groq` | `whisper-large-v3-turbo` | 速度最快，西语/英语识别质量好，免费层够用 |
| `siliconflow` | `SenseVoiceSmall` | 中文强，小语种一般 |
| `mistral` | `voxtral-mini-latest` | 支持多语种，免费层限速较严 |
| `ollama` / 本地 | `faster-whisper`（`--asr-provider whisper`） | 完全离线，M 系列芯片 `small` 约 1–2 秒/句 |

不需要任何密钥就能用的组合只有一个：**本地 Ollama + faster-whisper**。

## 二、怎么切换

优先级：**命令行参数 > 环境变量 > 配置文件 > 预设默认值**。
同一个请求里显式写了什么，预设就不会覆盖它。

### 方式一：命令行（一次性）

```bash
# 用 Gemini：一把钥匙同时解决转写和翻译
export GEMINI_API_KEY=...
aula --provider gemini --open

# 用 Groq：转写走 whisper-large-v3-turbo，翻译走 llama-3.3-70b
export GROQ_API_KEY=...
aula --provider groq --open

# 全本地：不联网、不花钱
aula --provider ollama --asr-provider whisper --asr-model small --open

# 预设 + 指定模型（显式参数优先）
aula --provider gemini --llm-model gemini-3.5-flash-lite --open
```

### 方式二：环境变量（长期生效）

```bash
export AULA_PROVIDER=gemini
export GEMINI_API_KEY=...
aula --open
```

### 方式三：界面里点

「设置」→ **服务商预设** 下拉选一家，下面的接口地址与模型会自动填好，密钥填一次即可（写进
`~/.config/aula/config.json`，权限 0600）。保存后立刻生效。下拉框下面那行小字会直接
显示该家的限速、上下文和密钥要求：

![设置面板](screenshot-settings.png)

直链：`http://127.0.0.1:8770/#settings` 可以一步打开设置面板。

### 方式四：只换一部分

预设管的是"接口 + 模型"，转写实现可以另外指定：

```bash
# 转写用本地 whisper（不上传音频），翻译用 Gemini（免费额度）
aula --provider gemini --asr-provider whisper --asr-model small --open
```

## 三、调用示例

### 1. 直接 curl 验证密钥能不能用

```bash
# Gemini（OpenAI 兼容层）
curl -s https://generativelanguage.googleapis.com/v1beta/openai/chat/completions \
  -H "Authorization: Bearer $GEMINI_API_KEY" -H "Content-Type: application/json" \
  -d '{"model":"gemini-3.8-flash","messages":[{"role":"user","content":"只回复：可用"}]}'

# Groq
curl -s https://api.groq.com/openai/v1/chat/completions \
  -H "Authorization: Bearer $GROQ_API_KEY" -H "Content-Type: application/json" \
  -d '{"model":"llama-3.3-70b-versatile","messages":[{"role":"user","content":"只回复：可用"}]}'

# OpenRouter（注意模型名带 :free）
curl -s https://openrouter.ai/api/v1/chat/completions \
  -H "Authorization: Bearer $OPENROUTER_API_KEY" -H "Content-Type: application/json" \
  -d '{"model":"deepseek/deepseek-chat-v3.1:free","messages":[{"role":"user","content":"只回复：可用"}]}'

# 本地 Ollama（无需密钥）
curl -s http://localhost:11434/v1/chat/completions -H "Content-Type: application/json" \
  -d '{"model":"qwen3:8b","messages":[{"role":"user","content":"只回复：可用"}]}'
```

`aula` 自带同样的检查：

```bash
aula doctor --provider gemini
```

### 2. Gemini 原生音频转写（`aula` 内部就是这么调的）

```bash
# 先做一个 16kHz 单声道 wav：a.wav
curl -s "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.8-flash:generateContent?key=$GEMINI_API_KEY" \
  -H "Content-Type: application/json" \
  -d "{\"contents\":[{\"parts\":[
        {\"text\":\"逐字转写这段课堂音频（语言：西班牙语）。只输出转写文本。\"},
        {\"inline_data\":{\"mime_type\":\"audio/wav\",\"data\":\"$(base64 -i a.wav)\"}}
      ]}],\"generationConfig\":{\"temperature\":0}}"
```

### 3. 在 Python 里复用

```python
from aula.llm import LLMClient

# 任何 OpenAI 兼容服务都是这一套
client = LLMClient(base_url="https://api.groq.com/openai/v1",
                   api_key="gsk_...", model="llama-3.3-70b-versatile")
print(client.chat([{"role": "user", "content": "只回复：可用"}]))

# 转写也可以单独用
import asyncio
from aula.asr import GeminiASR
asr = GeminiASR(base_url="", api_key="...", model="gemini-3.8-flash")
result = asyncio.run(asr.transcribe(pcm_bytes, 16000, "es"))
print(result.text)
```

### 4. 写进配置文件（不想每次敲参数）

```bash
cat > ~/.config/aula/config.json <<'EOF'
{ "provider": "gemini", "source_lang": "es", "target_lang": "zh" }
EOF
chmod 600 ~/.config/aula/config.json
```

密钥建议留在环境变量里，不要写进文件。

## 四、额度用完了怎么办

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| 429 / `RESOURCE_EXHAUSTED` | 撞到 RPM 或 RPD | 等一分钟再试；或 `aula --provider groq` 换一家 |
| 401 / 403 | 密钥无效或未开通 | `aula doctor --provider <id>` 先验一遍 |
| 转写很慢 | 本地 `small` 模型在 CPU 上跑 | 换 `--asr-model base`，或用云端 `gemini`/`groq` |
| 译文出现"（演示译文）" | 还在演示模式 | 确认 `aula providers` 里当前预设与 `/api/health` 一致 |

长期在教室用，我的建议：

1. **平时**：`gemini`（免费层一天几百次，转写+翻译一把钥匙，最省心）
2. **网络差/要求隐私**：`--provider ollama --asr-provider whisper`（全离线）
3. **限速撞墙时**：`groq` 转写最快，配合 `openrouter` 的 `:free` 模型做翻译做备胎

## 五、翻译接口（零配置免密钥 / Google / Azure / DeepL）

上面的"翻译"都是**大模型翻译**。另外内置了六条翻译引擎，其中三条**完全免密钥**（不注册、不绑卡）：

```bash
aula providers    # 输出末尾会列出这三个
```

| 引擎 | 免费额度 | 开通条件 | 密钥 | 实测延迟 |
| --- | --- | --- | --- | --- |
| **`google_free`** | 无官方说明，按 IP 限流 | **零配置** | **不需要** | 约 0.2–0.5 秒 |
| **`mymemory`** | 匿名约 1000 词/天（填邮箱 1 万） | **零配置** | **不需要** | 约 0.3–0.8 秒 |
| **`libre`** | 公共实例共享额度 | **零配置**（可自建实例） | **不需要** | 约 0.5–1.5 秒 |
| `llm`（默认） | 跟服务商走（Gemini 免费层等） | 配好上面任一预设 | 同服务商 | 0.6–2 秒 |
| `google` | 每月前 50 万字符免费 | 需要 GCP 项目并开通结算（绑卡，额度内不扣费） | `GOOGLE_TRANSLATE_API_KEY` | 约 0.1–0.3 秒 |
| `azure` | F0 免费层每月 200 万字符，每秒 1 次 | Azure 订阅（注册需卡验证），要填区域 | `AZURE_TRANSLATOR_KEY` + 区域 | 约 0.1–0.3 秒 |
| `deepl` | 每月 50 万字符 | DeepL API Free 注册（需卡验证） | `DEEPL_API_KEY` | 约 0.2–0.4 秒 |

前三条是**真的零配置**：不注册、不绑卡、不填密钥，装上就能译。

### 免密钥引擎实测（2026-10-21）

同一个西班牙语句子，三家免密钥接口的实际输出：

| 原文 | `google_free` | `mymemory` | `libre` |
| --- | --- | --- | --- |
| El plazo es de cinco semanas. | 截止日期是五周。 | 截止日期为五周。 | 期限为5周. |
| La prevención de riesgos laborales es fundamental y debe estar presente durante todo el proceso. | 职业风险的预防至关重要，必须贯穿于整个过程。 | 职业风险预防至关重要，必须贯穿整个过程。 | 预防职业风险至关重要,必须贯穿整个过程. |

结论：**`google_free` 明显最好**（句子更通顺、中文标点正确），MyMemory 接近但略生硬，LibreTranslate 会输出英文句号、用词更机械。日常课堂用 `google_free`，被限流时退 `mymemory`。

三家的实现细节：

| 引擎 | 实际调用的接口 | 说明 |
| --- | --- | --- |
| `google_free` | `clients5.google.com/translate_a/t?client=dict-chrome-ex` | 你浏览器里翻译网页用的就是这类接口。不需要 key，也不走官方配额 |
| `mymemory` | `api.mymemory.translated.net/get?langpair=es\|zh-CN` | 翻译记忆库，有明确日额度 |
| `libre` | `POST https://translate.disroot.org/translate` | 开源引擎的公共实例；可 `docker run libretranslate` 自建，无限量 |

**必须知道的风险**（不是免责声明，是会真实咬人的）：

1. **它们是未公开接口**，参数或地址随时可能改。`translate.googleapis.com/translate_a/single`（老接口，网上教程几乎都抄这个）在本次实测里**已被拦**，返回 "Sorry..." 页面；只有 `clients5` 这个域名通。
2. **按 IP 限流**。一节课几百句、每句一次请求，很可能中途被拒。真被拒了切 `mymemory` 或 Azure。
3. **没有 SLA、不适合商用**。教学自用没问题；产品化请用官方 `google` / `azure`。
4. **微软那边没有可用的免密钥路径**。实测 `edge.microsoft.com/translate/auth` 返回 404——这条老路子已经下线，微软只能走需要 Azure 订阅的官方接口 `azure`。

### 选哪个

| | 大模型翻译（`llm`） | 专用接口（`google`/`azure`/`deepl`） |
| --- | --- | --- |
| 术语 | 首次出现会在括号里注原文，术语更准 | 直译，可能把 "plan general" 译成"总平面图" |
| 断句容忍 | 半句话也能译通 | 半句话容易译歪 |
| 延迟 | 0.6–2 秒，高峰期可能 503 | 0.1–0.3 秒，很稳 |
| 成本 | 吃 LLM 额度（RPD 限制） | 按字符计费，免费额度大 |
| 课堂建议 | 术语多的专业课用它 | 网络差、或 LLM 被限流时切过去 |
| 零配置 | 需要配密钥或本地模型 | `google_free` 起完全免密钥 |

两者可以随时切换，切换只影响翻译，不影响转写与总结。

### 怎么切

```bash
# 命令行
export AZURE_TRANSLATOR_KEY=...
aula --translate-engine azure --mt-region westeurope --open

# 转写用 Gemini，翻译换成 DeepL
export DEEPL_API_KEY=...
aula --provider gemini --translate-engine deepl --open

# 环境变量长期生效
export AULA_TRANSLATE_ENGINE=google
export AULA_MT_API_KEY=...
```

界面里：「设置 → 翻译与总结 → 翻译引擎」，选中后那一行小字会显示该家的额度与密钥变量。
Azure 还要填区域（如 `westeurope`），其他引擎留空。

**失败会自动回退**：专用接口报错（额度用尽、密钥失效、429）时，这一句会退回大模型翻译；
大模型也失败才留空。不会因为翻译接口挂了丢转写。

### 直接 curl 验证

```bash
# Google（注意中文用 zh-CN）
curl -s "https://translation.googleapis.com/language/translate/v2?key=$GOOGLE_TRANSLATE_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"q":["El plazo es de cinco semanas."],"source":"es","target":"zh-CN","format":"text"}'

# Azure（中文用 zh-Hans，区域必须带）
curl -s "https://api.cognitive.microsofttranslator.com/translate?api-version=3.0&from=es&to=zh-Hans" \
  -H "Ocp-Apim-Subscription-Key: $AZURE_TRANSLATOR_KEY" \
  -H "Ocp-Apim-Subscription-Region: westeurope" \
  -H "Content-Type: application/json" \
  -d '[{"Text":"El plazo es de cinco semanas."}]'

# DeepL（免费版域名是 api-free）
curl -s https://api-free.deepl.com/v2/translate \
  -H "Authorization: DeepL-Auth-Key $DEEPL_API_KEY" \
  -d "text=El plazo es de cinco semanas.&source_lang=ES&target_lang=ZH-HANS"
```

### 在 Python 里直接用

```python
from aula.mt import AzureTranslatorMT

mt = AzureTranslatorMT("你的密钥", "westeurope")
print(mt.translate("El plazo es de cinco semanas.", "es", "zh"))
# 工期是五周
```

## 六、实测记录（2026-10-21）

用 macOS `say -v Eddy` 合成一段 11.8 秒的西班牙语课堂句子，转成 16kHz 单声道，直接喂给
Gemini 原生音频接口。复现命令：`python3 -m unittest tests.test_live_gemini -v`（需密钥）。

| 模型 | 转写结果 | 结论 |
| --- | --- | --- |
| `gemini-3.8-flash` | `Hola a todos. Vamos a ver el plan general de construcción de la instalación eléctrica. La prevención de riesgos laborales es fundamental.` | 与原文逐字一致，标点也对，**可直接当转写用** |
| `gemini-3.5-flash` | 同上，逐字一致 | 可用，速度更稳 |
| `gemini-3.5-transcribe` | 空字符串 | 专用转写模型的请求格式不同，当前实现不适配，**别选它** |
| `gemini-2.5-flash` | 404：`no longer available to new users` | 旧模型已对新用户下线 |

翻译实测（同一把钥匙）：

```
输入: Vamos a ver el plan general de construcción de la instalación eléctrica.
输出: 我们来看电气安装（instalación eléctrica）的总施工计划（plan general de construcción）。
```

术语括号注原文的行为符合预期。另外实测到 `gemini-3.8-flash` 在高峰时段会返回
**503 high demand**，客户端会自动重试 2 次（退避 1.5s / 3s），仍失败时该句译文留空但
转写保留——不会让整节课中断。真被限流了就换 `gemini-3.5-flash`。

## 七、新增一家预设

编辑 `aula/providers.py` 的 `PROVIDERS`，加一条 `Provider(...)` 即可，界面和
`aula providers` 会自动带上它：

```python
Provider(
    id="myservice",
    label="我的服务",
    kind="chat",
    base_url="https://api.example.com/v1",
    model="my-model",
    key_env="MY_API_KEY",
    context="约 64K",
    limits="免费层 10 RPM / 200 RPD",
    docs="https://example.com/docs",
)
```

要求：必须兼容 `POST {base_url}/chat/completions`（OpenAI 协议）。不兼容的服务需要自己写
适配层——现有的 `asr.py` 里 `GeminiASR` 就是这种"非标准协议"的例子。
