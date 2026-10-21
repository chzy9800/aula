"""命令行入口：``aula`` / ``python -m aula``。

一个可执行文件覆盖三种用法，因为它们共享同一套配置与模型连接：

    aula                     起服务（上课 + 讲义两个工作区）
    aula providers           看内置服务商与免费额度
    aula doctor              验模型接口通不通
    aula notes 转写.txt       把一份转写压成听课记录（不需要起服务）
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import __version__, config, digest, doc_render, mt as mtmod, providers
from .chunk import chunk_turns, stats
from .llm import LLMClient, LLMError
from .textclean import clean_turns
from .transcript import BilingualTurn, Document, align_monotonic, find_pairs, load


# —— 参数表 ——


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aula",
        description="课堂到笔记：实时转写 + 逐句翻译 + 讲义对照 + 听课记录。",
    )
    parser.add_argument("--version", action="version", version="aula %s" % __version__)

    # 连接与语言（服务与生成共用，放在子命令之前）
    parser.add_argument("--provider", help="服务商预设，如 gemini / groq / ollama（先看 `aula providers`）")
    parser.add_argument("--source-lang", help="课堂语言，如 es / en / fr（默认 es）")
    parser.add_argument("--target-lang", help="笔记语言，如 zh / en（默认 zh）")
    parser.add_argument("--base-url", "--llm-base-url", dest="llm_base_url", help="翻译/总结/提问的接口地址")
    parser.add_argument("--api-key", "--llm-api-key", dest="llm_api_key", help="接口密钥")
    parser.add_argument("--model", "--llm-model", dest="llm_model", help="模型名")

    # 转写
    parser.add_argument("--asr-provider", choices=["auto", "mock", "openai", "whisper", "gemini"], help="转写实现")
    parser.add_argument("--asr-base-url", help="云端转写的 base url")
    parser.add_argument("--asr-api-key", help="云端转写的密钥")
    parser.add_argument("--asr-model", help="转写模型名，如 whisper-1 / small")

    # 翻译引擎
    parser.add_argument("--translate-engine", help="翻译引擎：llm（默认）/ google_free / mymemory / libre / google / azure / deepl")
    parser.add_argument("--mt-api-key", help="专用翻译接口密钥")
    parser.add_argument("--mt-region", help="Azure 翻译区域，如 westeurope")

    # 服务
    parser.add_argument("--host", help="监听地址（默认 127.0.0.1）")
    parser.add_argument("--port", type=int, help="端口（默认 8780）")
    parser.add_argument("--data-dir", help="数据目录（默认 ~/.aula）")
    parser.add_argument("--open", action="store_true", help="启动后打开浏览器")
    parser.add_argument("--demo", action="store_true", help="演示模式：转写用预置句子，不调用模型")

    subparsers = parser.add_subparsers(dest="command")

    serve = subparsers.add_parser("serve", help="启动 Web 服务（默认命令）")
    serve.add_argument("--open", action="store_true", default=argparse.SUPPRESS, help="启动后打开浏览器")
    serve.add_argument("--host", default=argparse.SUPPRESS, help="监听地址")
    serve.add_argument("--port", type=int, default=argparse.SUPPRESS, help="端口")

    listing = subparsers.add_parser("providers", help="列出内置服务商与免费额度")
    listing.add_argument("--json", action="store_true", help="输出 JSON")
    listing.add_argument("--id", help="只看某一个预设")

    doctor = subparsers.add_parser("doctor", help="检查模型接口连通性")
    doctor.add_argument("--base-url", dest="doctor_base_url", help="临时指定接口地址")
    doctor.add_argument("--api-key", dest="doctor_api_key", help="临时指定密钥")
    doctor.add_argument("--model", dest="doctor_model", help="临时指定模型")

    preview = subparsers.add_parser("preview", help="只解析与分块，不调用模型")
    _add_digest_args(preview, with_out=False)

    notes = subparsers.add_parser("notes", help="把一份转写生成听课记录")
    _add_digest_args(notes, with_out=True)

    batch = subparsers.add_parser("batch", help="批量处理一个目录里的转写配对")
    batch.add_argument("directory", help="目录，内含 *_original.txt / *_translation.txt 或 srt")
    batch.add_argument("--out-dir", help="输出目录（默认与输入同目录）")
    batch.add_argument("--course", default="", help="课程名，写入 frontmatter")
    batch.add_argument("--chunk-budget", type=int, default=4000, help="每块字符预算（默认 4000）")
    batch.add_argument("--overlap", type=int, default=2, help="相邻块重叠句数（默认 2）")

    return parser


def _add_digest_args(parser: argparse.ArgumentParser, with_out: bool) -> None:
    parser.add_argument("input", help="转写文件；AirTranslate 的 original / translation 给其一即可")
    parser.add_argument("--original", help="显式指定原文文件")
    parser.add_argument("--translation", help="显式指定译文文件")
    parser.add_argument("--chunk-budget", type=int, default=4000, help="每块字符预算（默认 4000）")
    parser.add_argument("--overlap", type=int, default=2, help="相邻块重叠句数（默认 2）")
    if with_out:
        parser.add_argument("--out", help="输出文件或目录（默认与输入同目录）")
        parser.add_argument("--course", default="", help="课程名，写入 frontmatter")


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    return build_parser().parse_args(argv)


# —— 配置 ——


def overrides_from(args: argparse.Namespace) -> Dict[str, Any]:
    mapping = {
        "provider": getattr(args, "provider", None),
        "host": getattr(args, "host", None),
        "port": getattr(args, "port", None),
        "source_lang": getattr(args, "source_lang", None),
        "target_lang": getattr(args, "target_lang", None),
        "asr_provider": getattr(args, "asr_provider", None),
        "asr_base_url": getattr(args, "asr_base_url", None),
        "asr_api_key": getattr(args, "asr_api_key", None),
        "asr_model": getattr(args, "asr_model", None),
        "llm_base_url": getattr(args, "llm_base_url", None),
        "llm_api_key": getattr(args, "llm_api_key", None),
        "llm_model": getattr(args, "llm_model", None),
        "translate_engine": getattr(args, "translate_engine", None),
        "mt_api_key": getattr(args, "mt_api_key", None),
        "mt_region": getattr(args, "mt_region", None),
        "data_dir": getattr(args, "data_dir", None),
    }
    result = {key: value for key, value in mapping.items() if value is not None}
    if getattr(args, "demo", False):
        result["asr_provider"] = "mock"
    return result


def _explicit_fields(args: argparse.Namespace) -> set:
    """用户显式传了哪些字段——预设不该覆盖它们。"""
    names = (
        "llm_base_url", "llm_api_key", "llm_model",
        "asr_base_url", "asr_api_key", "asr_model", "asr_provider",
        "translate_engine", "mt_api_key", "mt_region",
    )
    return {name for name in names if getattr(args, name, None) is not None}


def settings_from(args: argparse.Namespace) -> config.Settings:
    settings = config.load(overrides_from(args))
    if getattr(args, "provider", None):
        settings = providers.apply(settings, _explicit_fields(args))
    return settings


def client_for(settings: config.Settings) -> LLMClient:
    return LLMClient(
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key,
        model=settings.resolved_summary_model(),
        timeout=300,
        retries=2,
    )


# —— providers / doctor ——


def cmd_providers(args: argparse.Namespace) -> int:
    if args.id:
        provider = providers.get(args.id)
        if provider is None:
            print("没有这个预设：%s（用 `aula providers` 看全部）" % args.id)
            return 1
        info = provider.summary()
        print("%s · %s" % (info["id"], info["label"]))
        print("  能力      %s" % info["kind"])
        print("  默认模型  %s" % info["model"])
        if provider.alt_models:
            print("  可选模型  %s" % " / ".join(provider.alt_models))
        print("  base_url  %s" % provider.base_url)
        print("  上下文    %s" % info["context"])
        print("  限速      %s" % info["limits"])
        print("  密钥      %s%s" % (info["key"], ("（%s）" % provider.key_env) if provider.key_env else ""))
        if provider.key_url:
            print("  申请      %s" % provider.key_url)
        if provider.free_note:
            print("  说明      %s" % provider.free_note)
        if provider.asr_mode:
            print("  转写      %s · %s" % (provider.asr_mode, provider.asr_model))
        print("  文档      %s" % provider.docs)
        return 0

    if args.json:
        import json

        print(json.dumps([p.summary() for p in providers.PROVIDERS], ensure_ascii=False, indent=2))
        return 0

    print(providers.render_table())
    print()
    print("专用 / 免密钥翻译接口（--translate-engine，与上面的 LLM 翻译并列可选）：")
    print()
    print(mtmod.render_table())
    print()
    print("切换：aula --provider <id>       详情：aula providers --id gemini")
    print("翻译：aula --provider groq --translate-engine azure")
    print("密钥从各家约定的环境变量读取，例如 GEMINI_API_KEY / GROQ_API_KEY。")
    print("额度会变，实际上限以各家控制台为准。")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    settings = settings_from(args)
    client = LLMClient(
        base_url=args.doctor_base_url or settings.llm_base_url,
        api_key=args.doctor_api_key or settings.llm_api_key,
        model=args.doctor_model or settings.llm_model,
        timeout=30,
        retries=0,
    )
    print("aula %s" % __version__)
    print("接口：%s" % client.endpoint)
    print("模型：%s" % client.model)
    preset = providers.get(settings.provider)
    if preset:
        print("预设：%s（%s）" % (preset.id, preset.limits))
    print("翻译：%s" % settings.pick_mt())
    print("转写：%s（%s）" % (settings.pick_asr_provider(), settings.resolved_asr_model()))
    try:
        reply = client.chat([{"role": "user", "content": "只回复两个字：可用"}], max_tokens=64)
    except LLMError as exc:
        print("连通性：失败 —— %s" % exc)
        return 2
    if not reply.strip():
        print("连通性：可疑 —— 接口返回空内容。检查密钥是否有效，或调大 max_tokens。")
        return 2
    print("连通性：正常，模型回复「%s」" % reply.strip()[:20])
    return 0


# —— 转写 → 听课记录 ——


def load_turns(
    path: Path,
    original: Optional[Path] = None,
    translation: Optional[Path] = None,
) -> Tuple[List[BilingualTurn], str]:
    """从任意输入构造双语序列，返回 (turns, 语言)。"""
    if original:
        source = load(original)
        target = load(translation) if translation else None
        if target is None:
            return _document_to_turns(source), source.source_lang
        aligned = align_monotonic(
            [segment.text for segment in source.segments],
            [segment.text for segment in target.segments],
        )
        turns = [
            BilingualTurn(
                source=left,
                target=right,
                start=source.segments[index].start if index < len(source.segments) else None,
            )
            for index, (left, right) in enumerate(aligned)
        ]
        return clean_turns(turns, source.source_lang), source.source_lang

    sibling = _guess_sibling(path)
    if sibling is not None:
        return load_turns(sibling[0], original=sibling[0], translation=sibling[1])

    document = load(path)
    return _document_to_turns(document), document.source_lang


def _reject_non_transcript(path: Path) -> None:
    """讲义请走 /reader：把 PDF 当转写喂进来只会得到一堆乱码句子。"""
    if path.suffix.lower() in (".pdf", ".docx", ".pptx", ".epub"):
        raise SystemExit(
            "%s 不是转写文件。听课记录只接受 txt / srt / vtt；"
            "读讲义请起服务后打开 /reader。" % path.name
        )


def _document_to_turns(document: Document) -> List[BilingualTurn]:
    turns = [BilingualTurn(source=segment.text, start=segment.start) for segment in document.segments]
    return clean_turns(turns, document.source_lang)


def _guess_sibling(path: Path) -> Optional[Tuple[Path, Path]]:
    """AirTranslate 会导出 xxx_original.txt 与 xxx_translation.txt 两个文件。"""
    name = path.name
    stem = name
    for suffix in ("_original.txt", "_translation.txt", "_original.srt", "_translation.srt"):
        if name.endswith(suffix):
            stem = name[: -len(suffix)]
            break
    else:
        return None

    for original_suffix, translation_suffix in (("_original.txt", "_translation.txt"), ("_original.srt", "_translation.srt")):
        original = path.with_name(stem + original_suffix)
        translation = path.with_name(stem + translation_suffix)
        if original.exists() and translation.exists():
            return original, translation
    return None


def cmd_preview(args: argparse.Namespace) -> int:
    path = Path(args.input).expanduser()
    if not path.exists():
        print("找不到文件：%s" % path, file=sys.stderr)
        return 1
    _reject_non_transcript(path)
    turns, language = load_turns(
        path,
        Path(args.original).expanduser() if args.original else None,
        Path(args.translation).expanduser() if args.translation else None,
    )
    if not turns:
        print("没有解析出内容，检查文件格式。", file=sys.stderr)
        return 1

    chunks = chunk_turns(turns, budget=args.chunk_budget, overlap=args.overlap)
    print("语言：%s" % language)
    print("规模：%s" % stats(turns))
    print("分块：%d 块（预算 %d 字符，重叠 %d 句）" % (len(chunks), args.chunk_budget, args.overlap))
    print()
    for index, chunk in enumerate(chunks, 1):
        print("—— 第 %d/%d 块 · %d 句 ——" % (index, len(chunks), len(chunk)))
        for turn in chunk[:2]:
            print("  %s" % turn.source[:100])
            if turn.target:
                print("  → %s" % turn.target[:100])
        if len(chunk) > 2:
            print("  …")
    return 0


def cmd_notes(args: argparse.Namespace) -> int:
    path = Path(args.input).expanduser()
    if not path.exists():
        print("找不到文件：%s" % path, file=sys.stderr)
        return 1
    _reject_non_transcript(path)

    turns, language = load_turns(
        path,
        Path(args.original).expanduser() if args.original else None,
        Path(args.translation).expanduser() if args.translation else None,
    )
    if not turns:
        print("没有解析出内容，检查文件格式。", file=sys.stderr)
        return 1

    settings = settings_from(args)
    client = client_for(settings)
    print("语言：%s" % language)
    print("规模：%s" % stats(turns))
    print("模型：%s" % client.model)

    try:
        notes = digest.build_notes(
            turns,
            client,
            chunk_budget=args.chunk_budget,
            overlap=args.overlap,
            progress=lambda message: print("  %s" % message),
        )
    except LLMError as exc:
        print("生成失败：%s" % exc, file=sys.stderr)
        print("先跑 `aula doctor` 确认模型可用。", file=sys.stderr)
        return 2

    markdown = doc_render.render_markdown(
        notes,
        date=_date_from(path),
        course=args.course,
        source=path.name,
        language=language,
        model=client.model,
    )

    target = _resolve_out(args.out, notes.title, _date_from(path), path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(markdown, encoding="utf-8")
    print()
    print("已写入：%s" % target)
    print("  要点 %d · 术语 %d · 强调 %d · 作业 %d · 待查 %d" % (
        len(notes.points), len(notes.terms), len(notes.emphasis), len(notes.assignments), len(notes.questions)
    ))
    return 0


def cmd_batch(args: argparse.Namespace) -> int:
    directory = Path(args.directory).expanduser()
    if not directory.is_dir():
        print("不是目录：%s" % directory, file=sys.stderr)
        return 1

    pairs = find_pairs(directory)
    if not pairs:
        print("目录里没有找到配对转写（*_original.txt + *_translation.txt）。", file=sys.stderr)
        return 1

    settings = settings_from(args)
    client = client_for(settings)
    out_dir = Path(args.out_dir).expanduser() if args.out_dir else None
    print("共 %d 对，模型 %s" % (len(pairs), client.model))

    failed = 0
    for index, (original, translation) in enumerate(pairs, 1):
        print()
        print("[%d/%d] %s" % (index, len(pairs), original.name))
        turns, language = load_turns(original, original=original, translation=translation)
        if not turns:
            print("  跳过：没有内容")
            failed += 1
            continue
        try:
            notes = digest.build_notes(
                turns, client, chunk_budget=args.chunk_budget, overlap=args.overlap
            )
        except LLMError as exc:
            print("  失败：%s" % exc)
            failed += 1
            continue
        markdown = doc_render.render_markdown(
            notes,
            date=_date_from(original),
            course=args.course,
            source=original.name,
            language=language,
            model=client.model,
        )
        target = (out_dir or original.parent) / doc_render.safe_filename(notes.title, _date_from(original))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(markdown, encoding="utf-8")
        print("  写入 %s" % target)

    print()
    print("完成 %d 个，失败 %d 个" % (len(pairs) - failed, failed))
    return 0 if failed == 0 else 2


def _date_from(path: Path) -> str:
    """文件名里的 2026-10-18 优先，否则今天。"""
    import re

    match = re.search(r"(20\d{2})[-_](\d{2})[-_](\d{2})", path.name)
    if match:
        return "%s-%s-%s" % match.groups()
    return datetime.now().strftime("%Y-%m-%d")


def _resolve_out(out: Optional[str], title: str, date: str, input_path: Path) -> Path:
    if not out:
        return input_path.parent / doc_render.safe_filename(title, date)
    target = Path(out).expanduser()
    # 目录的三种写法都要认：已存在、以 / 结尾、没写扩展名（`--out 笔记` 想建目录）
    if target.is_dir() or out.endswith("/") or not target.suffix:
        return target / doc_render.safe_filename(title, date)
    return target


# —— 服务 ——


def cmd_serve(args: argparse.Namespace) -> int:
    from .server import create_app

    settings = settings_from(args)
    app = create_app(settings)
    url = "http://%s:%d" % (settings.host, settings.port)

    print("aula %s" % __version__)
    print("  上课    %s/" % url)
    print("  讲义    %s/reader" % url)
    print("  转写    %s（%s）" % (settings.pick_asr_provider(), settings.resolved_asr_model()))
    engine = settings.pick_mt()
    engine_label = settings.resolved_translate_model() if engine == "llm" else "%s（专用接口）" % engine
    print("  翻译    %s → %s（%s）" % (settings.source_lang, settings.target_lang, engine_label))
    print("  数据    %s" % settings.db_path())
    preset = providers.get(settings.provider)
    if preset:
        print("  预设    %s（%s）" % (preset.id, preset.limits))
    if settings.pick_asr_provider() == "mock":
        print("  提示    当前是演示模式：转写返回预置句子；加 --asr-provider whisper 或 openai 才会真识别。")
    print()

    if getattr(args, "open", False):
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()

    import uvicorn

    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    uvicorn.run(app, host=settings.host, port=settings.port, log_level="info")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    if args.command == "providers":
        return cmd_providers(args)
    if args.command == "doctor":
        return cmd_doctor(args)
    if args.command == "preview":
        return cmd_preview(args)
    if args.command == "notes":
        return cmd_notes(args)
    if args.command == "batch":
        return cmd_batch(args)
    return cmd_serve(args)


if __name__ == "__main__":
    raise SystemExit(main())
