from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import web_tool


def emit_json(data: dict[str, Any]) -> None:
    print(json.dumps(data, ensure_ascii=False), flush=True)


def make_progress(jsonl: bool):
    def progress(message: str, event_type: str = "progress", data: dict[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {"type": event_type, "message": message}
        if data is not None:
            payload["data"] = data
        if jsonl:
            emit_json(payload)
        else:
            print(f"[{event_type}] {message}", file=sys.stderr, flush=True)

    return progress


def payload_from_args(args: argparse.Namespace) -> dict[str, Any]:
    industry_prompt = ""
    if getattr(args, "industry_prompt_file", None):
        industry_prompt = Path(args.industry_prompt_file).read_text(encoding="utf-8")
    enhance_with_prompt = bool(getattr(args, "enhance_with_prompt", False) or industry_prompt or getattr(args, "industry_prompt_id", ""))
    return {
        "mode": "cpa" if getattr(args, "use_cpa", False) else "local",
        "terms": getattr(args, "terms", "") or "",
        "matchMode": getattr(args, "match_mode", "fuzzy") or "fuzzy",
        "apiKey": getattr(args, "api_key", "") or "",
        "baseUrl": getattr(args, "base_url", web_tool.DEFAULT_BASE_URL) or web_tool.DEFAULT_BASE_URL,
        "model": getattr(args, "model", web_tool.DEFAULT_MODEL) or web_tool.DEFAULT_MODEL,
        "enhanceWithPrompt": enhance_with_prompt,
        "industryPromptId": getattr(args, "industry_prompt_id", "") or "",
        "industryPrompt": industry_prompt,
        "minConfidence": getattr(args, "min_confidence", 0.45),
        "padding": getattr(args, "padding", 8),
        "blockSize": getattr(args, "block_size", 12),
        "imageInstruction": getattr(args, "instruction", "") or "",
    }


def summarize_result(result: dict[str, Any], job_dir: Path, full: bool = False) -> dict[str, Any]:
    if full:
        return result
    output_file = result.get("outputFile") or ""
    images = (result.get("reportData") or {}).get("images") or []
    return {
        "ok": result.get("ok", False),
        "jobId": result.get("jobId"),
        "source": result.get("source"),
        "outputFile": output_file,
        "outputPath": str((job_dir / "output" / output_file).resolve()) if output_file else "",
        "reportPath": str((job_dir / "output" / "report.md").resolve()),
        "reportJsonPath": str((job_dir / "output" / "report.json").resolve()),
        "zipPath": str((job_dir / "output" / "csdn_safe_package.zip").resolve()),
        "imageCount": len(images),
        "maskedRegions": sum(int(item.get("maskedRegions") or 0) for item in images),
        "images": [
            {
                "index": item.get("index"),
                "status": item.get("status"),
                "maskedRegions": item.get("maskedRegions"),
                "originalRef": item.get("originalRef"),
                "sourceImageRef": item.get("sourceImageRef"),
                "outputRef": item.get("outputRef"),
                "lastRerunSource": item.get("lastRerunSource"),
                "matches": [match.get("text") for match in item.get("matches") or []],
            }
            for item in images
        ],
        "notices": result.get("notices") or [],
    }


def cmd_health(_: argparse.Namespace) -> int:
    lark_cli = web_tool.resolve_lark_cli()
    version = ""
    if lark_cli:
        try:
            completed = web_tool.run_lark_cli(["--version"], timeout=20)
            version = (completed.stdout or completed.stderr or "").strip()
        except Exception:
            version = ""
    emit_json(
        {
            "ok": True,
            "python": sys.version.split()[0],
            "larkCli": lark_cli,
            "larkCliVersion": version,
            "runsDir": str(web_tool.RUNS_DIR.resolve()),
            "csdnEditorUrl": web_tool.CSDN_EDITOR_URL,
        }
    )
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    web_tool.RUNS_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Open: http://{args.host}:{args.port}", file=sys.stderr)
    web_tool.app.run(host=args.host, port=args.port, debug=False)
    return 0


def cmd_process(args: argparse.Namespace) -> int:
    inputs = [Path(item).expanduser().resolve() for item in args.input]
    missing = [str(path) for path in inputs if not path.exists()]
    if missing:
        raise web_tool.UserVisibleError("输入文件不存在：" + "；".join(missing))

    job_dir = web_tool.make_job_dir()
    source = web_tool.combine_saved_files(inputs, job_dir)
    result = web_tool.process_source(
        job_dir,
        source,
        payload_from_args(args),
        "CLI local files",
        progress=make_progress(args.jsonl),
    )
    emit_json(summarize_result(result, job_dir, args.full))
    return 0


def cmd_lark(args: argparse.Namespace) -> int:
    job_dir = web_tool.make_job_dir()
    progress = make_progress(args.jsonl)
    content, notices = web_tool.fetch_lark_markdown(args.url, progress)
    source = job_dir / "input" / "lark_source.md"
    source.parent.mkdir(parents=True, exist_ok=True)
    web_tool.write_text(source, content)
    result = web_tool.process_source(
        job_dir,
        source,
        payload_from_args(args),
        "CLI Feishu/Lark document",
        notices=notices,
        progress=progress,
    )
    emit_json(summarize_result(result, job_dir, args.full))
    return 0


def cmd_rerun(args: argparse.Namespace) -> int:
    job_dir = web_tool.get_job_dir(args.job_id)
    payload = payload_from_args(args)
    payload["imageSource"] = args.image_source
    result = web_tool.rerun_single_image(job_dir, args.image_index, payload)
    emit_json(summarize_result(result, job_dir, args.full))
    return 0


def add_processing_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--terms", default="", help="额外脱敏词，支持中文逗号、英文逗号或换行分隔")
    parser.add_argument("--match-mode", choices=["fuzzy", "exact"], default="fuzzy", help="脱敏词匹配方式")
    parser.add_argument("--use-cpa", action="store_true", help="启用 CPA 大模型辅助判断")
    parser.add_argument("--api-key", default="", help="CPA API Key，也可使用环境变量 CPA_API_KEY")
    parser.add_argument("--base-url", default=web_tool.DEFAULT_BASE_URL, help="CPA OpenAI-compatible Base URL")
    parser.add_argument("--model", default=web_tool.DEFAULT_MODEL, help="CPA 模型名称")
    parser.add_argument("--enhance-with-prompt", action="store_true", help="结合行业提示词增强判断")
    parser.add_argument("--industry-prompt-id", default="", help="使用 Web 设置中已保存的行业提示词 ID")
    parser.add_argument("--industry-prompt-file", default="", help="直接读取一个本地行业提示词文件")
    parser.add_argument("--min-confidence", type=float, default=0.45, help="OCR 最低置信度")
    parser.add_argument("--padding", type=int, default=8, help="马赛克外扩像素")
    parser.add_argument("--block-size", type=int, default=12, help="马赛克颗粒大小")
    parser.add_argument("--jsonl", action="store_true", help="处理过程按 JSON Lines 输出，方便 Agent 实时读取")
    parser.add_argument("--full", action="store_true", help="输出完整任务结果，包含文章正文和剪贴板 HTML")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="auto-image-redactor",
        description="Agent-friendly CLI for the local CSDN image redaction assistant.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    health = subparsers.add_parser("health", help="检查 Python、飞书 CLI 和运行目录")
    health.set_defaults(func=cmd_health)

    serve = subparsers.add_parser("serve", help="启动本地 HTML 工具")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8866)
    serve.set_defaults(func=cmd_serve)

    process = subparsers.add_parser("process", help="处理本地 Markdown/HTML/Word/PDF/图片")
    process.add_argument("input", nargs="+", help="一个或多个本地文件路径")
    add_processing_options(process)
    process.set_defaults(func=cmd_process)

    lark = subparsers.add_parser("lark", help="读取飞书文档链接并脱敏")
    lark.add_argument("url", help="飞书 docx/wiki 文档链接")
    add_processing_options(lark)
    lark.set_defaults(func=cmd_lark)

    rerun = subparsers.add_parser("rerun-image", help="对某个任务中的单张图片重新识别")
    rerun.add_argument("job_id", help="任务 ID")
    rerun.add_argument("image_index", type=int, help="图片序号，从 1 开始")
    rerun.add_argument("--image-source", choices=["original", "masked"], default="original", help="重新识别基于原图还是当前打码图")
    rerun.add_argument("--instruction", default="", help="这一张图的单独识别要求")
    add_processing_options(rerun)
    rerun.set_defaults(func=cmd_rerun)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return args.func(args)
    except web_tool.UserVisibleError as exc:
        emit_json({"ok": False, "error": str(exc)})
        return 2
    except Exception as exc:  # noqa: BLE001
        emit_json({"ok": False, "error": str(exc)})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
