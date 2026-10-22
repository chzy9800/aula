# -*- coding: utf-8 -*-
"""用真实讲座音频跑一遍 aula 的完整链路：YouTube → 本地转写 → 翻译 → 听课记录。

和 ``live_check.py`` 的区别：那个用固定句子验功能，这个用**真人的课堂语音**
验真实条件——有背景噪声、有语气词、有长句短句混在一起、语速不均匀。

    # 先看看有哪些候选（不下���）
    python examples/youtube_check.py --search "MF1180 instalaciones eléctricas"

    # 取某个视频的前 3 分钟跑一遍（默认就是 3 分钟：45 分钟的课全译要几小时）
    python examples/youtube_check.py "https://www.youtube.com/watch?v=XXXX" --seconds 180

    # 换成你自己的课（带时间码跳过片头）
    python examples/youtube_check.py "<URL>" --start 620 --seconds 300 --digest

只用音频、只下音频；音频在内存里处理，不落盘（除非 --keep）。
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from aula import chat as chatmod  # noqa: E402
from aula import config as configmod  # noqa: E402
from aula import digest as digestmod  # noqa: E402
from aula import doc_render  # noqa: E402
from aula import llm as llmmod  # noqa: E402
from aula.transcript import BilingualTurn  # noqa: E402
from aula.translate import Translator  # noqa: E402

TARGET_RATE = 16000


def ffmpeg_exe() -> str:
    import imageio_ffmpeg

    return imageio_ffmpeg.get_ffmpeg_exe()


def search(query: str, count: int = 5) -> None:
    import yt_dlp

    opts = {"quiet": True, "skip_download": True, "no_warnings": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info("ytsearch%d:%s" % (count, query), download=False)
    print("候选视频（复制 URL 交给下一步）：")
    for entry in info.get("entries") or []:
        if not entry:
            continue
        print("  %-11s %6.1f 分  %-50s | %s" % (
            entry.get("id"), (entry.get("duration") or 0) / 60,
            (entry.get("title") or "")[:50], (entry.get("uploader") or "")[:22]))
        print("             https://www.youtube.com/watch?v=%s" % entry.get("id"))


def fetch_audio(url: str, keep_dir: str | None) -> tuple[bytes, str, str]:
    """下载音频并转成 16k 单声道 PCM。返回 (pcm, 标题, 时长秒)。"""
    import yt_dlp

    opts = {
        "quiet": True,
        "no_warnings": True,
        "format": "bestaudio/best",
        "outtmpl": str(Path(keep_dir or tempfile.mkdtemp()) / "audio.%(ext)s"),
        # 只取音频：不下载视频流，几十 MB 而非几百 MB
        "postprocessors": [],
    }
    # YouTube 偶尔要求指定客户端；web/android 都被限流时依次回退
    clients = ["default", "tv", "web_safari"]
    last = None
    for client in clients:
        try:
            opts["extractor_args"] = {"youtube": {"player_client": [client]}}
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                if not info:
                    raise RuntimeError("拿不到视频信息")
                path = Path(ydl.prepare_filename(info))
                candidates = sorted(path.parent.glob("audio.*"))
                if not candidates:
                    raise RuntimeError("下载后没找到音频文件")
                audio = candidates[0]
                title = str(info.get("title") or "")
                duration = float(info.get("duration") or 0)
            break
        except Exception as exc:  # noqa: BLE001
            last = exc
            continue
    else:
        raise RuntimeError("下载失败：%s" % str(last)[:140])

    cmd = [
        ffmpeg_exe(), "-v", "quiet", "-i", str(audio),
        "-f", "s16le", "-ac", "1", "-ar", str(TARGET_RATE), "-",
    ]
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0 or not proc.stdout:
        raise RuntimeError("音频转码失败：%s" % proc.stderr.decode("utf-8", "replace")[:140])
    return proc.stdout, title, duration


def split_by_silence(pcm: bytes, silence_ms: int = 700, min_speech_ms: int = 350,
                     max_segment_ms: int = 15000) -> list[tuple[int, int]]:
    """按静音切句，口径与浏览器里的 VAD 一致。

    为什么自己切而不用固定窗口：课堂语音有停顿，固定窗口会把两句话塞进一段，
    那样测出来的"每句延迟"不是课堂上的真实体感。
    """
    import array

    samples = array.array("h")
    samples.frombytes(pcm[: len(pcm) - (len(pcm) % 2)])
    frame = 320  # 20ms
    total_frames = len(samples) // frame
    silence_frames = max(1, silence_ms // 20)
    min_frames = max(1, min_speech_ms // 20)
    max_frames = max(4, max_segment_ms // 20)

    segments: list[tuple[int, int]] = []
    start_frame = None
    quiet = 0
    for index in range(total_frames):
        offset = index * frame
        window = samples[offset : offset + frame]
        rms = (sum(v * v for v in window) / max(1, len(window))) ** 0.5
        speaking = rms > 500
        if speaking:
            if start_frame is None:
                start_frame = index
            quiet = 0
        elif start_frame is not None:
            quiet += 1
            if quiet >= silence_frames:
                end_frame = index - quiet + 1
                if end_frame - start_frame >= min_frames:
                    segments.append((start_frame * frame, end_frame * frame))
                start_frame = None
                quiet = 0
        if start_frame is not None and index - start_frame >= max_frames:
            segments.append((start_frame * frame, (index + 1) * frame))
            start_frame = None
            quiet = 0
    if start_frame is not None and total_frames - start_frame >= min_frames:
        segments.append((start_frame * frame, total_frames * frame))
    return segments


def main() -> int:
    parser = argparse.ArgumentParser(description="用真实讲座音频测试 aula")
    parser.add_argument("url", nargs="?", help="YouTube 视频 URL")
    parser.add_argument("--search", default="", help="只搜索候选视频，不下载")
    parser.add_argument("--start", type=float, default=0, help="从第几秒开始（默认 0）")
    parser.add_argument("--seconds", type=float, default=180, help="取多少秒音频（默认 180）")
    parser.add_argument("--lang", default="", help="课堂语言，默认用配置里的")
    parser.add_argument("--digest", action="store_true", help="跑完再生成一份听课记录")
    parser.add_argument("--out", default="", help="结果写到这个 markdown 文件")
    parser.add_argument("--keep", default="", help="保留下载的音频到这个目录")
    args = parser.parse_args()

    if args.search:
        search(args.search)
        return 0
    if not args.url:
        parser.error("给一个 YouTube URL，或者用 --search 先找候选")

    settings = configmod.load()
    client = llmmod.client_for(settings, timeout=180)
    translator = Translator(client, settings.source_lang, settings.target_lang)
    lang = args.lang or settings.source_lang

    print("下载音频（只要音频，不要视频）…")
    started = time.time()
    pcm, title, duration = fetch_audio(args.url, args.keep or None)
    print("  《%s》 音频 %.1f 分钟，下载+转码 %.1fs" % (title[:50], len(pcm) / 2 / TARGET_RATE / 60, time.time() - started))

    begin = int(args.start * TARGET_RATE) * 2
    length = int(args.seconds * TARGET_RATE) * 2
    clip = pcm[begin : begin + length]
    if not clip:
        print("截取范围内没有音频，检查 --start/--seconds")
        return 1
    clip_seconds = len(clip) / 2 / TARGET_RATE
    print("截取 %.0f–%.0f 秒（%.1f 秒音频）" % (args.start, args.start + args.seconds, clip_seconds))

    segments = split_by_silence(clip)
    print("按静音切成 %d 段（口径与课堂实时一致）\n" % len(segments))

    from aula.asr import build_asr

    asr = build_asr(settings)
    print("转写：%s" % getattr(asr, "name", "?"))
    print("翻译：%s（%s）\n" % (settings.pick_mt(), settings.resolved_translate_model()))

    rows = []
    asr_total = mt_total = 0.0
    audio_total = 0.0
    for index, (start, end) in enumerate(segments, 1):
        chunk = clip[start:end]
        seconds = (end - start) / 2 / TARGET_RATE
        t0 = time.time()
        text = getattr(asr, "_transcribe_sync", None)
        result = asr._transcribe_sync(chunk, TARGET_RATE, lang) if text else None
        if result is None:
            import asyncio

            result = asyncio.get_event_loop().run_until_complete(
                asr.transcribe(chunk, TARGET_RATE, lang)
            )
        t1 = time.time()
        translated = translator.translate(result.text)
        t2 = time.time()
        asr_total += t1 - t0
        mt_total += t2 - t1
        audio_total += seconds
        rows.append((seconds, t1 - t0, t2 - t1, result.text, translated))
        print("[%2d] 音频 %.1fs ｜ 转写 %.2fs ｜ 翻译 %.2fs" % (index, seconds, t1 - t0, t2 - t1))
        print("     西：%s" % result.text[:96])
        print("     中：%s" % translated[:96])
        sys.stdout.flush()

    print("\n" + "=" * 70)
    if not rows:
        print("没有切出任何语音段——这段可能没有人声，换 --start 试试")
        return 1
    n = len(rows)
    print("共 %d 段 ｜ 音频 %.1f 秒" % (n, audio_total))
    print("转写 平均 %.2f 秒（%.1f 倍实时）" % (asr_total / n, audio_total / max(asr_total, 0.001)))
    print("翻译 平均 %.2f 秒" % (mt_total / n))
    print("端到端 平均 %.2f 秒/句" % ((asr_total + mt_total) / n))
    print("=" * 70)

    out_path = args.out or "/tmp/aula_youtube_check.md"
    lines = ["# %s" % title, "",
             "> YouTube 真实音频测试：%d 段 / %.0f 秒" % (n, audio_total), ""]
    for seconds, asr_s, mt_s, source, target in rows:
        lines.append("- `%5.1fs` %s" % (seconds, source))
        lines.append("  <br>%s" % target)
        lines.append("")
    if args.digest:
        print("\n生成听课记录（分块抽取 + 全局合成，要一两分钟）…")
        turns = [
            BilingualTurn(source=source, target=target, start=index * 5.0)
            for index, (_, _, _, source, target) in enumerate(rows)
        ]
        notes = digestmod.build_notes(turns, client)
        lines.append("\n---\n")
        lines.append(doc_render.render_markdown(notes, model=client.model, source=args.url))
    Path(out_path).write_text("\n".join(lines), encoding="utf-8")
    print("结果已写入 %s" % out_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())