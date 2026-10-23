#!/bin/bash
# R1：验证 python-build-standalone（pbs）+ ctranslate2 + faster-whisper 能否在 app 内跑起来。
# 这是"内嵌 Python sidecar"方案的地基：如果 ctranslate2 的 C++ 扩展在 pbs 下装不上或跑不动，整个 A/B 路线都要改 ASR 引擎。
set -x
set -e
cd /tmp/aula-probe

URL="https://github.com/astral-sh/python-build-standalone/releases/download/20261021/cpython-3.13.17%2B20261021-aarch64-apple-darwin-install_only.tar.gz"
if [ ! -f pbs.tar.gz ]; then
  curl -L --noproxy '*' --max-time 300 -o pbs.tar.gz "$URL"
fi
echo "== pbs download: $(du -h pbs.tar.gz | cut -f1) =="

rm -rf pbs && mkdir -p pbs && tar -xzf pbs.tar.gz -C pbs
PBSPY=/tmp/aula-probe/pbs/python/bin/python3
echo "== pbs python =="
$PBSPY -V
$PBSPY -c "import sys; print('prefix:', sys.prefix); print('executable:', sys.executable)"

rm -rf pbsenv
$PBSPY -m venv /tmp/aula-probe/pbsenv
PIP=/tmp/aula-probe/pbsenv/bin/pip
$PIP install -q --upgrade pip 2>&1 | tail -2

echo "== 安装 ctranslate2 + faster-whisper（关键：C++ 扩展） =="
$PIP install -q ctranslate2==4.8.3 faster-whisper 2>&1 | tail -20
echo "== 已装版本 =="
$PIP list 2>/dev/null | grep -iE "ctranslate2|faster-whisper|tokenizers|av|onnxruntime|huggingface"

echo "== 体积 =="
du -sh /tmp/aula-probe/pbsenv 2>/dev/null
du -sh /tmp/aula-probe/pbsenv/lib/python3.13/site-packages/ctranslate2* 2>/dev/null
du -sh /tmp/aula-probe/pbs 2>/dev/null

echo "== 真实推理测试（16k 西语 PCM） =="
HF_HOME=/Users/cheng/.cache/huggingface /tmp/aula-probe/pbsenv/bin/python - <<'PYEOF'
import time, wave
t0 = time.time()
from faster_whisper import WhisperModel
print("import ok: %.2fs" % (time.time() - t0))

t1 = time.time()
# 复用已有本地缓存模型，避免重复下载
model = WhisperModel("small", device="cpu", compute_type="int8")
print("model load: %.2fs" % (time.time() - t1))

import numpy as np
pcm = np.frombuffer(open("/tmp/aula-probe/s1.raw","rb").read(), dtype=np.int16).astype(np.float32) / 32768.0
print("audio: %.2fs" % (len(pcm)/16000))

for i in range(2):
    t = time.time()
    segs, info = model.transcribe(pcm, language="es", beam_size=1, vad_filter=False)
    text = "".join(s.text for s in segs).strip()
    print("run%d: %.2fs -> %s" % (i+1, time.time()-t, text))
PYEOF
echo "== R1 DONE =="
