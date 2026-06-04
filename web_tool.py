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
from urllib.parse import quote, unquote, urlparse

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
    extract_redaction_terms_from_instruction,
    is_additive_instruction,
    is_data_image,
    is_no_mask_instruction,
    is_strict_only_instruction,
    is_url,
    looks_like_image,
    load_terms,
    process_document,
    process_image,
    read_text,
    replace_refs,
    resolve_local_image,
    write_report,
    write_report_json,
    write_text,
)


APP_DIR = Path(__file__).resolve().parent
RUNS_DIR = APP_DIR / "runs"
PROMPTS_CONFIG_PATH = APP_DIR / "industry_prompts.json"
DEFAULT_PROMPTS_DIR = APP_DIR / "industry_prompts"
CSDN_CREATION_URL = "https://mp.csdn.net/mp_blog/creation/editor"
CSDN_LEGACY_MD_URL = "https://editor.csdn.net/md/"
CSDN_EDITOR_URL = CSDN_CREATION_URL
CSDN_CDP_DEFAULT_PORT = 9222
CSDN_CDP_HOSTS = ["127.0.0.1", "localhost", "[::1]"]
CSDN_COOKIE_URLS = [
    "https://www.csdn.net/",
    "https://blog.csdn.net/",
    "https://mp.csdn.net/",
    CSDN_CREATION_URL,
    CSDN_LEGACY_MD_URL,
    "https://passport.csdn.net/",
    "https://imgservice.csdn.net/direct/v1.0/image/obs/upload",
    "https://imgservice.csdn.net/v1/image/direct/upload/signature",
]

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
    missing_scopes = error.get("missing_scopes") or []
    hint = ""
    if error.get("subtype") == "missing_scope" and missing_scopes:
        scope_text = " ".join(str(item) for item in missing_scopes)
        if any(str(item).startswith("drive:") for item in missing_scopes):
            hint = (
                "这是飞书云空间文件能力缺权限，不是文档正文权限问题。请到“设置 → 飞书账号 → 高级权限编号”填入："
                f"{scope_text}，重新生成企业账号授权链接并完成授权。"
            )
        else:
            hint = f"请在飞书账号设置里的“高级权限编号”填入：{scope_text}，重新授权后再试。"
    elif code == 330004:
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


def lark_url_path(value: str) -> str:
    match = re.search(r"https?://[^/]+(?P<path>/[^\s?#]*)", value or "", flags=re.I)
    return match.group("path") if match else ""


def is_lark_file_url(value: str) -> bool:
    return bool(re.search(r"/file/[^/?#\s]+", lark_url_path(value), flags=re.I))


def extract_lark_file_token(value: str) -> str:
    match = re.search(r"/file/(?P<token>[^/?#\s]+)", lark_url_path(value), flags=re.I)
    if not match:
        raise UserVisibleError("这不是飞书云空间文件链接。请粘贴 /file/ 开头的完整飞书文件链接。")
    return unquote(match.group("token")).strip()


def lark_file_scope_hint() -> str:
    scopes = "drive:drive.metadata:readonly drive:file:download"
    return (
        "这是飞书云空间文件链接，不是在线文档链接。请在右上角“设置 → 飞书账号 → 高级权限编号”填入："
        f"{scopes}，重新生成企业账号授权链接并完成授权；或者先把文件下载到本地，再从“本地文件”上传。"
    )


def safe_download_name(name: str, fallback: str) -> str:
    cleaned = secure_filename(name or "")
    if not cleaned:
        cleaned = fallback
    if len(cleaned) > 150:
        stem = Path(cleaned).stem[:110] or fallback
        suffix = Path(cleaned).suffix[:20]
        cleaned = f"{stem}{suffix}"
    return cleaned


def find_nested_value(value: Any, keys: set[str]) -> Any:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in keys and item not in (None, ""):
                return item
        for item in value.values():
            found = find_nested_value(item, keys)
            if found not in (None, ""):
                return found
    elif isinstance(value, list):
        for item in value:
            found = find_nested_value(item, keys)
            if found not in (None, ""):
                return found
    return None


def inspect_lark_file(doc_url: str, emit=None) -> dict[str, str]:
    if emit:
        emit("检测到飞书云空间文件链接，正在读取文件信息。")
    completed = run_lark_cli(
        ["drive", "+inspect", "--as", "user", "--url", doc_url, "--format", "json"],
        timeout=120,
    )
    raw = (completed.stdout or "") + ("\n" + completed.stderr if completed.stderr else "")
    if completed.returncode != 0:
        detail = friendly_cli_error(raw, "飞书云空间文件信息读取失败")
        if "missing_scope" in raw:
            detail = f"{detail}\n\n{lark_file_scope_hint()}"
        raise UserVisibleError(detail)
    try:
        data = parse_cli_json_any(raw)
    except json.JSONDecodeError as exc:
        raise UserVisibleError(f"飞书云空间文件信息返回内容不是标准 JSON：{exc}") from exc
    token = str(
        find_nested_value(data, {"file_token", "fileToken", "token", "doc_token", "obj_token", "objToken"})
        or extract_lark_file_token(doc_url)
    )
    title = str(find_nested_value(data, {"title", "name", "file_name", "fileName"}) or token)
    doc_type = str(find_nested_value(data, {"doc_type", "docType", "type", "obj_type", "objType"}) or "file")
    return {"token": token, "title": title, "type": doc_type}


def detect_download_suffix(path: Path) -> str:
    head = path.read_bytes()[:16] if path.exists() else b""
    if head.startswith(b"%PDF"):
        return ".pdf"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if head.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if head.startswith(b"GIF8"):
        return ".gif"
    if head.startswith(b"RIFF") and b"WEBP" in head:
        return ".webp"
    if head.startswith(b"BM"):
        return ".bmp"
    if head.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        return ".doc"
    if head.startswith(b"PK"):
        try:
            with zipfile.ZipFile(path) as archive:
                names = set(archive.namelist())
                if "[Content_Types].xml" in names and any(item.startswith("word/") for item in names):
                    return ".docx"
        except zipfile.BadZipFile:
            pass
    return ""


def download_lark_file(doc_url: str, job_dir: Path, emit=None) -> tuple[Path, list[str]]:
    meta = inspect_lark_file(doc_url, emit)
    token = meta["token"]
    title = meta["title"]
    filename = safe_download_name(title, f"{token}.download")
    target = job_dir / "input" / filename
    if not target.suffix:
        target = target.with_suffix(".download")
    target.parent.mkdir(parents=True, exist_ok=True)
    if emit:
        emit(f"正在从飞书云空间下载文件：{title}")
    completed = run_lark_cli(
        ["drive", "+download", "--as", "user", "--file-token", token, "--output", str(target), "--overwrite"],
        timeout=300,
    )
    raw = (completed.stdout or "") + ("\n" + completed.stderr if completed.stderr else "")
    if completed.returncode != 0:
        detail = friendly_cli_error(raw, "飞书云空间文件下载失败")
        if "missing_scope" in raw:
            detail = f"{detail}\n\n{lark_file_scope_hint()}"
        raise UserVisibleError(detail)
    if not target.exists() or not target.is_file():
        raise UserVisibleError("飞书 CLI 显示下载完成，但本地没有找到下载后的文件。请重试或改用本地上传。")
    detected_suffix = detect_download_suffix(target)
    if detected_suffix and target.suffix.lower() in {"", ".download", ".bin"}:
        renamed = target.with_suffix(detected_suffix)
        if renamed.exists():
            renamed = target.with_name(f"{target.stem}_{uuid.uuid4().hex[:8]}{detected_suffix}")
        target.rename(renamed)
        target = renamed
    if target.suffix.lower() not in {".md", ".markdown", ".html", ".htm", ".docx", ".doc", ".pdf", *IMAGE_SUFFIXES}:
        raise UserVisibleError(
            f"飞书文件已下载，但当前工具暂不支持这个文件类型：{target.name}。请先另存为 Word、PDF、Markdown、HTML 或图片后再上传。"
        )
    if emit:
        emit(f"飞书云空间文件已下载：{target.name}。接下来按本地文件继续处理。")
    notice = "来源是飞书云空间文件，已先下载到本地临时任务目录再处理。"
    return target, [notice]


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
    for document_file in document_files:
        copy_relative_image_assets(document_file, input_dir)
    fragments = [file_to_html(path, media_dir) for path in files_to_render]

    source = input_dir / "uploaded_source.html"
    html_doc = "<!doctype html><html><head><meta charset=\"utf-8\"></head><body>\n"
    html_doc += "\n<hr>\n".join(fragments)
    html_doc += "\n</body></html>\n"
    write_text(source, html_doc)
    return source


def copy_relative_image_assets(document_path: Path, input_dir: Path) -> None:
    if document_path.suffix.lower() not in {".md", ".markdown", ".html", ".htm"}:
        return

    content = read_text(document_path)
    doc_dir = document_path.parent
    input_root = input_dir.resolve()
    for ref in collect_image_refs(content):
        if not looks_like_image(ref) or is_data_image(ref) or is_url(ref):
            continue
        cleaned = unquote(ref.split("#", 1)[0].split("?", 1)[0]).strip()
        if not cleaned:
            continue
        destination = (input_dir / cleaned).resolve()
        if destination == input_root or input_root not in destination.parents:
            continue
        local_path = resolve_local_image(ref, doc_dir)
        if not local_path or not local_path.is_file():
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not destination.exists() or local_path.resolve() != destination:
            shutil.copy2(local_path, destination)


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
    # Keep result payload light. Large articles with dozens of screenshots can
    # produce multi-megabyte data-URI HTML, which makes restore/copy brittle.
    clipboard_html = content if output_path.suffix.lower() in {".html", ".htm"} else markdown_to_clipboard_html(content)
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


SUPPORTED_COOKIE_BROWSERS = {
    "chrome": {
        "label": "Google Chrome",
    },
    "edge": {
        "label": "Microsoft Edge",
    },
}


def cookie_browser_label(browser: str) -> str:
    return SUPPORTED_COOKIE_BROWSERS.get(browser, SUPPORTED_COOKIE_BROWSERS["chrome"])["label"]


def normalize_cookie_browser(browser: str | None) -> str:
    value = (browser or "chrome").strip().lower()
    return value if value in SUPPORTED_COOKIE_BROWSERS else "chrome"


def powershell_quote(value: str | Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def normalize_cdp_port(value: Any) -> int:
    if value in (None, ""):
        return CSDN_CDP_DEFAULT_PORT
    try:
        port = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise UserVisibleError("DevTools 端口必须是数字，例如 9222。") from exc
    if not 1 <= port <= 65535:
        raise UserVisibleError("DevTools 端口必须在 1 到 65535 之间。")
    return port


def cdp_json_for_host(host: str, port: int, path: str, method: str = "GET", timeout: float = 3) -> Any:
    response = requests.request(method, f"http://{host}:{port}{path}", timeout=timeout)
    response.raise_for_status()
    return response.json()


def cdp_json(port: int, path: str, method: str = "GET", timeout: float = 3) -> Any:
    errors: list[str] = []
    for host in CSDN_CDP_HOSTS:
        try:
            return cdp_json_for_host(host, port, path, method=method, timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{host}: {type(exc).__name__}: {exc}")
    raise UserVisibleError("没有连上 CSDN 调试浏览器。连接细节：" + "；".join(errors[:3]))


def iter_cdp_targets(port: int, timeout: float = 1) -> list[tuple[str, list[dict[str, Any]]]]:
    found: list[tuple[str, list[dict[str, Any]]]] = []
    for host in CSDN_CDP_HOSTS:
        try:
            targets = cdp_json_for_host(host, port, "/json/list", timeout=timeout)
        except Exception:
            continue
        if isinstance(targets, list):
            found.append((host, [item for item in targets if isinstance(item, dict)]))
    return found


def target_is_csdn_page(item: dict[str, Any]) -> bool:
    return (
        item.get("type") == "page"
        and item.get("webSocketDebuggerUrl")
        and "csdn.net" in str(item.get("url") or "")
    )


def resolve_csdn_cdp_port(value: Any = None) -> int:
    preferred = normalize_cdp_port(value)
    candidates = [preferred] + [port for port in range(9222, 9231) if port != preferred]
    for port in candidates:
        host_targets = iter_cdp_targets(port, timeout=0.8)
        if any(target_is_csdn_page(item) for _, targets in host_targets for item in targets):
            return port
        if any(item.get("type") == "page" and item.get("webSocketDebuggerUrl") for _, targets in host_targets for item in targets):
            return port
    raise UserVisibleError("没有找到已打开的 CSDN 自动浏览器。请先点“打开 CSDN 自动浏览器”，并完成登录。")


def cdp_page_websocket_url(port: int) -> str:
    host_targets = iter_cdp_targets(port, timeout=2)
    if not host_targets:
        raise UserVisibleError(
            "没有连上 CSDN 调试浏览器。请先点“打开 CSDN 自动浏览器”，在打开的浏览器里登录 CSDN，"
            "然后再点“检查自动登录”。"
        )
    for _, targets in host_targets:
        for item in targets:
            if target_is_csdn_page(item):
                return str(item["webSocketDebuggerUrl"])
    for _, targets in host_targets:
        for item in targets:
            if item.get("type") == "page" and item.get("webSocketDebuggerUrl"):
                return str(item["webSocketDebuggerUrl"])

    encoded = quote(CSDN_CREATION_URL, safe="")
    for host, _ in host_targets:
        for method in ("PUT", "GET"):
            try:
                created = cdp_json_for_host(host, port, f"/json/new?{encoded}", method=method)
                ws_url = created.get("webSocketDebuggerUrl") if isinstance(created, dict) else None
                if ws_url:
                    return str(ws_url)
            except Exception:
                continue
    raise UserVisibleError("调试浏览器已打开，但没有找到可操作的页面。请在该浏览器里打开 CSDN 后重试。")


def websocket_origin(ws_url: str) -> str:
    parsed = urlparse(ws_url)
    return f"http://{parsed.netloc}" if parsed.netloc else f"http://localhost:{CSDN_CDP_DEFAULT_PORT}"


def cdp_cookie_header_from_cookies(cookies: list[dict[str, Any]]) -> str:
    chosen: dict[str, tuple[int, int, str]] = {}
    order = 0
    for cookie in cookies:
        if not isinstance(cookie, dict):
            continue
        domain = str(cookie.get("domain") or "").lower()
        name = str(cookie.get("name") or "").strip()
        value = str(cookie.get("value") or "")
        if not name or "csdn.net" not in domain:
            continue
        score = 0
        if name.lower() in CSDN_STRONG_AUTH_COOKIE_NAMES:
            score += 100
        if domain == ".csdn.net":
            score += 20
        elif domain.endswith(".csdn.net"):
            score += 10
        score += min(len(str(cookie.get("path") or "")), 20)
        if name not in chosen or score > chosen[name][0]:
            chosen[name] = (score, order, f"{name}={value}")
        order += 1
    return "; ".join(item for _, _, item in sorted(chosen.values(), key=lambda entry: entry[1]))


def read_csdn_cookie_from_cdp(port: int = CSDN_CDP_DEFAULT_PORT) -> str:
    port = resolve_csdn_cdp_port(port)
    try:
        import websocket  # type: ignore
    except Exception as exc:  # noqa: BLE001
        raise UserVisibleError("缺少浏览器调试连接组件，请重新启动工具让它安装依赖。") from exc

    ws_url = cdp_page_websocket_url(port)
    try:
        ws = websocket.create_connection(ws_url, timeout=8, origin=websocket_origin(ws_url))
    except Exception as exc:  # noqa: BLE001
        raise UserVisibleError(
            "已经找到调试浏览器，但连接被浏览器拒绝。请用页面上的“打开 CSDN 登录浏览器”按钮重新打开，"
            "登录后再读取。"
        ) from exc

    message_id = 0

    def send_cdp(method: str, params: dict[str, Any] | None = None, timeout: float = 8) -> dict[str, Any]:
        nonlocal message_id
        message_id += 1
        ws.send(json.dumps({"id": message_id, "method": method, "params": params or {}}))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            ws.settimeout(max(0.1, deadline - time.monotonic()))
            raw = ws.recv()
            payload = json.loads(raw)
            if payload.get("id") != message_id:
                continue
            if payload.get("error"):
                error = payload["error"]
                raise UserVisibleError(str(error.get("message") or error))
            return payload.get("result") or {}
        raise UserVisibleError("等待调试浏览器返回 Cookie 超时，请确认 CSDN 页面已经打开并完成登录。")

    try:
        send_cdp("Network.enable", timeout=5)
        result = send_cdp("Network.getCookies", {"urls": CSDN_COOKIE_URLS}, timeout=8)
        cookies = result.get("cookies") if isinstance(result, dict) else []
        if not cookies:
            try:
                result = send_cdp("Storage.getCookies", timeout=8)
                cookies = result.get("cookies") if isinstance(result, dict) else []
            except Exception:
                cookies = []
        cookie_header = cdp_cookie_header_from_cookies(cookies if isinstance(cookies, list) else [])
    finally:
        try:
            ws.close()
        except Exception:
            pass

    if not cookie_header:
        raise UserVisibleError(
            "调试浏览器里没有读到 CSDN Cookie。请确认刚打开的浏览器已经登录 CSDN，并至少打开过一次 CSDN 首页或创作中心。"
        )
    return cookie_header


def cdp_runtime_evaluate(port: int, expression: str, timeout: float = 120) -> Any:
    port = resolve_csdn_cdp_port(port)
    try:
        import websocket  # type: ignore
    except Exception as exc:  # noqa: BLE001
        raise UserVisibleError("缺少浏览器调试连接组件，请重新启动工具让它安装依赖，或先使用普通复制。") from exc

    ws_url = cdp_page_websocket_url(port)
    try:
        ws = websocket.create_connection(ws_url, timeout=8, origin=websocket_origin(ws_url))
    except Exception as exc:  # noqa: BLE001
        raise UserVisibleError("已经找到 CSDN 登录浏览器，但连接被拒绝。请重新打开登录浏览器后再试。") from exc

    message_id = 1
    try:
        ws.send(
            json.dumps(
                {
                    "id": message_id,
                    "method": "Runtime.evaluate",
                    "params": {
                        "expression": expression,
                        "awaitPromise": True,
                        "returnByValue": True,
                        "timeout": int(timeout * 1000),
                    },
                }
            )
        )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            ws.settimeout(max(0.1, deadline - time.monotonic()))
            payload = json.loads(ws.recv())
            if payload.get("id") != message_id:
                continue
            if payload.get("error"):
                error = payload["error"]
                raise UserVisibleError(str(error.get("message") or error))
            if payload.get("exceptionDetails"):
                detail = payload["exceptionDetails"]
                text = detail.get("text") or detail.get("exception", {}).get("description") or detail
                raise UserVisibleError(f"CSDN 页面执行上传失败：{text}")
            result = payload.get("result") or {}
            remote_value = result.get("result") if isinstance(result, dict) else {}
            if isinstance(remote_value, dict) and remote_value.get("subtype") == "error":
                raise UserVisibleError(str(remote_value.get("description") or "CSDN 页面执行上传失败。"))
            return remote_value.get("value") if isinstance(remote_value, dict) else None
    finally:
        try:
            ws.close()
        except Exception:
            pass
    raise UserVisibleError("等待 CSDN 登录浏览器上传图片超时。")


def csdn_upload_image_via_browser(port: int, image_path: Path) -> str:
    if not image_path.exists():
        raise UserVisibleError(f"图片不存在：{image_path.name}")
    mime = mime_for_path(image_path)
    encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
    payload = json.dumps({"name": image_path.name, "mime": mime, "base64": encoded}, ensure_ascii=False)
    expression = f"""
(async () => {{
  const input = {payload};
  function base64ToBlob(base64, mime) {{
    const binary = atob(base64);
    const chunkSize = 32768;
    const chunks = [];
    for (let offset = 0; offset < binary.length; offset += chunkSize) {{
      const slice = binary.slice(offset, offset + chunkSize);
      const bytes = new Uint8Array(slice.length);
      for (let i = 0; i < slice.length; i += 1) bytes[i] = slice.charCodeAt(i);
      chunks.push(bytes);
    }}
    return new Blob(chunks, {{ type: mime }});
  }}
  if (!window.csdn || !window.csdn.upload || typeof window.csdn.upload.uploadImg !== "function") {{
    throw new Error("CSDN 上传组件还没有加载完成。请确认登录浏览器停留在 CSDN 创作页，然后重试。");
  }}
  const blob = base64ToBlob(input.base64, input.mime);
  const file = new File([blob], input.name, {{ type: input.mime }});
  const response = await window.csdn.upload.uploadImg({{
    appName: "direct_blog",
    type: "blog",
    imageTemplate: "",
    file,
  }});
  const first = Array.isArray(response) ? response[0] : response;
  const data = first && first.data && first.data.data ? first.data.data : first && first.data ? first.data : first;
  return {{ ok: true, response, imageUrl: data && (data.imageUrl || data.url || data.imgUrl || data.location || data.src) }};
}})()
"""
    result = cdp_runtime_evaluate(port, expression, timeout=180)
    image_url = find_first_url(result)
    if not image_url:
        raise UserVisibleError("CSDN 页面上传图片成功返回异常，没有找到图片地址。")
    return image_url


def csdn_browser_upload_status(port: int) -> dict[str, Any]:
    expression = """
(() => ({
  href: location.href,
  title: document.title,
  hasCsdnUpload: Boolean(window.csdn && window.csdn.upload && typeof window.csdn.upload.uploadImg === "function"),
}))()
"""
    value = cdp_runtime_evaluate(port, expression, timeout=30)
    if not isinstance(value, dict):
        value = {}
    return value


def build_csdn_editor_injection_script(title: str, markdown_body: str, html_body: str | None = None) -> str:
    payload = json.dumps(
        {
            "title": title or "",
            "markdown": markdown_body or "",
            "html": html_body or markdown_to_clipboard_html(markdown_body or ""),
            "url": CSDN_CREATION_URL,
        },
        ensure_ascii=False,
    )
    return f"""
(async () => {{
  const input = {payload};
  const sleep = (ms) => new Promise(resolve => setTimeout(resolve, ms));
  const visible = (el) => {{
    if (!el) return false;
    const rect = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    return rect.width > 20 && rect.height > 10 && style.visibility !== "hidden" && style.display !== "none";
  }};
  const fire = (el) => {{
    for (const name of ["input", "change", "keyup", "blur"]) {{
      el.dispatchEvent(new Event(name, {{ bubbles: true }}));
    }}
  }};
  const setNativeValue = (el, value) => {{
    const proto = Object.getPrototypeOf(el);
    const descriptor = Object.getOwnPropertyDescriptor(proto, "value")
      || Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")
      || Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value");
    if (descriptor && descriptor.set) descriptor.set.call(el, value);
    else el.value = value;
    fire(el);
  }};
  const cleanText = (text) => String(text || "").replace(/\\s+/g, " ").trim();
  const titleSelectors = [
    "input[placeholder*='标题']",
    "textarea[placeholder*='标题']",
    "input#txtTitle",
    "input[name='title']",
    ".article-title input",
    ".editor-title input",
    ".title-input input"
  ];
  function setTitle() {{
    if (!input.title) return {{ ok: false, skipped: true }};
    const candidates = [];
    for (const selector of titleSelectors) {{
      candidates.push(...document.querySelectorAll(selector));
    }}
    const target = candidates.find(visible);
    if (!target) return {{ ok: false }};
    setNativeValue(target, input.title);
    return {{ ok: true, selector: target.id ? `#${{target.id}}` : target.tagName.toLowerCase() }};
  }}
  function setByCkeditor() {{
    const instances = window.CKEDITOR && window.CKEDITOR.instances ? Object.values(window.CKEDITOR.instances) : [];
    for (const instance of instances) {{
      if (instance && typeof instance.setData === "function") {{
        instance.setData(input.html);
        if (typeof instance.updateElement === "function") instance.updateElement();
        return {{ ok: true, method: "CKEditor" }};
      }}
    }}
    return {{ ok: false }};
  }}
  function setByCkeIframe() {{
    const frames = Array.from(document.querySelectorAll("iframe.cke_wysiwyg_frame, #cke_editor iframe"));
    for (const frame of frames) {{
      try {{
        if (!visible(frame)) continue;
        const doc = frame.contentDocument || (frame.contentWindow && frame.contentWindow.document);
        const target = doc && doc.body;
        if (!target) continue;
        target.innerHTML = input.html;
        target.dispatchEvent(new Event("input", {{ bubbles: true }}));
        target.dispatchEvent(new Event("change", {{ bubbles: true }}));
        return {{ ok: true, method: "CKEditor iframe" }};
      }} catch (err) {{
        // Cross-origin or not ready; try the next strategy.
      }}
    }}
    return {{ ok: false }};
  }}
  function setByCodeMirror() {{
    for (const el of document.querySelectorAll(".CodeMirror")) {{
      if (visible(el) && el.CodeMirror && typeof el.CodeMirror.setValue === "function") {{
        el.CodeMirror.setValue(input.markdown);
        if (typeof el.CodeMirror.refresh === "function") el.CodeMirror.refresh();
        return {{ ok: true, method: "CodeMirror" }};
      }}
    }}
    return {{ ok: false }};
  }}
  function setByMonaco() {{
    const monacoEditor = window.monaco && window.monaco.editor;
    const editors = monacoEditor && typeof monacoEditor.getEditors === "function" ? monacoEditor.getEditors() : [];
    if (editors && editors.length && typeof editors[0].setValue === "function") {{
      editors[0].setValue(input.markdown);
      return {{ ok: true, method: "Monaco" }};
    }}
    return {{ ok: false }};
  }}
  function setByTextarea() {{
    const textareas = Array.from(document.querySelectorAll("textarea"))
      .filter(el => {{
        if (!visible(el)) return false;
        const label = el.placeholder || el.name || el.id || "";
        if (/标题|title|摘要|概要|summary|标签|tag|分类|category/i.test(label)) return false;
        const rect = el.getBoundingClientRect();
        return rect.width > 400 && rect.height > 180;
      }})
      .sort((a, b) => (b.getBoundingClientRect().width * b.getBoundingClientRect().height) - (a.getBoundingClientRect().width * a.getBoundingClientRect().height));
    const target = textareas[0];
    if (!target) return {{ ok: false }};
    setNativeValue(target, input.markdown);
    return {{ ok: true, method: "textarea" }};
  }}
  function setByContentEditable() {{
    const editables = Array.from(document.querySelectorAll("[contenteditable='true'], [contenteditable='plaintext-only']"))
      .filter(visible)
      .sort((a, b) => (b.getBoundingClientRect().width * b.getBoundingClientRect().height) - (a.getBoundingClientRect().width * a.getBoundingClientRect().height));
    const target = editables.find(el => !/标题|title/i.test(cleanText(el.getAttribute("aria-label") || el.getAttribute("placeholder") || "")));
    if (!target) return {{ ok: false }};
    target.focus();
    target.innerHTML = input.html;
    fire(target);
    return {{ ok: true, method: "contenteditable" }};
  }}
  function trySetKnownWindowEditor() {{
    const seen = new Set();
    for (const key of Object.keys(window)) {{
      if (!/editor|markdown|md/i.test(key)) continue;
      const candidate = window[key];
      if (!candidate || seen.has(candidate)) continue;
      seen.add(candidate);
      for (const method of ["setValue", "setMarkdown", "setContent", "insertValue"]) {{
        if (typeof candidate[method] === "function") {{
          candidate[method](method.toLowerCase().includes("markdown") ? input.markdown : input.html);
          return {{ ok: true, method: `window.${{key}}.${{method}}` }};
        }}
      }}
    }}
    return {{ ok: false }};
  }}
  function injectBody() {{
    for (const setter of [setByCkeditor, setByCkeIframe, setByCodeMirror, setByMonaco, trySetKnownWindowEditor, setByTextarea, setByContentEditable]) {{
      const result = setter();
      if (result && result.ok) return result;
    }}
    return {{ ok: false }};
  }}
  function readyHint() {{
    return {{
      href: location.href,
      title: document.title,
      codeMirror: document.querySelectorAll(".CodeMirror").length,
      textarea: document.querySelectorAll("textarea").length,
      contenteditable: document.querySelectorAll("[contenteditable='true'], [contenteditable='plaintext-only']").length,
      bodyText: cleanText(document.body ? document.body.innerText : "").slice(0, 160)
    }};
  }}
  if (!/csdn\\.net/i.test(location.hostname)) {{
    location.href = input.url;
    await sleep(4000);
  }}
  const deadline = Date.now() + 90000;
  let titleResult = {{ ok: false }};
  let bodyResult = {{ ok: false }};
  let lastHint = readyHint();
  while (Date.now() < deadline) {{
    if (!/login|passport/i.test(location.href)) {{
      titleResult = setTitle();
      bodyResult = injectBody();
      if (bodyResult && bodyResult.ok) {{
        return {{
          ok: true,
          pageUrl: location.href,
          titleSet: Boolean(titleResult && (titleResult.ok || titleResult.skipped)),
          bodyLength: input.markdown.length,
          method: bodyResult.method,
          hint: readyHint()
        }};
      }}
    }}
    lastHint = readyHint();
    await sleep(800);
  }}
  throw new Error("没有找到可写入的 CSDN 正文编辑区。请确认自动浏览器已经登录并停留在创作编辑页。" + JSON.stringify(lastHint));
}})()
"""


def navigate_csdn_editor(port: int) -> None:
    expression = f"""
(() => {{
  if (!/csdn\\.net/i.test(location.hostname) || !/mp_blog\\/creation\\/editor|editor\\.csdn\\.net\\/md/i.test(location.href)) {{
    location.href = {json.dumps(CSDN_CREATION_URL)};
    return {{ navigated: true, href: location.href }};
  }}
  return {{ navigated: false, href: location.href }};
}})()
"""
    try:
        cdp_runtime_evaluate(port, expression, timeout=20)
    except UserVisibleError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise UserVisibleError(f"打开 CSDN 创作页失败：{exc}") from exc


def write_csdn_editor_via_browser(port: int, title: str, body: str, html_body: str | None = None) -> dict[str, Any]:
    if not body.strip():
        raise UserVisibleError("没有可写入 CSDN 的正文内容。")
    port = resolve_csdn_cdp_port(port)
    navigate_csdn_editor(port)
    result = cdp_runtime_evaluate(port, build_csdn_editor_injection_script(title, body, html_body), timeout=120)
    if not isinstance(result, dict) or not result.get("ok"):
        raise UserVisibleError("CSDN 编辑器写入失败：没有收到成功确认。")
    return result


def browser_executable(browser: str) -> Path | None:
    browser = normalize_cookie_browser(browser)
    names = ["chrome.exe", "chrome"] if browser == "chrome" else ["msedge.exe", "msedge"]
    for name in names:
        found = shutil.which(name)
        if found:
            return Path(found)
    env = {name: os.environ.get(name) for name in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")}
    candidates = (
        [
            Path(env["PROGRAMFILES"] or "") / "Google" / "Chrome" / "Application" / "chrome.exe",
            Path(env["PROGRAMFILES(X86)"] or "") / "Google" / "Chrome" / "Application" / "chrome.exe",
            Path(env["LOCALAPPDATA"] or "") / "Google" / "Chrome" / "Application" / "chrome.exe",
        ]
        if browser == "chrome"
        else [
            Path(env["PROGRAMFILES"] or "") / "Microsoft" / "Edge" / "Application" / "msedge.exe",
            Path(env["PROGRAMFILES(X86)"] or "") / "Microsoft" / "Edge" / "Application" / "msedge.exe",
            Path(env["LOCALAPPDATA"] or "") / "Microsoft" / "Edge" / "Application" / "msedge.exe",
        ]
    )
    return next((candidate for candidate in candidates if candidate.exists()), None)


def csdn_debug_profile_dir(browser: str) -> Path:
    base = Path(os.environ.get("LOCALAPPDATA") or RUNS_DIR)
    return base / "CSDNImageRedactor" / "debug-browser-profiles" / normalize_cookie_browser(browser)


def wait_csdn_debug_browser(port: int, timeout: float = 20) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    encoded = quote(CSDN_CREATION_URL, safe="")
    tried_open = False
    last_valid: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        host_targets = iter_cdp_targets(port, timeout=1)
        for host, targets in host_targets:
            if targets:
                last_valid = {
                    "cdpHost": host,
                    "targetCount": len(targets),
                    "pageUrl": str((next((item for item in targets if target_is_csdn_page(item)), targets[0])).get("url") or ""),
                }
            for item in targets:
                if target_is_csdn_page(item):
                    return {
                        "cdpHost": host,
                        "targetCount": len(targets),
                        "pageUrl": str(item.get("url") or ""),
                    }
        if host_targets and not tried_open:
            for host, _ in host_targets:
                for method in ("PUT", "GET"):
                    try:
                        cdp_json_for_host(host, port, f"/json/new?{encoded}", method=method, timeout=2)
                        tried_open = True
                        break
                    except Exception:
                        continue
                if tried_open:
                    break
        time.sleep(0.5)
    if last_valid:
        return last_valid
    raise UserVisibleError(
        "CSDN 浏览器窗口已尝试打开，但后台调试端口还没有响应。请关闭刚打开的 CSDN 自动浏览器窗口后再点一次。"
    )


def open_csdn_debug_browser(browser: str, port: int) -> dict[str, Any]:
    browser = normalize_cookie_browser(browser)
    port = normalize_cdp_port(port)
    exe = browser_executable(browser)
    if not exe:
        raise UserVisibleError(f"没有找到 {cookie_browser_label(browser)}，请先安装 Chrome。")
    profile_dir = csdn_debug_profile_dir(browser)
    profile_dir.mkdir(parents=True, exist_ok=True)
    args = [
        str(exe),
        f"--remote-debugging-address=127.0.0.1",
        f"--remote-debugging-port={port}",
        "--remote-allow-origins=*",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--new-window",
        CSDN_CREATION_URL,
    ]
    subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    debug_state = wait_csdn_debug_browser(port, timeout=25)
    return {
        "browser": browser,
        "browserName": cookie_browser_label(browser),
        "port": port,
        **debug_state,
        "profileDir": str(profile_dir),
        "url": CSDN_CREATION_URL,
    }


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


def resolve_image_source(job_dir: Path, image_info: dict[str, Any]) -> Path:
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


def clean_previous_match_terms(matches: list[dict[str, Any]]) -> list[str]:
    terms: list[str] = []
    seen = set()
    for match in matches or []:
        text = str(match.get("text") or "").strip()
        if not text:
            continue
        key = re.sub(r"\s+", "", text).lower()
        if not key or key in seen:
            continue
        seen.add(key)
        terms.append(text)
    return terms


def build_rerun_instruction_options(image_info: dict[str, Any], options: Namespace) -> tuple[list[str], bool, str]:
    instruction = str(getattr(options, "image_instruction", "") or "").strip()
    previous_terms = clean_previous_match_terms(image_info.get("matches") or [])
    if instruction and is_no_mask_instruction(instruction):
        return [], True, instruction

    instruction_terms = extract_redaction_terms_from_instruction(instruction)
    strict_only = bool(instruction and is_strict_only_instruction(instruction))
    additive = bool(instruction and is_additive_instruction(instruction))
    if instruction_terms and not additive and not strict_only:
        strict_only = True

    if strict_only:
        ai_instruction = "\n".join(
            [
                "本次是单图二次识别。用户要求只按本次输入处理，忽略之前识别结果、默认敏感词和全局规则。",
                f"用户最新要求：{instruction}",
                f"从用户要求中提取的强制打码词：{json.dumps(instruction_terms, ensure_ascii=False)}",
            ]
        )
        return [], True, ai_instruction

    extra_terms = previous_terms if additive else []
    ai_instruction_parts = ["本次是单图二次识别，请以原图重新判断。"]
    if additive:
        ai_instruction_parts.append("用户希望保留原来已打码内容，并在此基础上补充新要求。")
        ai_instruction_parts.append(f"原来已命中的打码内容：{json.dumps(previous_terms, ensure_ascii=False)}")
    if instruction:
        ai_instruction_parts.append(f"用户最新要求：{instruction}")
        ai_instruction_parts.append(f"从用户要求中提取的强制打码词：{json.dumps(instruction_terms, ensure_ascii=False)}")
    return extra_terms, False, "\n".join(ai_instruction_parts)


def rerun_single_image(job_dir: Path, image_index: int, payload: dict[str, Any]) -> dict[str, Any]:
    report_data = load_report_data(job_dir)
    images = report_data.get("images") or []
    if image_index < 1 or image_index > len(images):
        raise UserVisibleError("图片序号无效")

    image_info = images[image_index - 1]
    source_path = resolve_image_source(job_dir, image_info)
    output_path = find_output_path(job_dir)
    output_dir = job_dir / "output"
    images_dir = output_dir / "images"
    rerun_payload = build_rerun_payload(job_dir, payload)
    options = build_options(job_dir, Path(read_json_file(job_dir / "options.json", {}).get("sourcePath") or output_path), rerun_payload)
    options.image_instruction = payload.get("imageInstruction") or rerun_payload.get("imageInstruction") or ""
    additive = bool(options.image_instruction and is_additive_instruction(options.image_instruction))
    extra_terms, strict_terms, ai_instruction = build_rerun_instruction_options(image_info, options)
    options.extra_terms = [f"exact:{term}" for term in extra_terms]
    options.strict_terms = strict_terms
    options.ai_image_instruction = ai_instruction
    options.preserve_local_mask_ids = additive and not strict_terms
    terms = load_terms(Path(options.terms))

    original_output_ref = image_info.get("outputRef")
    source_ref = image_info.get("sourceRef") or image_info.get("originalRef") or f"image-{image_index}"
    new_source_image_ref, new_output_ref, masked_count, matches = process_image(str(source_ref), source_path, images_dir, terms, options)

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
            "sourceImageRef": new_source_image_ref,
            "rerunInputRef": None,
            "lastRerunSource": "original",
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


CSDN_STRONG_AUTH_COOKIE_NAMES = {
    "usertoken",
    "username",
    "userinfo",
    "usernick",
    "au",
    "dc_session_id",
    "c_session_id",
    "session",
    "sessionid",
}


def cookie_pairs_from_header(cookie: str) -> dict[str, str]:
    pairs: dict[str, str] = {}
    for item in (cookie or "").split(";"):
        item = item.strip()
        if not item or "=" not in item:
            continue
        name, value = item.split("=", 1)
        name = name.strip()
        if name:
            pairs[name] = value.strip()
    return pairs


def analyze_csdn_cookie_input(value: str) -> dict[str, Any]:
    cookie = (value or "").strip()
    pairs = cookie_pairs_from_header(cookie)
    lower_names = {name.lower() for name in pairs}
    strong_names = sorted(name for name in pairs if name.lower() in CSDN_STRONG_AUTH_COOKIE_NAMES)
    has_user_token = "usertoken" in lower_names

    result: dict[str, Any] = {
        "ok": True,
        "inputKind": "cdp-cookie",
        "cookie": cookie,
        "cookieLength": len(cookie),
        "cookieCount": len(pairs),
        "strongAuthNames": strong_names,
        "hasUserToken": has_user_token,
        "looksIncomplete": False,
        "message": "",
    }
    if not cookie or "=" not in cookie or not pairs:
        result.update(
            {
                "ok": False,
                "looksIncomplete": True,
                "message": "CSDN 自动浏览器里没有读到有效登录态。请先在自动浏览器里登录 CSDN。",
            }
        )
        return result

    if not strong_names:
        result["ok"] = False
        result["looksIncomplete"] = True
        result["message"] = "CSDN 自动浏览器已连接，但没有读到完整登录凭证。请刷新创作页或重新登录 CSDN。"
        return result

    if not has_user_token:
        result["ok"] = False
        result["looksIncomplete"] = True
        result["message"] = f"已识别到 {len(pairs)} 个 Cookie，但没有看到 UserToken。请在 CSDN 自动浏览器里重新登录。"
        return result

    result["message"] = f"CSDN 自动登录态完整，已识别到 {len(pairs)} 个 Cookie。"
    return result


def strip_title_tag(content: str) -> tuple[str, str]:
    text = content or ""
    match = re.match(r"^\s*<title>(.*?)</title>\s*", text, flags=re.I | re.S)
    if not match:
        return "", text
    title = re.sub(r"\s+", " ", match.group(1)).strip()
    body = text[match.end() :].lstrip()
    return title, body


def infer_article_title(content: str) -> str:
    text = content or ""
    title, _ = strip_title_tag(text)
    if title:
        return title[:100]
    heading = re.search(r"^\s*#\s+(.+?)\s*$", text, flags=re.M)
    if heading:
        return re.sub(r"\s+", " ", heading.group(1)).strip()[:100]
    if "<" in text and ">" in text:
        soup = BeautifulSoup(text, "html.parser")
        for selector in ("title", "h1", "h2"):
            node = soup.find(selector)
            value = node.get_text(" ", strip=True) if node else ""
            if value:
                return re.sub(r"\s+", " ", value).strip()[:100]
    return ""


def html_body_fragment(content: str) -> str:
    text = content or ""
    soup = BeautifulSoup(text, "html.parser")
    body = soup.body
    if body:
        return "".join(str(item) for item in body.contents).strip()
    return text.strip()


def clean_clipboard_text(content: str) -> str:
    _, body = strip_title_tag(content)
    body = re.sub(
        r"data:image/[a-zA-Z0-9.+-]+;base64,[A-Za-z0-9+/=\r\n]+",
        "[图片已脱敏，复制到 CSDN 时会自动替换为 CSDN 图片链接]",
        body,
    )
    return body.strip() + ("\n" if body.strip() else "")


def write_system_clipboard(text: str) -> None:
    clipboard_dir = RUNS_DIR / ".clipboard"
    clipboard_dir.mkdir(parents=True, exist_ok=True)
    temp_path = clipboard_dir / f"{uuid.uuid4().hex}.txt"
    write_text(temp_path, text)
    command = f"Get-Content -LiteralPath {powershell_quote(temp_path)} -Raw -Encoding UTF8 | Set-Clipboard"
    try:
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
            cwd=str(APP_DIR),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        if completed.returncode != 0:
            raise UserVisibleError((completed.stderr or completed.stdout or "系统剪贴板写入失败。").strip())
    finally:
        try:
            temp_path.unlink()
        except OSError:
            pass


def result_has_local_images(content: str) -> bool:
    refs = collect_image_refs(content or "")
    return any(not ref.startswith("data:") and not re.match(r"^https?://", ref) for ref in refs)


def prepare_csdn_copy_payload(job_dir: Path, cdp_port: Any = None) -> dict[str, Any]:
    output_path = find_output_path(job_dir)
    content = read_text(output_path)
    if result_has_local_images(content):
        result = prepare_csdn_native(job_dir, "", cdp_port)
    else:
        meta = read_json_file(job_dir / "job.json", {})
        result = result_payload(job_dir, output_path, meta.get("source") or "文章", meta.get("notices") or [], persist=True)
    copy_text = clean_clipboard_text(str(result.get("content") or ""))
    if not copy_text:
        raise UserVisibleError("没有可复制的文章内容。")
    write_system_clipboard(copy_text)
    title = infer_article_title(str(result.get("content") or ""))
    result["copied"] = True
    result["copiedLength"] = len(copy_text)
    result["title"] = title
    return result


def prepare_csdn_editor_payload(job_dir: Path, cdp_port: Any = None) -> dict[str, Any]:
    port = normalize_cdp_port(cdp_port)
    result = prepare_csdn_copy_payload(job_dir, port)
    title = str(result.get("title") or "")
    body = clean_clipboard_text(str(result.get("content") or ""))
    html_body = html_body_fragment(str(result.get("clipboardHtml") or markdown_to_clipboard_html(body)))
    write_result = write_csdn_editor_via_browser(port, title, body, html_body)
    result["csdnEditorWrite"] = write_result
    result["editorUrl"] = write_result.get("pageUrl") or CSDN_CREATION_URL
    return result


def prepare_csdn_native(job_dir: Path, cookie: str = "", cdp_port: Any = None) -> dict[str, Any]:
    port = normalize_cdp_port(cdp_port) if cdp_port not in (None, "") else None
    if not port:
        raise UserVisibleError("请先打开 CSDN 自动浏览器并完成登录，才能自动上传图片。")
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
        url = csdn_upload_image_via_browser(port, candidate)
        replacements[ref] = url
        uploads.append({"ref": ref, "url": url})
    if not uploads:
        raise UserVisibleError("没有找到需要上传到 CSDN 的本地图片。")
    updated = replace_refs(content, replacements)
    ready_path = output_path.with_name(f"{output_path.stem}_csdn_ready{output_path.suffix}")
    write_text(ready_path, updated)
    meta = read_json_file(job_dir / "job.json", {})
    source_label = meta.get("source") or "文章"
    if "CSDN 原生图片" not in source_label:
        source_label = f"{source_label} · CSDN 原生图片"
    result = result_payload(
        job_dir,
        ready_path,
        source_label,
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


@app.post("/api/csdn/debug-browser/open")
def csdn_debug_browser_open():
    try:
        payload = request.get_json(force=True, silent=False)
        browser = normalize_cookie_browser(payload.get("browser"))
        port = normalize_cdp_port(payload.get("port"))
        data = open_csdn_debug_browser(browser, port)
        return jsonify(
            {
                "ok": True,
                **data,
                "message": (
                    f"已打开 {data['browserName']} 的 CSDN 登录浏览器。请在新窗口里登录 CSDN，"
                    "登录完成后回到本工具点击“检查自动登录”。"
                ),
            }
        )
    except UserVisibleError as exc:
        return json_error(str(exc), 400)
    except Exception as exc:  # noqa: BLE001
        return json_error(f"打开 CSDN 登录浏览器失败：{exc}", 500)


@app.post("/api/csdn/cookie/cdp")
def csdn_cookie_cdp():
    try:
        payload = request.get_json(force=True, silent=False)
        port = normalize_cdp_port(payload.get("port"))
        cookie = read_csdn_cookie_from_cdp(port)
        analysis = analyze_csdn_cookie_input(cookie)
        return jsonify(
            {
                "ok": True,
                "cookie": cookie,
                "analysis": analysis,
                "message": (
                    f"已从登录浏览器读取到 {analysis.get('cookieCount') or 0} 个 CSDN Cookie。"
                    "建议继续点击“检查上传组件”。"
                ),
            }
        )
    except UserVisibleError as exc:
        return json_error(str(exc), 400)
    except requests.RequestException as exc:
        return json_error(f"连接调试浏览器失败：{exc}", 400)
    except Exception as exc:  # noqa: BLE001
        return json_error(f"读取登录浏览器 Cookie 失败：{exc}", 500)


@app.post("/api/csdn/cookie/test")
def csdn_cookie_test():
    try:
        payload = request.get_json(force=True, silent=False)
        cdp_port = payload.get("cdpPort")
        if cdp_port in (None, ""):
            raise UserVisibleError("请先打开 CSDN 自动浏览器。")
        port = normalize_cdp_port(cdp_port)
        browser_cookie = read_csdn_cookie_from_cdp(port)
        analysis = analyze_csdn_cookie_input(browser_cookie)
        status = csdn_browser_upload_status(port)
        if not status.get("hasCsdnUpload"):
            raise UserVisibleError("CSDN 自动浏览器已连接，但创作页上传组件还没加载。请确认新窗口停留在 CSDN 创作页，刷新后再试。")
        if not analysis["ok"]:
            raise UserVisibleError(str(analysis["message"]))
        return jsonify(
            {
                "ok": True,
                "analysis": analysis,
                "message": "CSDN 自动浏览器可用：已读到完整登录态，且创作页上传组件已加载。",
                "pageUrl": status.get("href"),
            }
        )
    except UserVisibleError as exc:
        return json_error(str(exc), 400)
    except requests.RequestException as exc:
        return json_error(f"连接 CSDN 自动浏览器失败：{exc}", 500)
    except Exception as exc:  # noqa: BLE001
        return json_error(f"测试 CSDN 自动浏览器失败：{exc}", 500)


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


@app.get("/api/llm-guide")
def llm_guide():
    try:
        path = APP_DIR / "LLM.TXT"
        if not path.exists():
            raise UserVisibleError("没有找到 LLM.TXT 文件")
        return jsonify({"ok": True, "path": str(path), "content": read_text(path)})
    except UserVisibleError as exc:
        return json_error(str(exc), 404)
    except Exception as exc:  # noqa: BLE001
        return json_error(f"读取 LLM.TXT 失败：{exc}", 500)


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


def current_lark_status_message() -> str:
    try:
        data = run_lark_json(["auth", "status", "--verify"], timeout=8)
        status = safe_auth_status(data)
        user_name = status.get("userName") or "未登录"
        token_status = status.get("tokenStatus") or status.get("userStatus") or "-"
        return f"当前 CLI 可用账号：{user_name}，状态：{token_status}。"
    except Exception as exc:  # noqa: BLE001
        return f"同时检查当前 CLI 账号失败：{exc}"


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
        try:
            completed = run_lark_cli(["auth", "login", "--device-code", device_code], timeout=30)
        except subprocess.TimeoutExpired as exc:
            raise UserVisibleError(
                "确认飞书授权等待超过 30 秒。请确认授权网页已经选择企业账号并点击允许；"
                "如果设置页已经显示目标账号可用，就不需要重复点击确认。"
                + "\n"
                + current_lark_status_message()
            ) from exc
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
        doc_url = payload.get("docUrl") or ""
        if is_lark_file_url(doc_url):
            source, notices = download_lark_file(doc_url, job_dir)
            source_label = "飞书云空间文件"
        else:
            content, notices = fetch_lark_markdown(doc_url)
            source = job_dir / "input" / "lark_source.md"
            write_text(source, content)
            source_label = "飞书文档"
        return jsonify(process_source(job_dir, source, payload, source_label, notices))
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
        doc_url = payload.get("docUrl") or ""
        if is_lark_file_url(doc_url):
            source, notices = download_lark_file(doc_url, job_dir, emit)
            source_label = "飞书云空间文件"
        else:
            content, notices = fetch_lark_markdown(doc_url, emit)
            source = job_dir / "input" / "lark_source.md"
            write_text(source, content)
            emit("飞书正文已保存到本地临时草稿。")
            source_label = "飞书文档"
        emit("开始识别图片并执行脱敏。")
        return process_source(job_dir, source, payload, source_label, notices, progress=emit)

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
        return jsonify(prepare_csdn_native(job_dir, payload.get("cookie") or "", payload.get("cdpPort")))
    except UserVisibleError as exc:
        return json_error(str(exc), 400)
    except Exception as exc:  # noqa: BLE001
        return json_error(f"CSDN 原生图片准备失败：{exc}", 500)


@app.post("/api/jobs/<job_id>/copy-safe")
def job_copy_safe(job_id: str):
    try:
        job_dir = get_job_dir(job_id)
        payload = request.get_json(force=True, silent=True) or {}
        return jsonify(prepare_csdn_copy_payload(job_dir, payload.get("cdpPort")))
    except UserVisibleError as exc:
        return json_error(str(exc), 400)
    except Exception as exc:  # noqa: BLE001
        return json_error(f"复制安全草稿失败：{exc}", 500)


@app.post("/api/jobs/<job_id>/csdn/copy")
def job_csdn_copy(job_id: str):
    try:
        job_dir = get_job_dir(job_id)
        payload = request.get_json(force=True, silent=True) or {}
        return jsonify(prepare_csdn_editor_payload(job_dir, payload.get("cdpPort")))
    except UserVisibleError as exc:
        return json_error(str(exc), 400)
    except Exception as exc:  # noqa: BLE001
        return json_error(f"复制文章到 CSDN 失败：{exc}", 500)


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
