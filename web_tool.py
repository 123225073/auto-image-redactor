from __future__ import annotations

import base64
import html
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
import zipfile
from argparse import Namespace
from pathlib import Path
from queue import Empty, Queue
from threading import Event, Thread
from typing import Any

from bs4 import BeautifulSoup
from flask import Flask, Response, jsonify, request, send_file, send_from_directory, stream_with_context
from markdown import markdown
import requests
from werkzeug.utils import secure_filename

from csdn_image_mosaic import (
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    IMAGE_SUFFIXES,
    ProcessResult,
    collect_image_refs,
    load_terms,
    process_document,
    process_image,
    read_text,
    replace_refs,
    write_report,
    write_report_json,
    write_text,
)


APP_DIR = Path(__file__).resolve().parent
RUNS_DIR = APP_DIR / "runs"
PROMPTS_CONFIG_PATH = APP_DIR / "industry_prompts.json"
DEFAULT_PROMPTS_DIR = APP_DIR / "industry_prompts"
CSDN_EDITOR_URL = "https://editor.csdn.net/md/"

app = Flask(__name__, static_folder="static", static_url_path="/static")
app.config["MAX_CONTENT_LENGTH"] = 300 * 1024 * 1024

JOB_PAUSE_EVENTS: dict[str, Event] = {}


class UserVisibleError(RuntimeError):
    pass


def json_error(message: str, status: int = 400):
    return jsonify({"ok": False, "error": message}), status


def ndjson_event(event_type: str, message: str, data: dict[str, Any] | None = None) -> str:
    payload: dict[str, Any] = {
        "ok": event_type != "error",
        "type": event_type,
        "message": message,
        "time": time.strftime("%H:%M:%S"),
    }
    if data is not None:
        payload["data"] = data
    return json.dumps(payload, ensure_ascii=False) + "\n"


def stream_task(task):
    events: Queue[dict[str, Any] | None] = Queue()

    def emit(message: str, event_type: str = "progress", data: dict[str, Any] | None = None) -> None:
        events.put({"type": event_type, "message": message, "data": data})

    def worker() -> None:
        try:
            result = task(emit)
            events.put({"type": "done", "message": "处理完成。", "data": {"result": result}})
        except UserVisibleError as exc:
            events.put({"type": "error", "message": str(exc), "data": None})
        except Exception as exc:  # noqa: BLE001
            events.put({"type": "error", "message": f"处理失败：{exc}", "data": None})
        finally:
            events.put(None)

    Thread(target=worker, daemon=True).start()

    def generate():
        yield ndjson_event("start", "任务已开始。")
        while True:
            try:
                item = events.get(timeout=15)
            except Empty:
                yield ndjson_event("heartbeat", "仍在处理中，请稍等。")
                continue
            if item is None:
                break
            yield ndjson_event(item["type"], item["message"], item.get("data"))

    return Response(stream_with_context(generate()), mimetype="application/x-ndjson; charset=utf-8")


def make_job_dir() -> Path:
    job_dir = RUNS_DIR / uuid.uuid4().hex[:12]
    (job_dir / "input").mkdir(parents=True, exist_ok=True)
    (job_dir / "output").mkdir(parents=True, exist_ok=True)
    return job_dir


def job_id_from_dir(job_dir: Path) -> str:
    return job_dir.name


def get_pause_event(job_id: str) -> Event:
    event = JOB_PAUSE_EVENTS.get(job_id)
    if event is None:
        event = Event()
        event.set()
        JOB_PAUSE_EVENTS[job_id] = event
    return event


def set_job_paused(job_id: str, paused: bool) -> None:
    event = get_pause_event(job_id)
    if paused:
        event.clear()
    else:
        event.set()


def get_job_dir(job_id: str) -> Path:
    if not re.fullmatch(r"[a-f0-9]{12}", job_id):
        raise UserVisibleError("任务编号无效")
    job_dir = (RUNS_DIR / job_id).resolve()
    if not job_dir.exists():
        raise UserVisibleError("找不到这个处理任务")
    return job_dir


def save_terms(job_dir: Path, terms_text: str | None) -> Path:
    default_terms = read_text(APP_DIR / "sensitive_terms.txt") if (APP_DIR / "sensitive_terms.txt").exists() else ""
    combined = default_terms.strip()
    if terms_text and terms_text.strip():
        combined += "\n" + terms_text.strip()
    path = job_dir / "input" / "sensitive_terms.txt"
    write_text(path, combined.strip() + "\n")
    return path


def read_json_file(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(read_text(path))
    except Exception:
        return default


def prompt_config() -> dict[str, Any]:
    data = read_json_file(PROMPTS_CONFIG_PATH, {})
    prompts_dir = Path(data.get("path") or DEFAULT_PROMPTS_DIR).expanduser()
    return {"path": str(prompts_dir)}


def prompt_dir() -> Path:
    configured = Path(prompt_config()["path"]).expanduser()
    return configured if configured.is_absolute() else (APP_DIR / configured).resolve()


def safe_prompt_name(name: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", (name or "").strip()).strip(" ._")
    if not cleaned:
        raise UserVisibleError("请填写行业提示词名称")
    return cleaned[:80]


def list_prompt_files() -> list[dict[str, str]]:
    root = prompt_dir()
    root.mkdir(parents=True, exist_ok=True)
    prompts: list[dict[str, str]] = []
    for path in sorted(root.iterdir(), key=lambda item: item.name.lower()):
        if path.is_file() and path.suffix.lower() in {".txt", ".md", ".json"}:
            prompts.append({"id": path.name, "name": path.stem, "content": read_text(path)})
    return prompts


def load_industry_prompt(prompt_id: str | None) -> str:
    prompt_id = (prompt_id or "").strip()
    if not prompt_id:
        return ""
    root = prompt_dir().resolve()
    candidate = (root / Path(prompt_id).name).resolve()
    if root not in candidate.parents or not candidate.is_file():
        raise UserVisibleError("找不到选择的行业提示词")
    if candidate.suffix.lower() not in {".txt", ".md", ".json"}:
        raise UserVisibleError("行业提示词文件只支持 txt、md、json")
    return read_text(candidate)


def normalize_user_terms(text: str | None, match_mode: str) -> str:
    if not text:
        return ""
    prefix = "exact:" if match_mode == "exact" else "fuzzy:"
    terms = [part.strip() for part in re.split(r"[\n,，;；、]+", text) if part.strip()]
    return "\n".join(f"{prefix}{term}" for term in terms)


def bool_from_form(value: Any) -> bool:
    return str(value).lower() in {"1", "true", "yes", "on"}


def build_options(job_dir: Path, source_path: Path, payload: dict[str, Any]) -> Namespace:
    match_mode = payload.get("matchMode") or "fuzzy"
    enhance_with_prompt = bool_from_form(payload.get("enhanceWithPrompt"))
    industry_prompt_id = payload.get("industryPromptId") or ""
    industry_prompt = payload.get("industryPrompt") or (
        load_industry_prompt(industry_prompt_id) if enhance_with_prompt and industry_prompt_id else ""
    )
    return Namespace(
        input=str(source_path),
        output_dir=str(job_dir / "output"),
        terms=str(save_terms(job_dir, normalize_user_terms(payload.get("terms"), match_mode))),
        mode="cpa" if enhance_with_prompt else (payload.get("mode") or "local"),
        api_key=payload.get("apiKey") or os.environ.get("CPA_API_KEY", ""),
        base_url=payload.get("baseUrl") or DEFAULT_BASE_URL,
        model=payload.get("model") or DEFAULT_MODEL,
        match_mode=match_mode,
        mask_all_text=bool_from_form(payload.get("maskAllText")),
        enhance_with_prompt=enhance_with_prompt,
        industry_prompt_id=industry_prompt_id,
        industry_prompt=industry_prompt,
        image_instruction=payload.get("imageInstruction") or "",
        min_confidence=float(payload.get("minConfidence") or 0.45),
        padding=int(payload.get("padding") or 8),
        block_size=int(payload.get("blockSize") or 12),
    )


def resolve_lark_cli() -> str | None:
    candidates = [
        shutil.which("lark-cli.cmd"),
        shutil.which("lark-cli.exe"),
        shutil.which("lark-cli"),
        str(Path.home() / "AppData" / "Roaming" / "npm" / "lark-cli.cmd"),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    try:
        completed = subprocess.run(
            ["where.exe", "lark-cli.cmd"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        if completed.returncode == 0:
            first = completed.stdout.splitlines()[0].strip()
            if first and Path(first).exists():
                return first
    except Exception:
        pass
    return None


def run_lark_cli(args: list[str], timeout: int = 180) -> subprocess.CompletedProcess[str]:
    lark_cli = resolve_lark_cli()
    if not lark_cli:
        raise UserVisibleError("没有找到 lark-cli。请确认本机已安装飞书 CLI，或重新打开本工具。")

    if lark_cli.lower().endswith((".cmd", ".bat")):
        cmd = ["cmd.exe", "/d", "/c", lark_cli, *args]
    else:
        cmd = [lark_cli, *args]
    return subprocess.run(
        cmd,
        cwd=str(APP_DIR),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def run_lark_cli_progress(
    args: list[str],
    emit,
    timeout: int = 180,
) -> subprocess.CompletedProcess[str]:
    lark_cli = resolve_lark_cli()
    if not lark_cli:
        raise UserVisibleError("没有找到 lark-cli。请确认本机已安装飞书 CLI，或重新打开本工具。")

    if lark_cli.lower().endswith((".cmd", ".bat")):
        cmd = ["cmd.exe", "/d", "/c", lark_cli, *args]
    else:
        cmd = [lark_cli, *args]

    emit(f"已找到飞书 CLI：{Path(lark_cli).name}")
    emit("开始执行飞书 CLI 命令。")
    process = subprocess.Popen(
        cmd,
        cwd=str(APP_DIR),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    output_lines: list[str] = []
    started = time.monotonic()
    json_notice_sent = False
    assert process.stdout is not None
    while True:
        if time.monotonic() - started > timeout:
            process.kill()
            raise UserVisibleError("飞书 CLI 执行超时，请检查网络、授权或文档权限。")
        line = process.stdout.readline()
        if line:
            output_lines.append(line)
            clean = line.strip()
            if clean:
                if clean[0] in "{[}]" or clean.startswith('"'):
                    if not json_notice_sent:
                        emit("飞书 CLI 已返回结构化结果，正在解析。")
                        json_notice_sent = True
                else:
                    emit(f"飞书 CLI：{clean[:800]}")
            continue
        if process.poll() is not None:
            remainder = process.stdout.read()
            if remainder:
                output_lines.append(remainder)
                for clean in (item.strip() for item in remainder.splitlines()):
                    if clean:
                        if clean[0] in "{[}]" or clean.startswith('"'):
                            if not json_notice_sent:
                                emit("飞书 CLI 已返回结构化结果，正在解析。")
                                json_notice_sent = True
                        else:
                            emit(f"飞书 CLI：{clean[:800]}")
            break
        time.sleep(0.1)

    stdout = "".join(output_lines)
    emit(f"飞书 CLI 命令结束，退出码 {process.returncode or 0}。")
    return subprocess.CompletedProcess(cmd, process.returncode or 0, stdout=stdout, stderr="")


def parse_cli_json(text: str) -> dict[str, Any]:
    raw = text.strip()
    start = raw.find("{")
    if start > 0:
        raw = raw[start:]
    return json.loads(raw)


def parse_cli_json_any(text: str) -> Any:
    raw = text.strip()
    starts = [index for index in (raw.find("{"), raw.find("[")) if index >= 0]
    if starts:
        raw = raw[min(starts):]
    return json.loads(raw)


def run_lark_json(args: list[str], timeout: int = 120) -> Any:
    completed = run_lark_cli(args, timeout=timeout)
    raw = (completed.stdout or "") + ("\n" + completed.stderr if completed.stderr else "")
    if completed.returncode != 0:
        raise UserVisibleError(raw.strip() or "飞书 CLI 执行失败")
    try:
        return parse_cli_json_any(raw)
    except json.JSONDecodeError:
        return {"raw": raw.strip()}


def safe_auth_status(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        return {"raw": data}
    identities = data.get("identities") or {}
    user_identity = identities.get("user") if isinstance(identities, dict) else {}
    return {
        "appId": data.get("appId"),
        "brand": data.get("brand"),
        "defaultAs": data.get("defaultAs"),
        "identity": data.get("identity"),
        "userName": data.get("userName") or (user_identity or {}).get("userName"),
        "userOpenId": data.get("userOpenId") or (user_identity or {}).get("openId"),
        "tokenStatus": data.get("tokenStatus") or (user_identity or {}).get("tokenStatus"),
        "expiresAt": data.get("expiresAt") or (user_identity or {}).get("expiresAt"),
        "refreshExpiresAt": data.get("refreshExpiresAt") or (user_identity or {}).get("refreshExpiresAt"),
        "botStatus": ((identities.get("bot") or {}) if isinstance(identities, dict) else {}).get("status"),
        "userStatus": ((identities.get("user") or {}) if isinstance(identities, dict) else {}).get("status"),
    }


def friendly_cli_error(raw: str, fallback: str = "飞书 CLI 执行失败") -> str:
    try:
        data = parse_cli_json_any(raw)
    except Exception:
        return raw.strip() or fallback
    error = data.get("error") if isinstance(data, dict) else None
    if not isinstance(error, dict):
        return raw.strip() or fallback
    code = error.get("code")
    message = error.get("message") or fallback
    hint = ""
    if code == 330004:
        hint = "当前飞书账号没有这个文档的查看权限。请切换到有权限的账号，或让文档所有者授权。"
    elif code == 131005:
        hint = "飞书没有找到这个知识库节点。请确认链接是否完整、是否已分享给当前账号。"
    elif "permission" in str(message).lower():
        hint = "请检查当前飞书账号是否有文档权限，必要时重新授权或切换账号。"
    return f"飞书接口返回错误：{message}" + (f"\n{hint}" if hint else "") + (f"\n错误码：{code}" if code else "")


def resolve_wiki_doc_url(doc_url: str, emit=None) -> tuple[str, str | None]:
    if "/wiki/" not in doc_url:
        return doc_url, None
    if emit:
        emit("检测到知识库链接，先解析真实文档。")
        completed = run_lark_cli_progress(["wiki", "+node-get", "--as", "user", "--token", doc_url, "--format", "json"], emit, timeout=120)
    else:
        completed = run_lark_cli(["wiki", "+node-get", "--as", "user", "--token", doc_url, "--format", "json"], timeout=120)
    if completed.returncode != 0:
        return doc_url, (completed.stdout or completed.stderr or "").strip()
    try:
        data = parse_cli_json((completed.stdout or "") + ("\n" + completed.stderr if completed.stderr else ""))
    except json.JSONDecodeError:
        return doc_url, (completed.stdout or completed.stderr or "").strip()
    node = ((data.get("data") or {}).get("node")) or (data.get("data") or {})
    obj_token = node.get("obj_token") or node.get("objToken")
    obj_type = node.get("obj_type") or node.get("objType")
    if obj_token and obj_type in {"doc", "docx"}:
        if emit:
            emit("知识库链接解析完成，已拿到真实文档 token。")
        return obj_token, None
    return doc_url, None


def fetch_lark_markdown(doc_url: str, emit=None) -> tuple[str, list[str]]:
    doc_url = (doc_url or "").strip().rstrip("，。；;：:")
    if not doc_url:
        raise UserVisibleError("请先填写飞书文档链接")

    if emit:
        emit("开始检查飞书文档链接。")
    resolved_doc, wiki_warning = resolve_wiki_doc_url(doc_url, emit)
    cmd = [
        "docs",
        "+fetch",
        "--api-version",
        "v2",
        "--as",
        "user",
        "--doc",
        resolved_doc,
        "--doc-format",
        "markdown",
        "--format",
        "json",
    ]
    if emit:
        emit("开始读取飞书文档正文。")
        completed = run_lark_cli_progress(cmd, emit)
    else:
        completed = run_lark_cli(cmd)
    raw = (completed.stdout or "") + ("\n" + completed.stderr if completed.stderr else "")
    if completed.returncode != 0:
        detail = friendly_cli_error(raw, "飞书 CLI 获取文档失败，请检查是否已登录并有文档权限")
        if wiki_warning:
            detail = f"知识库链接解析失败：{friendly_cli_error(wiki_warning, '知识库链接解析失败')}\n\n文档读取失败：{detail}"
        raise UserVisibleError(detail)

    try:
        data = parse_cli_json(completed.stdout)
    except json.JSONDecodeError as exc:
        raise UserVisibleError(f"飞书 CLI 返回内容不是标准 JSON：{exc}") from exc

    if not data.get("ok"):
        raise UserVisibleError(friendly_cli_error(json.dumps(data, ensure_ascii=False), "飞书文档读取失败"))

    document = (((data.get("data") or {}).get("document")) or {})
    content = document.get("content") or ""
    if not content.strip():
        raise UserVisibleError("飞书文档内容为空，或当前账号没有读取正文的权限")
    if emit:
        emit(f"飞书正文读取完成，正文长度约 {len(content)} 个字符。")

    notices: list[str] = []
    notice = data.get("_notice") or {}
    if notice.get("update"):
        notices.append(str(notice["update"].get("message") or "lark-cli 有可用更新"))
    return content, notices


def convert_doc_to_docx(path: Path) -> Path:
    try:
        import win32com.client  # type: ignore
    except Exception as exc:  # noqa: BLE001
        raise UserVisibleError("当前电脑缺少 Word 自动转换组件，暂时无法直接处理 .doc；请先另存为 .docx 再上传。") from exc

    output = path.with_suffix(".docx")
    word = win32com.client.Dispatch("Word.Application")
    word.Visible = False
    try:
        doc = word.Documents.Open(str(path))
        doc.SaveAs(str(output), FileFormat=16)
        doc.Close(False)
    finally:
        word.Quit()
    return output


def docx_to_html(path: Path) -> str:
    import mammoth

    with path.open("rb") as handle:
        result = mammoth.convert_to_html(handle, convert_image=mammoth.images.data_uri)
    messages = [str(message) for message in result.messages]
    warning_html = ""
    if messages:
        warning_html = "<blockquote>Word 转换提示：" + html.escape("；".join(messages)) + "</blockquote>"
    return warning_html + result.value


def pdf_to_html(path: Path, media_dir: Path) -> str:
    import fitz

    pdf = fitz.open(path)
    blocks: list[str] = [f"<h2>{html.escape(path.stem)}</h2>"]
    doc_media = media_dir / secure_filename(path.stem or "pdf")
    doc_media.mkdir(parents=True, exist_ok=True)
    matrix = fitz.Matrix(2, 2)
    for index, page in enumerate(pdf, start=1):
        pixmap = page.get_pixmap(matrix=matrix, alpha=False)
        image_path = doc_media / f"page_{index:03d}.png"
        pixmap.save(str(image_path))
        rel = image_path.relative_to(media_dir.parent).as_posix()
        blocks.append(f'<h3>第 {index} 页</h3><p><img src="{html.escape(rel)}" alt="{html.escape(path.stem)} 第 {index} 页"></p>')
    return "\n".join(blocks)


def file_to_html(path: Path, media_dir: Path) -> str:
    suffix = path.suffix.lower()
    title = html.escape(path.name)

    if suffix in {".html", ".htm"}:
        return read_text(path)
    if suffix in {".md", ".markdown"}:
        body = markdown(read_text(path), extensions=["extra", "tables", "sane_lists"])
        return f"<h2>{title}</h2>\n{body}"
    if suffix == ".txt":
        return f"<h2>{title}</h2><pre>{html.escape(read_text(path))}</pre>"
    if suffix == ".doc":
        path = convert_doc_to_docx(path)
        suffix = ".docx"
    if suffix == ".docx":
        return f"<h2>{title}</h2>\n{docx_to_html(path)}"
    if suffix == ".pdf":
        return pdf_to_html(path, media_dir)
    if suffix in {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff"}:
        target = media_dir / path.name
        if path.resolve() != target.resolve():
            shutil.copy2(path, target)
        rel = target.relative_to(media_dir.parent).as_posix()
        return f'<h2>{title}</h2><p><img src="{html.escape(rel)}" alt="{title}"></p>'

    raise UserVisibleError(f"暂不支持这个文件类型：{path.name}")


def save_uploaded_files(files: list[Any], job_dir: Path) -> list[Path]:
    input_dir = job_dir / "input"
    saved_files: list[Path] = []
    for index, file in enumerate(files, start=1):
        filename = secure_filename(file.filename or f"upload_{index}")
        original_suffix = Path(file.filename or "").suffix
        if not filename:
            filename = f"upload_{index}{original_suffix}"
        elif not Path(filename).suffix and original_suffix:
            filename = f"{filename}{original_suffix}"
        saved_path = input_dir / filename
        file.save(saved_path)
        saved_files.append(saved_path)
    return saved_files


def combine_saved_files(saved_files: list[Path], job_dir: Path) -> Path:
    if not saved_files:
        raise UserVisibleError("请至少上传一个文件")

    input_dir = job_dir / "input"
    media_dir = input_dir / "uploaded_media"
    media_dir.mkdir(parents=True, exist_ok=True)

    image_suffixes = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff"}
    document_files = [path for path in saved_files if path.suffix.lower() not in image_suffixes]
    files_to_render = document_files or saved_files

    # If an article file and its referenced images are uploaded together, render only the article.
    # The image files stay beside it so relative links can be resolved without duplicating images.
    fragments = [file_to_html(path, media_dir) for path in files_to_render]

    source = input_dir / "uploaded_source.html"
    html_doc = "<!doctype html><html><head><meta charset=\"utf-8\"></head><body>\n"
    html_doc += "\n<hr>\n".join(fragments)
    html_doc += "\n</body></html>\n"
    write_text(source, html_doc)
    return source


def combine_uploaded_files(files: list[Any], job_dir: Path) -> Path:
    if not files:
        raise UserVisibleError("请至少上传一个文件")
    saved_files = save_uploaded_files(files, job_dir)
    return combine_saved_files(saved_files, job_dir)


def mime_for_path(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".jpg", ".jpeg"}:
        return "image/jpeg"
    if suffix == ".webp":
        return "image/webp"
    if suffix == ".gif":
        return "image/gif"
    return "image/png"


def image_to_data_uri(path: Path) -> str:
    mime = mime_for_path(path)
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def markdown_to_clipboard_html(content: str) -> str:
    return markdown(content, extensions=["extra", "tables", "sane_lists", "nl2br"])


def inline_images_for_clipboard(content: str, output_path: Path) -> str:
    if output_path.suffix.lower() in {".html", ".htm"}:
        html_content = content
    else:
        html_content = markdown_to_clipboard_html(content)

    soup = BeautifulSoup(html_content, "html.parser")
    for img in soup.find_all("img"):
        src = img.get("src")
        if not src or src.startswith("data:") or re.match(r"^https?://", src):
            continue
        candidate = (output_path.parent / src).resolve()
        if candidate.exists() and output_path.parent.resolve() in candidate.parents:
            img["src"] = image_to_data_uri(candidate)
    return str(soup)


def write_job_meta(job_dir: Path, output_path: Path, source_label: str, notices: list[str] | None = None) -> None:
    write_text(
        job_dir / "job.json",
        json.dumps(
            {
                "outputFile": output_path.name,
                "source": source_label,
                "notices": notices or [],
            },
            ensure_ascii=False,
            indent=2,
        ),
    )


def write_job_options(job_dir: Path, payload: dict[str, Any], source_path: Path) -> None:
    stored = {
        "sourcePath": str(source_path),
        "terms": payload.get("terms") or "",
        "matchMode": payload.get("matchMode") or "fuzzy",
        "mode": payload.get("mode") or "local",
        "baseUrl": payload.get("baseUrl") or DEFAULT_BASE_URL,
        "model": payload.get("model") or DEFAULT_MODEL,
        "enhanceWithPrompt": bool_from_form(payload.get("enhanceWithPrompt")),
        "industryPromptId": payload.get("industryPromptId") or "",
        "minConfidence": payload.get("minConfidence") or 0.45,
        "padding": payload.get("padding") or 8,
        "blockSize": payload.get("blockSize") or 12,
    }
    write_text(job_dir / "options.json", json.dumps(stored, ensure_ascii=False, indent=2))


def find_output_path(job_dir: Path) -> Path:
    meta_path = job_dir / "job.json"
    output_dir = job_dir / "output"
    if meta_path.exists():
        meta = json.loads(read_text(meta_path))
        candidate = output_dir / meta.get("outputFile", "")
        if candidate.exists():
            return candidate
    candidates = [
        path
        for path in output_dir.iterdir()
        if path.is_file() and path.name not in {"report.md", "report.json"} and not path.name.endswith(".zip")
    ]
    if not candidates:
        raise UserVisibleError("找不到这个任务的输出文件")
    return sorted(candidates, key=lambda item: item.stat().st_mtime, reverse=True)[0]


def result_payload(
    job_dir: Path,
    output_path: Path,
    source_label: str,
    notices: list[str] | None = None,
    persist: bool = True,
) -> dict[str, Any]:
    job_id = job_id_from_dir(job_dir)
    content = read_text(output_path)
    report_path = output_path.parent / "report.md"
    report_json_path = output_path.parent / "report.json"
    report = read_text(report_path) if report_path.exists() else ""
    report_data = json.loads(read_text(report_json_path)) if report_json_path.exists() else {"images": []}
    clipboard_html = inline_images_for_clipboard(content, output_path)
    zip_path = make_zip(job_dir)
    if persist:
        write_job_meta(job_dir, output_path, source_label, notices)
    return {
        "ok": True,
        "jobId": job_id,
        "source": source_label,
        "outputFile": output_path.name,
        "content": content,
        "report": report,
        "reportData": report_data,
        "clipboardHtml": clipboard_html,
        "editorUrl": CSDN_EDITOR_URL,
        "downloadUrl": f"/api/jobs/{job_id}/download",
        "outputUrl": f"/api/jobs/{job_id}/file/{output_path.name}",
        "zipFile": zip_path.name,
        "notices": notices or [],
    }


def process_source(
    job_dir: Path,
    source_path: Path,
    payload: dict[str, Any],
    source_label: str,
    notices: list[str] | None = None,
    progress=None,
):
    write_job_options(job_dir, payload, source_path)
    options = build_options(job_dir, source_path, payload)
    job_id = job_id_from_dir(job_dir)
    options.job_id = job_id
    options.pause_event = get_pause_event(job_id)
    options.progress_callback = progress
    output_path = process_document(options)
    return result_payload(job_dir, output_path, source_label, notices)


def make_zip(job_dir: Path) -> Path:
    output_dir = job_dir / "output"
    zip_path = job_dir / "csdn_safe_package.zip"
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in output_dir.rglob("*"):
            if path.is_file():
                archive.write(path, path.relative_to(output_dir).as_posix())
    return zip_path


def cpa_url(base_url: str, path: str) -> str:
    return f"{(base_url or DEFAULT_BASE_URL).rstrip('/')}/{path.lstrip('/')}"


def cpa_headers(api_key: str) -> dict[str, str]:
    if not api_key:
        raise UserVisibleError("请先填写 CPA API Key")
    return {"authorization": f"Bearer {api_key}", "content-type": "application/json"}


def try_read_csdn_cookie() -> str:
    try:
        import browser_cookie3  # type: ignore
    except Exception as exc:  # noqa: BLE001
        raise UserVisibleError("当前环境还缺少自动读取浏览器 Cookie 的组件，请先运行依赖安装，或按页面里的手动引导获取。") from exc

    def friendly_cookie_error(browser: str, exc: Exception) -> str:
        message = str(exc)
        lower = message.lower()
        if "requires admin" in lower or "run as admin" in lower:
            return f"{browser}: Windows 当前不允许读取加密 Cookie，可以用管理员方式启动本工具，或按页面手动引导复制。"
        if "profile" in lower:
            return f"{browser}: 没找到这个浏览器的用户数据。"
        if "permission" in lower or "access" in lower:
            return f"{browser}: 没有读取权限，可以关闭浏览器后重试，或按页面手动引导复制。"
        return f"{browser}: {message}"

    pairs: dict[str, str] = {}
    errors: list[str] = []
    loaders = [
        ("Edge", getattr(browser_cookie3, "edge", None)),
        ("Chrome", getattr(browser_cookie3, "chrome", None)),
        ("Firefox", getattr(browser_cookie3, "firefox", None)),
    ]
    for name, loader in loaders:
        if not callable(loader):
            continue
        try:
            jar = loader(domain_name=".csdn.net")
            for cookie in jar:
                if "csdn.net" in (cookie.domain or ""):
                    pairs[cookie.name] = cookie.value
        except Exception as exc:  # noqa: BLE001
            errors.append(friendly_cookie_error(name, exc))

    if not pairs:
        detail = "；".join(errors[:2])
        raise UserVisibleError(
            "没有从本机浏览器读到 CSDN 登录 Cookie。请先确认 Chrome/Edge 已登录 CSDN；如果自动读取仍失败，请按页面下方手动引导复制。"
            + (f"\n读取细节：{detail}" if detail else "")
        )
    return "; ".join(f"{name}={value}" for name, value in sorted(pairs.items()))


def get_report_json_path(job_dir: Path) -> Path:
    return job_dir / "output" / "report.json"


def load_report_data(job_dir: Path) -> dict[str, Any]:
    path = get_report_json_path(job_dir)
    if not path.exists():
        raise UserVisibleError("这个任务没有图片检查报告，无法重新识别")
    return json.loads(read_text(path))


def build_rerun_payload(job_dir: Path, payload: dict[str, Any]) -> dict[str, Any]:
    stored = read_json_file(job_dir / "options.json", {})
    rerun = dict(stored)
    for key in (
        "terms",
        "matchMode",
        "mode",
        "apiKey",
        "baseUrl",
        "model",
        "enhanceWithPrompt",
        "industryPromptId",
        "imageInstruction",
        "minConfidence",
        "padding",
        "blockSize",
    ):
        if key in payload:
            rerun[key] = payload.get(key)
    if bool_from_form(rerun.get("enhanceWithPrompt")):
        rerun["mode"] = "cpa"
    return rerun


def resolve_output_image_ref(job_dir: Path, image_ref: str | None) -> Path | None:
    if not image_ref:
        return None
    output_dir = (job_dir / "output").resolve()
    candidate = (output_dir / str(image_ref)).resolve()
    if candidate.exists() and candidate.is_file() and output_dir in candidate.parents:
        return candidate
    return None


def resolve_image_source(job_dir: Path, image_info: dict[str, Any], image_source: str = "original") -> Path:
    if image_source == "masked":
        masked = resolve_output_image_ref(job_dir, image_info.get("outputRef"))
        if masked:
            return masked
        raise UserVisibleError("找不到当前打码后的图片，无法基于打码图重新识别。")

    candidates = [
        image_info.get("sourceImageRef"),
        image_info.get("sourcePath"),
    ]
    for value in candidates:
        if not value:
            continue
        output_match = resolve_output_image_ref(job_dir, str(value))
        if output_match:
            return output_match
        path = Path(str(value))
        if path.exists() and path.is_file():
            return path
        relative = (job_dir / str(value)).resolve()
        if relative.exists() and relative.is_file():
            return relative
    source_ref = image_info.get("sourceRef")
    if source_ref:
        cache_dir = job_dir / "output" / ".source_cache"
        from csdn_image_mosaic import materialize_image  # local import keeps the public import list tidy

        source_path, error = materialize_image(str(source_ref), job_dir / "input", cache_dir)
        if source_path:
            return source_path
        raise UserVisibleError(f"重新读取原图失败：{error}")
    raise UserVisibleError("报告里没有原图信息，无法重新识别这张图片。请重新处理整篇文章。")


def is_output_image_ref(ref: str | None) -> bool:
    if not ref:
        return False
    return Path(str(ref).split("?", 1)[0]).suffix.lower() in IMAGE_SUFFIXES


def rerun_single_image(job_dir: Path, image_index: int, payload: dict[str, Any]) -> dict[str, Any]:
    report_data = load_report_data(job_dir)
    images = report_data.get("images") or []
    if image_index < 1 or image_index > len(images):
        raise UserVisibleError("图片序号无效")

    image_info = images[image_index - 1]
    image_source = payload.get("imageSource") or "original"
    if image_source not in {"original", "masked"}:
        image_source = "original"
    source_path = resolve_image_source(job_dir, image_info, image_source)
    output_path = find_output_path(job_dir)
    output_dir = job_dir / "output"
    images_dir = output_dir / "images"
    rerun_payload = build_rerun_payload(job_dir, payload)
    options = build_options(job_dir, Path(read_json_file(job_dir / "options.json", {}).get("sourcePath") or output_path), rerun_payload)
    options.image_instruction = payload.get("imageInstruction") or rerun_payload.get("imageInstruction") or ""
    terms = load_terms(Path(options.terms))

    original_output_ref = image_info.get("outputRef")
    source_ref = image_info.get("sourceRef") or image_info.get("originalRef") or f"image-{image_index}"
    processing_ref = str(source_ref) if image_source == "original" else f"{source_ref}__masked_input"
    new_source_image_ref, new_output_ref, masked_count, matches = process_image(processing_ref, source_path, images_dir, terms, options)

    if is_output_image_ref(original_output_ref) and original_output_ref != new_output_ref:
        src = (output_dir / new_output_ref).resolve()
        dst = (output_dir / original_output_ref).resolve()
        if src.exists() and output_dir.resolve() in src.parents and output_dir.resolve() in dst.parents:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            new_output_ref = original_output_ref

    image_info.update(
        {
            "status": "ok",
            "outputRef": new_output_ref,
            "sourcePath": str(source_path),
            "sourceImageRef": new_source_image_ref if image_source == "original" or not image_info.get("sourceImageRef") else image_info.get("sourceImageRef"),
            "rerunInputRef": new_source_image_ref if image_source == "masked" else None,
            "lastRerunSource": image_source,
            "maskedRegions": masked_count,
            "error": None,
            "matches": matches,
            "note": options.image_instruction or None,
            "updatedAt": int(time.time()),
        }
    )
    write_text(get_report_json_path(job_dir), json.dumps(report_data, ensure_ascii=False, indent=2))

    source_ref_for_replace = image_info.get("sourceRef")
    if source_ref_for_replace and new_output_ref:
        content = read_text(output_path)
        updated = replace_refs(content, {str(source_ref_for_replace): new_output_ref})
        if updated != content:
            write_text(output_path, updated)

    source_path_from_options = Path(read_json_file(job_dir / "options.json", {}).get("sourcePath") or output_path)
    mode = options.mode
    write_report(output_dir / "report.md", source_path_from_options, output_path, [
        ProcessResult(
            original_ref=item.get("sourceRef") or item.get("originalRef") or "",
            status=item.get("status") or "ok",
            output_ref=item.get("outputRef"),
            source_path=item.get("sourcePath"),
            source_image_ref=item.get("sourceImageRef"),
            rerun_input_ref=item.get("rerunInputRef"),
            last_rerun_source=item.get("lastRerunSource"),
            masked_regions=int(item.get("maskedRegions") or 0),
            error=item.get("error"),
            matches=item.get("matches") or [],
            note=item.get("note"),
            updated_at=item.get("updatedAt"),
        )
        for item in images
    ], mode)
    make_zip(job_dir)
    meta = read_json_file(job_dir / "job.json", {})
    return result_payload(job_dir, output_path, meta.get("source") or "历史任务", meta.get("notices") or [], persist=False)


def find_first_url(value: Any) -> str | None:
    if isinstance(value, str):
        match = re.search(r"https?://[^\s\"'<>]+", value)
        return match.group(0) if match else None
    if isinstance(value, dict):
        for preferred in ("url", "imageUrl", "imgUrl", "location", "src"):
            found = find_first_url(value.get(preferred))
            if found:
                return found
        for item in value.values():
            found = find_first_url(item)
            if found:
                return found
    if isinstance(value, list):
        for item in value:
            found = find_first_url(item)
            if found:
                return found
    return None


def csdn_upload_token(cookie: str, suffix: str) -> dict[str, Any]:
    suffix = suffix.lstrip(".").lower() or "png"
    response = requests.get(
        "https://imgservice.csdn.net/direct/v1.0/image/obs/upload",
        params={
            "type": "blog",
            "rtype": "blog_picture",
            "x-image-template": "standard",
            "x-image-app": "direct_blog",
            "x-image-dir": "direct",
            "x-image-suffix": suffix,
        },
        headers={
            "accept": "*/*",
            "content-type": "application/json",
            "origin": "https://mp.csdn.net",
            "referer": "https://mp.csdn.net/mp_blog/creation/editor",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
            "cookie": cookie,
        },
        timeout=45,
    )
    response.raise_for_status()
    data = response.json()
    token = data.get("data") if isinstance(data, dict) else None
    if not isinstance(token, dict):
        raise UserVisibleError("CSDN 没有返回图片上传凭证，请检查 Cookie 是否有效。")
    return token


def csdn_upload_image(cookie: str, image_path: Path) -> str:
    suffix = image_path.suffix.lower().lstrip(".") or "png"
    token = csdn_upload_token(cookie, suffix)
    custom = token.get("customParam") or {}
    fields = {
        "key": token.get("filePath", ""),
        "policy": token.get("policy", ""),
        "AccessKeyId": token.get("accessId", ""),
        "signature": token.get("signature", ""),
        "callbackUrl": token.get("callbackUrl", ""),
        "callbackBody": token.get("callbackBody", ""),
        "callbackBodyType": token.get("callbackBodyType", ""),
        "x:rtype": custom.get("rtype", "blog_picture"),
        "x:watermark": custom.get("watermark") or custom.get("rtype", "blog_picture"),
        "x:templateName": custom.get("templateName") or custom.get("rtype", "blog_picture"),
        "x:filePath": token.get("filePath", ""),
        "x:isAudit": custom.get("isAudit", "false"),
        "x:x-image-app": custom.get("x-image-app", "direct_blog"),
        "x:type": custom.get("type", "blog"),
        "x:x-image-suffix": custom.get("x-image-suffix", suffix),
        "x:username": custom.get("username", ""),
    }
    mime = mime_for_path(image_path)
    with image_path.open("rb") as handle:
        response = requests.post(
            "https://csdn-img-blog.obs.cn-north-4.myhuaweicloud.com/",
            data=fields,
            files={"file": (image_path.name, handle, mime)},
            headers={
                "accept": "application/json, text/javascript, */*; q=0.01",
                "origin": "https://mp.csdn.net",
                "referer": "https://mp.csdn.net/mp_blog/creation/editor",
                "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
            },
            timeout=120,
        )
    response.raise_for_status()
    try:
        data = response.json()
    except Exception as exc:  # noqa: BLE001
        raise UserVisibleError(f"CSDN 图片上传返回内容无法解析：{response.text[:200]}") from exc
    image_url = find_first_url(data)
    if not image_url:
        raise UserVisibleError("CSDN 图片已请求上传，但没有返回可用图片地址。")
    return image_url


def prepare_csdn_native(job_dir: Path, cookie: str) -> dict[str, Any]:
    cookie = (cookie or "").strip()
    if not cookie:
        raise UserVisibleError("请先在设置里填写 CSDN Cookie，才能使用 CSDN 原生图片上传。")
    output_path = find_output_path(job_dir)
    output_dir = job_dir / "output"
    content = read_text(output_path)
    refs = [ref for ref in collect_image_refs(content) if not ref.startswith("data:") and not re.match(r"^https?://", ref)]
    replacements: dict[str, str] = {}
    uploads: list[dict[str, str]] = []
    for ref in refs:
        candidate = (output_dir / ref).resolve()
        if not candidate.exists() or output_dir.resolve() not in candidate.parents:
            continue
        url = csdn_upload_image(cookie, candidate)
        replacements[ref] = url
        uploads.append({"ref": ref, "url": url})
    if not uploads:
        raise UserVisibleError("没有找到需要上传到 CSDN 的本地图片。")
    updated = replace_refs(content, replacements)
    ready_path = output_path.with_name(f"{output_path.stem}_csdn_ready{output_path.suffix}")
    write_text(ready_path, updated)
    meta = read_json_file(job_dir / "job.json", {})
    result = result_payload(
        job_dir,
        ready_path,
        f"{meta.get('source') or '文章'} · CSDN 原生图片",
        meta.get("notices") or [],
        persist=True,
    )
    result["csdnUploads"] = uploads
    return result


@app.get("/")
def home():
    return send_from_directory(APP_DIR / "static", "index.html")


@app.post("/api/cpa/models")
def cpa_models():
    try:
        payload = request.get_json(force=True, silent=False)
        response = requests.get(
            cpa_url(payload.get("baseUrl") or DEFAULT_BASE_URL, "models"),
            headers=cpa_headers(payload.get("apiKey") or os.environ.get("CPA_API_KEY", "")),
            timeout=45,
        )
        response.raise_for_status()
        data = response.json()
        models = [item.get("id") for item in data.get("data", []) if item.get("id")]
        return jsonify({"ok": True, "models": models, "rawCount": len(models)})
    except UserVisibleError as exc:
        return json_error(str(exc), 400)
    except Exception as exc:  # noqa: BLE001
        return json_error(f"获取模型失败：{exc}", 500)


@app.post("/api/cpa/test")
def cpa_test():
    try:
        payload = request.get_json(force=True, silent=False)
        api_key = payload.get("apiKey") or os.environ.get("CPA_API_KEY", "")
        base_url = payload.get("baseUrl") or DEFAULT_BASE_URL
        model = payload.get("model") or DEFAULT_MODEL

        models_response = requests.get(cpa_url(base_url, "models"), headers=cpa_headers(api_key), timeout=45)
        models_response.raise_for_status()
        chat_response = requests.post(
            cpa_url(base_url, "chat/completions"),
            headers=cpa_headers(api_key),
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": "你只返回 OK。"},
                    {"role": "user", "content": "连接测试，请返回 OK。"},
                ],
                "temperature": 0,
            },
            timeout=60,
        )
        chat_response.raise_for_status()
        return jsonify({"ok": True, "message": f"连接成功，模型 {model} 可以响应。"})
    except UserVisibleError as exc:
        return json_error(str(exc), 400)
    except Exception as exc:  # noqa: BLE001
        return json_error(f"测试连接失败：{exc}", 500)


@app.get("/api/csdn/cookie")
def csdn_cookie():
    try:
        cookie = try_read_csdn_cookie()
        return jsonify({"ok": True, "cookie": cookie, "message": "已从本机浏览器读取到 CSDN Cookie。"})
    except UserVisibleError as exc:
        return json_error(str(exc), 400)
    except Exception as exc:  # noqa: BLE001
        return json_error(f"读取 CSDN Cookie 失败：{exc}", 500)


@app.get("/api/industry-prompts")
def industry_prompts_list():
    try:
        return jsonify({"ok": True, "path": str(prompt_dir()), "prompts": list_prompt_files()})
    except UserVisibleError as exc:
        return json_error(str(exc), 400)
    except Exception as exc:  # noqa: BLE001
        return json_error(f"读取行业提示词失败：{exc}", 500)


@app.post("/api/industry-prompts/path")
def industry_prompts_path():
    try:
        payload = request.get_json(force=True, silent=False)
        raw_path = (payload.get("path") or "").strip()
        if not raw_path:
            raise UserVisibleError("请填写行业提示词存放路径")
        target = Path(raw_path).expanduser()
        target.mkdir(parents=True, exist_ok=True)
        write_text(PROMPTS_CONFIG_PATH, json.dumps({"path": str(target)}, ensure_ascii=False, indent=2))
        return jsonify({"ok": True, "path": str(target), "prompts": list_prompt_files()})
    except UserVisibleError as exc:
        return json_error(str(exc), 400)
    except Exception as exc:  # noqa: BLE001
        return json_error(f"保存行业提示词路径失败：{exc}", 500)


@app.post("/api/industry-prompts/save")
def industry_prompts_save():
    try:
        payload = request.get_json(force=True, silent=False)
        name = safe_prompt_name(payload.get("name") or "")
        content = (payload.get("content") or "").strip()
        if not content:
            raise UserVisibleError("行业提示词内容不能为空")
        root = prompt_dir()
        root.mkdir(parents=True, exist_ok=True)
        suffix = Path(name).suffix.lower()
        filename = name if suffix in {".txt", ".md", ".json"} else f"{name}.md"
        path = root / filename
        write_text(path, content + "\n")
        return jsonify({"ok": True, "path": str(path), "prompts": list_prompt_files()})
    except UserVisibleError as exc:
        return json_error(str(exc), 400)
    except Exception as exc:  # noqa: BLE001
        return json_error(f"保存行业提示词失败：{exc}", 500)


@app.get("/api/lark/auth/status")
def lark_auth_status():
    try:
        verify = request.args.get("verify") == "1"
        args = ["auth", "status"]
        if verify:
            args.append("--verify")
        data = run_lark_json(args, timeout=60)
        return jsonify({"ok": True, "status": safe_auth_status(data), "raw": data})
    except UserVisibleError as exc:
        return json_error(str(exc), 400)
    except Exception as exc:  # noqa: BLE001
        return json_error(f"检查飞书账号失败：{exc}", 500)


@app.get("/api/lark/auth/list")
def lark_auth_list():
    try:
        data = run_lark_json(["auth", "list"], timeout=60)
        users = data if isinstance(data, list) else data.get("users", [])
        return jsonify({"ok": True, "users": users, "raw": data})
    except UserVisibleError as exc:
        return json_error(str(exc), 400)
    except Exception as exc:  # noqa: BLE001
        return json_error(f"读取飞书账号列表失败：{exc}", 500)


@app.get("/api/lark/profile/list")
def lark_profile_list():
    try:
        data = run_lark_json(["profile", "list"], timeout=60)
        profiles = data if isinstance(data, list) else data.get("profiles", [])
        return jsonify({"ok": True, "profiles": profiles, "raw": data})
    except UserVisibleError as exc:
        return json_error(str(exc), 400)
    except Exception as exc:  # noqa: BLE001
        return json_error(f"读取飞书身份槽位失败：{exc}", 500)


@app.post("/api/lark/profile/use")
def lark_profile_use():
    try:
        payload = request.get_json(force=True, silent=False)
        name = (payload.get("profile") or "").strip()
        if not name:
            raise UserVisibleError("请先选择要使用的飞书身份槽位")
        completed = run_lark_cli(["profile", "use", name], timeout=60)
        raw = (completed.stdout or "") + ("\n" + completed.stderr if completed.stderr else "")
        if completed.returncode != 0:
            raise UserVisibleError(raw.strip() or "切换飞书身份槽位失败")
        return jsonify({"ok": True, "message": raw.strip() or f"已切换到 {name}"})
    except UserVisibleError as exc:
        return json_error(str(exc), 400)
    except Exception as exc:  # noqa: BLE001
        return json_error(f"切换飞书身份槽位失败：{exc}", 500)


@app.post("/api/lark/auth/login/start")
def lark_auth_login_start():
    try:
        payload = request.get_json(force=True, silent=False)
        domains = payload.get("domains") or ["docs", "wiki", "drive"]
        scope = (payload.get("scope") or "").strip()
        recommend = bool_from_form(payload.get("recommend"))
        args = ["auth", "login", "--no-wait", "--json"]
        if recommend:
            args.append("--recommend")
        if scope:
            args.extend(["--scope", scope])
        else:
            for domain in domains:
                domain = str(domain).strip()
                if domain:
                    args.extend(["--domain", domain])
        data = run_lark_json(args, timeout=60)
        return jsonify({"ok": True, "auth": data})
    except UserVisibleError as exc:
        return json_error(str(exc), 400)
    except Exception as exc:  # noqa: BLE001
        return json_error(f"发起飞书授权失败：{exc}", 500)


@app.post("/api/lark/auth/login/complete")
def lark_auth_login_complete():
    try:
        payload = request.get_json(force=True, silent=False)
        device_code = (payload.get("deviceCode") or "").strip()
        if not device_code:
            raise UserVisibleError("请先发起授权，拿到 device_code 后再完成授权")
        completed = run_lark_cli(["auth", "login", "--device-code", device_code], timeout=180)
        raw = (completed.stdout or "") + ("\n" + completed.stderr if completed.stderr else "")
        if completed.returncode != 0:
            raise UserVisibleError(raw.strip() or "飞书授权未完成")
        return jsonify({"ok": True, "message": raw.strip() or "飞书授权完成"})
    except UserVisibleError as exc:
        return json_error(str(exc), 400)
    except Exception as exc:  # noqa: BLE001
        return json_error(f"完成飞书授权失败：{exc}", 500)


def payload_from_upload_form() -> dict[str, Any]:
    return {
        "mode": request.form.get("mode", "local"),
        "terms": request.form.get("terms", ""),
        "apiKey": request.form.get("apiKey", ""),
        "baseUrl": request.form.get("baseUrl", DEFAULT_BASE_URL),
        "model": request.form.get("model", DEFAULT_MODEL),
        "matchMode": request.form.get("matchMode", "fuzzy"),
        "maskAllText": request.form.get("maskAllText", ""),
        "enhanceWithPrompt": request.form.get("enhanceWithPrompt", ""),
        "industryPromptId": request.form.get("industryPromptId", ""),
    }


@app.post("/api/process-lark")
def process_lark():
    try:
        payload = request.get_json(force=True, silent=False)
        job_dir = make_job_dir()
        content, notices = fetch_lark_markdown(payload.get("docUrl") or "")
        source = job_dir / "input" / "lark_source.md"
        write_text(source, content)
        return jsonify(process_source(job_dir, source, payload, "飞书文档", notices))
    except UserVisibleError as exc:
        return json_error(str(exc), 400)
    except Exception as exc:  # noqa: BLE001
        return json_error(f"处理失败：{exc}", 500)


@app.post("/api/process-lark-stream")
def process_lark_stream():
    payload = request.get_json(force=True, silent=False)

    def task(emit):
        emit("已收到飞书处理请求，准备创建任务。")
        job_dir = make_job_dir()
        job_id = job_id_from_dir(job_dir)
        emit(f"本地任务已创建：{job_id}", "job_created", {"jobId": job_id})
        content, notices = fetch_lark_markdown(payload.get("docUrl") or "", emit)
        source = job_dir / "input" / "lark_source.md"
        write_text(source, content)
        emit("飞书正文已保存到本地临时草稿。")
        emit("开始识别图片并执行脱敏。")
        return process_source(job_dir, source, payload, "飞书文档", notices, progress=emit)

    return stream_task(task)


@app.post("/api/process-upload")
def process_upload():
    try:
        payload = payload_from_upload_form()
        job_dir = make_job_dir()
        source = combine_uploaded_files(request.files.getlist("files"), job_dir)
        return jsonify(process_source(job_dir, source, payload, "本地上传文件"))
    except UserVisibleError as exc:
        return json_error(str(exc), 400)
    except Exception as exc:  # noqa: BLE001
        return json_error(f"处理失败：{exc}", 500)


@app.post("/api/process-upload-stream")
def process_upload_stream():
    payload = payload_from_upload_form()
    files = request.files.getlist("files")
    if not files:
        return json_error("请至少上传一个文件", 400)
    job_dir = make_job_dir()
    saved_files = save_uploaded_files(files, job_dir)

    def task(emit):
        emit("已收到上传文件，准备创建任务。")
        job_id = job_id_from_dir(job_dir)
        emit(f"本地任务已创建：{job_id}", "job_created", {"jobId": job_id})
        source = combine_saved_files(saved_files, job_dir)
        emit("文件已保存并转换为可处理文章。")
        emit("开始识别图片并执行脱敏。")
        return process_source(job_dir, source, payload, "本地上传文件", progress=emit)

    return stream_task(task)


@app.get("/api/jobs/<job_id>/file/<path:filename>")
def job_file(job_id: str, filename: str):
    try:
        job_dir = get_job_dir(job_id)
        output_dir = (job_dir / "output").resolve()
        target = (output_dir / filename).resolve()
        if output_dir not in target.parents and target != output_dir:
            return json_error("文件路径无效", 400)
        if not target.exists() or not target.is_file():
            return json_error("文件不存在", 404)
        return send_from_directory(output_dir, filename)
    except UserVisibleError as exc:
        return json_error(str(exc), 404)


@app.get("/api/jobs/<job_id>/result")
def job_result(job_id: str):
    try:
        job_dir = get_job_dir(job_id)
        meta_path = job_dir / "job.json"
        meta = json.loads(read_text(meta_path)) if meta_path.exists() else {}
        output_path = find_output_path(job_dir)
        return jsonify(
            result_payload(
                job_dir,
                output_path,
                meta.get("source") or "历史任务",
                meta.get("notices") or [],
                persist=False,
            )
        )
    except UserVisibleError as exc:
        return json_error(str(exc), 404)
    except Exception as exc:  # noqa: BLE001
        return json_error(f"恢复缓存失败：{exc}", 500)


@app.post("/api/jobs/<job_id>/pause")
def job_pause(job_id: str):
    try:
        get_job_dir(job_id)
        set_job_paused(job_id, True)
        return jsonify({"ok": True, "message": "任务已暂停。当前正在处理的图片会先收尾，下一张图开始前会等待。"})
    except UserVisibleError as exc:
        return json_error(str(exc), 404)


@app.post("/api/jobs/<job_id>/resume")
def job_resume(job_id: str):
    try:
        get_job_dir(job_id)
        set_job_paused(job_id, False)
        return jsonify({"ok": True, "message": "任务已继续。"})
    except UserVisibleError as exc:
        return json_error(str(exc), 404)


@app.post("/api/jobs/<job_id>/images/<int:image_index>/rerun")
def job_image_rerun(job_id: str, image_index: int):
    try:
        job_dir = get_job_dir(job_id)
        payload = request.get_json(force=True, silent=False)
        return jsonify(rerun_single_image(job_dir, image_index, payload))
    except UserVisibleError as exc:
        return json_error(str(exc), 400)
    except Exception as exc:  # noqa: BLE001
        return json_error(f"重新识别失败：{exc}", 500)


@app.post("/api/jobs/<job_id>/csdn/prepare")
def job_csdn_prepare(job_id: str):
    try:
        job_dir = get_job_dir(job_id)
        payload = request.get_json(force=True, silent=False)
        return jsonify(prepare_csdn_native(job_dir, payload.get("cookie") or ""))
    except UserVisibleError as exc:
        return json_error(str(exc), 400)
    except Exception as exc:  # noqa: BLE001
        return json_error(f"CSDN 原生图片准备失败：{exc}", 500)


@app.get("/api/jobs/<job_id>/download")
def download_job(job_id: str):
    try:
        job_dir = get_job_dir(job_id)
        zip_path = make_zip(job_dir)
        return send_file(zip_path, as_attachment=True, download_name="csdn_safe_package.zip")
    except UserVisibleError as exc:
        return json_error(str(exc), 404)


@app.get("/api/health")
def health():
    lark_cli = resolve_lark_cli()
    version = ""
    if lark_cli:
        try:
            version_result = run_lark_cli(["--version"], timeout=20)
            version = (version_result.stdout or version_result.stderr or "").strip()
        except Exception:
            version = ""
    return jsonify(
        {
            "ok": True,
            "python": sys.version.split()[0],
            "larkCli": lark_cli,
            "larkCliVersion": version,
            "csdnEditorUrl": CSDN_EDITOR_URL,
            "appFile": str(Path(__file__).resolve()),
            "routeCount": len(list(app.url_map.iter_rules())),
        }
    )


def main() -> int:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    print("CSDN image privacy web tool")
    print("Open: http://127.0.0.1:8866")
    app.run(host="127.0.0.1", port=8866, debug=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
