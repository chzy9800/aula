# 可行性探针

B 路线（Swift 原生 UI + Python sidecar）拍板前的一次性验证脚本。结论见 **[docs/b-route-validation.md](../../docs/b-route-validation.md)**。
留在这里是为了「结论可复现」——不是产品代码，不进构建。

| 脚本 | 验证什么 | 跑法 |
| --- | --- | --- |
| `pdfkit_ref.swift` | PDFKit 按 `reader.js` 同口径切段落，能否追平 pdf.js | `swiftc -O -o /tmp/pdfkit_ref pdfkit_ref.swift && /tmp/pdfkit_ref <讲义.pdf> 5` |
| `pdfjs_ref.mjs` | 同上，pdf.js 侧基准（用仓库 vendored 的 pdf.min.mjs） | `node pdfjs_ref.mjs <讲义.pdf> 5` |
| `pdfview_select.swift` | PDFView 程序化拖选：能否拿到文本 / 矩形 / selectionChanged | `swiftc -O -o /tmp/pdfview_select pdfview_select.swift && /tmp/pdfview_select <讲义.pdf>` |
| `wsprobe.swift` | 原生 `URLSessionWebSocketTask` 直连 `/ws/live`，三层字幕是否可驱动 | `swiftc -O -o /tmp/wsprobe wsprobe.swift && /tmp/wsprobe <session_id> <pcm_dir> 8780` |
| `r1_pbs.sh` | pbs CPython + ctranslate2 + faster-whisper 能否内嵌并真实推理 | `bash r1_pbs.sh`（下 24MB pbs，建 venv） |
| `r3_tcc.sh` | 自签证书的 designated requirement 是否跨重编译稳定 | `bash r3_tcc.sh`（用独立 keychain，测完自动删） |

## 复用注意

- `wsprobe.swift` 需要一份可用的服务端：`AULA_CONFIG` 指向独立配置即可，配合 `examples/fake_llm.py` 零密钥跑通。
- 音频片段目录需含 `s1.raw` / `s2.raw` / `s3.raw`——16k、单声道、PCM16LE 裸数据。造法：

  ```bash
  say -v Mónica --data-format=LEI16@16000 --file-format=WAVE -o s1.wav "句子"
  # 去掉 44 字节 WAV 头即得裸 PCM
  ```

- `r1_pbs.sh` 复用 `~/.cache/huggingface` 里已有的 faster-whisper 模型，首次跑前需存在 `Systran/faster-whisper-small`。
- `r3_tcc.sh` **不会**改用户的 keychain 搜索列表，证书装在 `tcc/test.keychain-db`，结束即删。
