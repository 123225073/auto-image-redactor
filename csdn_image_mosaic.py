from __future__ import annotations

import argparse
import base64
import hashlib
from difflib import SequenceMatcher
import json
import mimetypes
import os
import re
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import unquote, urlparse

import requests
from PIL import Image, ImageOps


DEFAULT_BASE_URL = "https://cpa.fengsha.online/v1"
DEFAULT_MODEL = "gpt-5.5"

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff"}

DEFAULT_TERMS = [
    "公司名称",
    "公司代码",
    "公司",
    "供应商名称",
    "供应商",
    "客户名称",
    "客户",
    "联系人",
    "姓名",
    "电话",
    "手机",
    "邮箱",
    "地址",
    "税号",
    "统一社会信用代码",
    "开户行",
    "银行账号",
    "账号",
    "账户",
    "工号",
    "身份证",
    "SAP账号",
    "SAP账户",
]

STANDARD_LABEL_TERMS = {
    "公司名称",
    "公司代码",
    "公司",
    "集团公司",
    "供应商名称",
    "供应商",
    "客户名称",
    "客户",
    "联系人",
    "姓名",
    "电话",
    "手机",
    "邮箱",
    "地址",
    "税号",
    "统一社会信用代码",
    "开户行",
    "银行账号",
    "账号",
    "账户",
    "工号",
    "身份证",
    "SAP账号",
    "SAP账户",
    "资产",
    "子编号",
    "事务类型",
    "物料",
    "凭证号",
    "采购组织",
    "销售组织",
    "工厂",
    "利润中心",
    "成本中心",
    "会计年度",
    "期间",
    "货币",
    "折旧",
    "帐面折旧",
    "账面折旧",
}

SAP_CHROME_KEYWORDS = {
    "交易",
    "编辑",
    "转到",
    "附加",
    "环境",
    "系统",
    "帮助",
    "行项目",
    "抬头数据更改",
    "附加资产科目分配",
}

DEFAULT_PATTERNS = [
    re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
    re.compile(r"(?<!\d)\d{6}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx](?!\d)"),
    re.compile(r"(?<![0-9A-Z])[159Y][1239]\d{6}[0-9A-Z]{9}[0-9X](?![0-9A-Z])", re.IGNORECASE),
]

BUSINESS_IDENTIFIER_KEYWORDS = {
    "物料",
    "物料号",
    "物料编码",
    "对象标识",
    "对象",
    "BOM",
    "组件",
    "单据",
    "单据号",
    "凭证",
    "凭证号",
    "订单",
    "订单号",
    "采购订单",
    "销售订单",
    "项目",
    "行项目",
    "批次",
    "批号",
    "层级",
    "层",
    "工厂",
    "数量",
    "金额",
    "库存",
    "库位",
    "CS03",
    "CS15",
    "MM03",
    "ME23N",
}

NUMERIC_SENSITIVE_LABEL_KEYWORDS = {
    "账号",
    "账户",
    "银行账号",
    "银行账户",
    "银行卡",
    "卡号",
    "密码",
    "口令",
    "登录名",
    "用户名",
    "用户ID",
    "手机号",
    "手机",
    "电话",
    "身份证",
    "证件号",
    "税号",
    "统一社会信用代码",
}

MARKDOWN_IMAGE_RE = re.compile(r"!\[(?P<alt>[^\]]*)\]\((?P<target>[^)\n]+)\)")
HTML_IMG_QUOTED_RE = re.compile(
    r"(?P<prefix><img\b[^>]*?\bsrc\s*=\s*)(?P<quote>[\"'])(?P<src>.*?)(?P=quote)(?P<suffix>[^>]*>)",
    re.IGNORECASE | re.DOTALL,
)
HTML_IMG_UNQUOTED_RE = re.compile(
    r"(?P<prefix><img\b[^>]*?\bsrc\s*=\s*)(?P<src>[^\s>\"']+)(?P<suffix>[^>]*>)",
    re.IGNORECASE | re.DOTALL,
)


@dataclass
class OcrBox:
    id: int
    text: str
    confidence: float
    bbox: tuple[int, int, int, int]


@dataclass
class ProcessResult:
    original_ref: str
    status: str
    output_ref: str | None = None
    source_path: str | None = None
    source_image_ref: str | None = None
    rerun_input_ref: str | None = None
    last_rerun_source: str | None = None
    masked_regions: int = 0
    error: str | None = None
    matches: list[dict[str, Any]] | None = None
    note: str | None = None
    updated_at: int | None = None


def read_text(path: Path) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return path.read_text(encoding=encoding)
        except UnicodeDecodeError:
            continue
    return path.read_text(errors="replace")


def write_text(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


def is_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"}


def is_data_image(value: str) -> bool:
    return value.startswith("data:image/")


def parse_markdown_target(target: str) -> tuple[str, str, str]:
    raw = target.strip()
    if raw.startswith("<"):
        end = raw.find(">")
        if end != -1:
            return raw[1:end], "<", ">" + raw[end + 1 :]

    match = re.match(r"^(?P<url>\S+)(?P<suffix>\s+[\"'].*[\"'])$", raw)
    if match:
        return match.group("url"), "", match.group("suffix")

    return raw, "", ""


def rebuild_markdown_target(new_ref: str, prefix: str, suffix: str) -> str:
    if prefix == "<":
        return f"<{new_ref}{suffix}"
    return f"{new_ref}{suffix}"


def collect_image_refs(content: str) -> list[str]:
    refs: list[str] = []

    for match in MARKDOWN_IMAGE_RE.finditer(content):
        url, _, _ = parse_markdown_target(match.group("target"))
        refs.append(url)

    for match in HTML_IMG_QUOTED_RE.finditer(content):
        refs.append(match.group("src"))

    for match in HTML_IMG_UNQUOTED_RE.finditer(content):
        refs.append(match.group("src"))

    unique: list[str] = []
    seen = set()
    for ref in refs:
        clean = ref.strip()
        if clean and clean not in seen:
            seen.add(clean)
            unique.append(clean)
    return unique


def looks_like_image(ref: str) -> bool:
    if is_url(ref) or is_data_image(ref):
        return True
    suffix = Path(unquote(ref).split("?", 1)[0]).suffix.lower()
    return suffix in IMAGE_SUFFIXES


def safe_stem(value: str) -> str:
    digest = hashlib.sha1(value.encode("utf-8", errors="ignore")).hexdigest()[:10]
    if value.startswith("data:image/"):
        return f"embedded_{digest}"
    parsed = urlparse(value)
    name = Path(unquote(parsed.path or value)).stem
    name = re.sub(r"[^A-Za-z0-9_.-]+", "_", name).strip("._")
    return f"{name or 'image'}_{digest}"


def display_ref(value: str) -> str:
    if value.startswith("data:image/"):
        digest = hashlib.sha1(value.encode("utf-8", errors="ignore")).hexdigest()[:10]
        return f"embedded image {digest}"
    if len(value) > 160:
        return value[:120] + "..." + value[-24:]
    return value


def guess_suffix(ref: str, content_type: str | None = None) -> str:
    parsed_suffix = Path(unquote(urlparse(ref).path or ref).split("?", 1)[0]).suffix.lower()
    if parsed_suffix in IMAGE_SUFFIXES:
        return parsed_suffix
    if content_type:
        guessed = mimetypes.guess_extension(content_type.split(";", 1)[0].strip())
        if guessed in IMAGE_SUFFIXES:
            return guessed
    return ".png"


def resolve_local_image(ref: str, doc_dir: Path) -> Path | None:
    cleaned = unquote(ref.split("#", 1)[0].split("?", 1)[0]).strip()
    if cleaned.startswith("file:///"):
        cleaned = cleaned[8:]
    candidate = Path(cleaned)
    if candidate.is_absolute() and candidate.exists():
        return candidate
    candidate = (doc_dir / cleaned).resolve()
    if candidate.exists():
        return candidate
    return None


def materialize_image(ref: str, doc_dir: Path, cache_dir: Path) -> tuple[Path | None, str | None]:
    cache_dir.mkdir(parents=True, exist_ok=True)

    if is_data_image(ref):
        try:
            header, encoded = ref.split(",", 1)
            suffix = ".png"
            mime_match = re.match(r"data:(image/[^;]+)", header)
            if mime_match:
                suffix = guess_suffix(ref, mime_match.group(1))
            path = cache_dir / f"{safe_stem(ref)}{suffix}"
            path.write_bytes(base64.b64decode(encoded))
            return path, None
        except Exception as exc:  # noqa: BLE001
            return None, f"data image decode failed: {exc}"

    if is_url(ref):
        try:
            response = requests.get(ref, timeout=30)
            response.raise_for_status()
            suffix = guess_suffix(ref, response.headers.get("content-type"))
            path = cache_dir / f"{safe_stem(ref)}{suffix}"
            path.write_bytes(response.content)
            return path, None
        except Exception as exc:  # noqa: BLE001
            return None, f"download failed: {exc}"

    local = resolve_local_image(ref, doc_dir)
    if local:
        return local, None

    return None, "local image not found"


def load_terms(path: Path | None) -> list[str]:
    terms = list(DEFAULT_TERMS)
    if path and path.exists():
        for line in read_text(path).splitlines():
            value = line.strip()
            if not value or value.startswith("#"):
                continue
            for part in re.split(r"[\n,，;；、]+", value):
                part = part.strip()
                if part:
                    terms.append(part)
    seen = set()
    unique: list[str] = []
    for term in terms:
        key = term.lower()
        if key not in seen:
            seen.add(key)
            unique.append(term)
    return unique


def load_ocr_boxes(image_path: Path, min_confidence: float) -> list[OcrBox]:
    try:
        from rapidocr_onnxruntime import RapidOCR
    except ImportError as exc:
        raise RuntimeError("缺少本地 OCR 组件，请先运行：python -m pip install -r requirements.txt") from exc

    ocr = RapidOCR()
    result, _ = ocr(str(image_path))
    boxes: list[OcrBox] = []
    for index, item in enumerate(result or [], start=1):
        points, text, confidence = item
        if float(confidence) < min_confidence:
            continue
        xs = [int(round(point[0])) for point in points]
        ys = [int(round(point[1])) for point in points]
        boxes.append(
            OcrBox(
                id=index,
                text=str(text or "").strip(),
                confidence=float(confidence),
                bbox=(min(xs), min(ys), max(xs), max(ys)),
            )
        )
    return boxes


def term_matches(text: str, terms: Iterable[str]) -> bool:
    lowered = text.lower()
    compact = re.sub(r"\s+", "", lowered)
    for term in terms:
        if term.startswith("re:"):
            try:
                if re.search(term[3:], text, re.IGNORECASE):
                    return True
            except re.error:
                continue
        elif term.startswith("exact:"):
            needle = re.sub(r"\s+", "", term[6:].lower())
            if needle and needle in compact:
                return True
        elif term.startswith("fuzzy:"):
            needle = re.sub(r"\s+", "", term[6:].lower())
            if needle and fuzzy_contains(compact, needle):
                return True
        else:
            needle = re.sub(r"\s+", "", term.lower())
            if needle and needle in compact:
                return True
    return False


def compact_text(text: str) -> str:
    return re.sub(r"[\s:：,，;；、|｜\-\[\]【】（）()\u3000]+", "", text or "").strip()


def compact_alnum(text: str) -> str:
    return re.sub(r"[^0-9A-Za-z]", "", text or "").strip()


def matched_terms(text: str, terms: Iterable[str]) -> list[str]:
    matches: list[str] = []
    lowered = text.lower()
    compact = re.sub(r"\s+", "", lowered)
    for term in terms:
        if not term or term.startswith("#"):
            continue
        if term.startswith("re:"):
            try:
                if re.search(term[3:], text):
                    matches.append(term)
            except re.error:
                continue
        elif term.startswith("exact:"):
            needle = re.sub(r"\s+", "", term[6:].lower())
            if needle and needle in compact:
                matches.append(term)
        elif term.startswith("fuzzy:"):
            needle = re.sub(r"\s+", "", term[6:].lower())
            if needle and fuzzy_contains(compact, needle):
                matches.append(term)
        else:
            needle = re.sub(r"\s+", "", term.lower())
            if needle and needle in compact:
                matches.append(term)
    return matches


def clean_term(term: str) -> str:
    return compact_text(re.sub(r"^(?:exact|fuzzy):", "", term, flags=re.IGNORECASE))


def user_terms_only(terms: Iterable[str]) -> list[str]:
    default_keys = {compact_text(item).lower() for item in DEFAULT_TERMS}
    custom: list[str] = []
    for term in terms:
        if not term or term.startswith("#") or term.startswith("re:"):
            continue
        cleaned = clean_term(term).lower()
        if cleaned and cleaned not in default_keys:
            custom.append(term)
    return custom


def explicit_user_term_matches(text: str, terms: Iterable[str]) -> bool:
    return term_matches(text, user_terms_only(terms))


def is_standard_label_text(text: str) -> bool:
    compact = compact_text(text)
    if not compact:
        return True
    if compact in {compact_text(item) for item in STANDARD_LABEL_TERMS | SAP_CHROME_KEYWORDS}:
        return True
    if len(compact) <= 8 and any(compact == compact_text(item) for item in STANDARD_LABEL_TERMS):
        return True
    return False


def is_sap_chrome_text(text: str) -> bool:
    compact = compact_text(text)
    if not compact:
        return True
    if any(keyword in text for keyword in SAP_CHROME_KEYWORDS):
        return True
    if re.search(r"\bS4Q\b|\bsaps4q\d+\b|\bSAP\b", text, re.IGNORECASE) and any(mark in text for mark in ("|", ">", "OVR", "III")):
        return True
    if re.search(r"^\s*[>»|｜\s]*(S4Q|SAP|OVR|III|\d{3,4})", text, re.IGNORECASE) and "|" in text:
        return True
    return False


def matched_only_standard_labels(text: str, terms: Iterable[str]) -> bool:
    terms_found = matched_terms(text, terms)
    if not terms_found:
        return False
    cleaned = [clean_term(term) for term in terms_found if clean_term(term)]
    if not cleaned:
        return False
    return all(item in {compact_text(term) for term in STANDARD_LABEL_TERMS} for item in cleaned)


def is_sensitive_field_label(text: str) -> bool:
    compact = compact_text(text)
    if any(compact_text(keyword) in compact for keyword in NUMERIC_SENSITIVE_LABEL_KEYWORDS):
        return True
    return compact in {
        compact_text(item)
        for item in (
            "公司名称",
            "公司代码",
            "供应商名称",
            "供应商",
            "客户名称",
            "客户",
            "联系人",
            "姓名",
            "电话",
            "手机",
            "邮箱",
            "地址",
            "税号",
            "统一社会信用代码",
            "开户行",
            "银行账号",
            "账号",
            "账户",
            "SAP账号",
            "SAP账户",
            "密码",
            "登录密码",
            "用户名",
            "用户ID",
            "银行卡",
            "卡号",
        )
    }


def is_standalone_sensitive_pattern(text: str) -> bool:
    return any(pattern.search(text) for pattern in DEFAULT_PATTERNS)


def is_non_sensitive_business_identifier(text: str, terms: Iterable[str]) -> bool:
    if explicit_user_term_matches(text, terms):
        return False

    compact = compact_text(text)
    alnum = compact_alnum(compact)
    if not alnum:
        return False

    has_long_number = bool(re.search(r"\d{5,}", compact))
    has_business_keyword = any(compact_text(keyword).lower() in compact.lower() for keyword in BUSINESS_IDENTIFIER_KEYWORDS)
    if has_business_keyword and has_long_number:
        return True

    # SAP material numbers, document numbers, BOM component IDs, vouchers and
    # order numbers are often long pure digits. Do not redact them just because
    # they happen to have the same length as a tax ID or identity number.
    if re.fullmatch(r"\d{12,20}", alnum):
        return True

    if is_standalone_sensitive_pattern(text):
        return False

    if re.fullmatch(r"\d{6,20}", alnum):
        return True

    return False


def is_contextual_sensitive_value(text: str, label_text: str) -> bool:
    compact = compact_text(text)
    alnum = compact_alnum(compact)
    if not compact or is_standard_label_text(text) or is_sap_chrome_text(text):
        return False

    label = compact_text(label_text)
    if any(compact_text(keyword) in label for keyword in BUSINESS_IDENTIFIER_KEYWORDS):
        return False

    if any(keyword in label for keyword in ("手机", "电话")):
        return bool(re.search(r"(?<!\d)1[3-9]\d{9}(?!\d)", text) or re.search(r"(?<!\d)(?:0\d{2,3}[-\s]?)?\d{7,8}(?!\d)", text))

    if any(keyword in label for keyword in ("身份证", "证件号")):
        return bool(re.search(r"(?<!\d)\d{6}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx](?!\d)", text))

    if any(keyword in label for keyword in ("税号", "统一社会信用代码")):
        return bool(re.fullmatch(r"[0-9A-Za-z]{15,20}", alnum))

    if any(keyword in label for keyword in ("银行账号", "银行账户", "银行卡", "卡号")):
        return bool(re.fullmatch(r"\d{8,24}", alnum))

    if any(keyword in label for keyword in ("密码", "口令")):
        return len(compact) >= 3

    if any(keyword in label for keyword in ("账号", "账户", "登录名", "用户名", "用户ID", "SAP账号", "SAP账户")):
        return bool(re.search(r"[A-Za-z]", alnum) or re.fullmatch(r"\d{4,24}", alnum))

    if any(keyword in label for keyword in ("公司", "供应商", "客户", "联系人", "姓名", "地址", "开户行")):
        if re.fullmatch(r"\d+", alnum):
            return False
        return bool(re.search(r"[\u4e00-\u9fff]{2,}", compact) or re.search(r"[A-Za-z]{2,}", compact))

    return False


def has_sensitive_neighbor_context(box: OcrBox, boxes: list[OcrBox]) -> bool:
    x1, y1, _, y2 = box.bbox
    center_y = (y1 + y2) / 2
    height = max(1, y2 - y1)
    for label in boxes:
        if label.id == box.id or not is_sensitive_field_label(label.text):
            continue
        lx1, ly1, lx2, ly2 = label.bbox
        label_center_y = (ly1 + ly2) / 2
        same_row = abs(center_y - label_center_y) <= max(18, height * 0.9, (ly2 - ly1) * 0.9)
        nearby_right = x1 >= lx2 - 8 and x1 <= lx2 + 420
        label_inside_same_box = label.id == box.id or compact_text(label.text) in compact_text(box.text)
        if (same_row and nearby_right or label_inside_same_box) and is_contextual_sensitive_value(box.text, label.text):
            return True
    return False


def looks_like_field_value(text: str) -> bool:
    compact = compact_text(text)
    if not compact or is_standard_label_text(text) or is_sap_chrome_text(text):
        return False
    if len(compact) <= 1:
        return False
    if re.search(r"[\u4e00-\u9fff]{2,}", compact):
        return True
    if re.search(r"[A-Za-z]\w{1,}|\d{2,}", compact):
        return True
    return False


def filter_standard_mask_ids(boxes: list[OcrBox], mask_ids: set[int], terms: Iterable[str]) -> set[int]:
    filtered: set[int] = set()
    for box in boxes:
        if box.id not in mask_ids:
            continue
        if is_sap_chrome_text(box.text):
            continue
        if is_standard_label_text(box.text):
            continue
        if matched_only_standard_labels(box.text, terms) and not pattern_matches(box.text):
            continue
        if is_non_sensitive_business_identifier(box.text, terms) and not has_sensitive_neighbor_context(box, boxes):
            continue
        filtered.add(box.id)
    return filtered


def fuzzy_contains(text: str, needle: str) -> bool:
    if not needle:
        return False
    if needle in text:
        return True
    if len(needle) <= 4 or len(text) < max(3, len(needle) - 2):
        return False

    # Keep fuzzy matching local. A loose subsequence match can turn short
    # business words into false positives inside SAP field labels.
    min_ratio = 0.86 if len(needle) <= 8 else 0.8
    min_len = max(3, len(needle) - 2)
    max_len = min(len(text), len(needle) + 2)
    for window_len in range(min_len, max_len + 1):
        for start in range(0, len(text) - window_len + 1):
            window = text[start : start + window_len]
            if SequenceMatcher(None, window, needle).ratio() >= min_ratio:
                return True
    return False


def pattern_matches(text: str) -> bool:
    return is_standalone_sensitive_pattern(text)


def find_local_mask_ids(boxes: list[OcrBox], terms: list[str], mask_all_text: bool) -> set[int]:
    if mask_all_text:
        return {box.id for box in boxes if box.text}

    mask_ids: set[int] = set()
    label_boxes: list[OcrBox] = []

    for box in boxes:
        if not box.text:
            continue
        if is_sensitive_field_label(box.text):
            label_boxes.append(box)
            if is_contextual_sensitive_value(box.text, box.text):
                mask_ids.add(box.id)
                continue
        if is_sap_chrome_text(box.text) or is_standard_label_text(box.text):
            continue
        term_hit = term_matches(box.text, terms)
        pattern_hit = pattern_matches(box.text)
        if term_hit and matched_only_standard_labels(box.text, terms) and not pattern_hit:
            continue
        if not term_hit and is_non_sensitive_business_identifier(box.text, terms):
            continue
        if term_hit or pattern_hit:
            mask_ids.add(box.id)
            if is_sensitive_field_label(box.text):
                label_boxes.append(box)

    # OCR sometimes splits a sensitive field label and its value into adjacent boxes.
    # We keep the SAP field label visible and only mask a value-like box beside it.
    for label in label_boxes:
        lx1, ly1, lx2, ly2 = label.bbox
        label_center_y = (ly1 + ly2) / 2
        label_height = max(1, ly2 - ly1)
        for box in boxes:
            if box.id in mask_ids:
                continue
            if not is_contextual_sensitive_value(box.text, label.text):
                continue
            x1, y1, x2, y2 = box.bbox
            center_y = (y1 + y2) / 2
            same_row = abs(center_y - label_center_y) <= max(18, label_height * 0.9)
            nearby_right = x1 >= lx2 - 8 and x1 <= lx2 + 380
            if same_row and nearby_right:
                mask_ids.add(box.id)

    return filter_standard_mask_ids(boxes, mask_ids, terms)


def extract_json_object(text: str) -> dict:
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL | re.IGNORECASE)
    if fenced:
        return json.loads(fenced.group(1))
    raw = re.search(r"\{.*\}", text, re.DOTALL)
    if raw:
        return json.loads(raw.group(0))
    return json.loads(text)


def cpa_classify_mask_ids(
    boxes: list[OcrBox],
    api_key: str,
    base_url: str,
    model: str,
    terms: list[str],
    match_mode: str,
    industry_prompt: str = "",
    image_instruction: str = "",
    candidate_ids: set[int] | None = None,
    refine_only: bool = False,
) -> set[int]:
    if not boxes:
        return set()

    endpoint = f"{base_url.rstrip('/')}/chat/completions"
    candidate_ids = candidate_ids or set()
    lines = [
        {
            "id": box.id,
            "text": box.text,
            "candidate": box.id in candidate_ids,
        }
        for box in boxes
        if box.text
    ]
    user_terms = [
        term
        for term in terms
        if not term.startswith("re:")
        and term not in DEFAULT_TERMS
        and not term.startswith("#")
    ]
    prompt_parts = [
        "请判断下面这些 OCR 文本行中，哪些应该在公开发布到 CSDN 前打码。",
        "敏感信息包括：公司名称、供应商名称、客户名称、人员姓名、手机号、邮箱、地址、税号、银行账号、SAP 登录账号、内部组织/公司代码，以及能定位具体公司或个人的信息。",
        "普通菜单名、按钮名、字段标签、系统标准术语如果没有暴露具体实体，不要打码；如果标签和具体值在同一行，或者旁边就是具体值，要选择真正暴露实体的行。",
        "SAP GUI 顶部菜单、事务栏、状态栏、系统环境名、窗口标题、按钮文本、页签名、字段标签本身通常不是敏感信息，不要因为出现“公司”“供应商”“客户”“账号”等字段名就打码。",
        "物料编码、物料号、BOM 组件号、单据号、凭证号、订单号、项目号、行项目号、批次、数量、层级、工厂代码、CS03/CS15 查询结果等业务编号通常不要打码，除非用户额外敏感词明确指定它们。",
        "纯数字或普通业务编码不要仅凭长度打码；只有手机号、身份证、税号、银行卡/银行账号、登录账号、密码等安全或个人信息才应选择。",
        "例如“公司代码”“供应商”“客户”“资产”“子编号”“事务类型”“帐面折旧”“行项目”“抬头数据更改”等只是 SAP 标准字段/菜单时，不要选择；只有右侧/同一行的真实公司、供应商、客户、人员、账号、密码、税号、银行卡等敏感值才选择。",
        f"用户额外指定的脱敏词为：{json.dumps(user_terms, ensure_ascii=False)}。",
        f"匹配模式为：{match_mode}。fuzzy 表示包含、近似或 OCR 有轻微错字也应命中；exact 表示必须出现指定短语。",
    ]
    if refine_only:
        prompt_parts.append(
            "当前是行业提示词增强模式：candidate=true 表示本地敏感词已初步命中。请以这些候选为主，结合行业提示词去掉误伤；只有非常明显的漏网敏感实体才可以补充 candidate=false 的行。"
        )
    if industry_prompt.strip():
        prompt_parts.append("行业/业务场景提示词如下，请用它判断哪些词是业务主数据，哪些只是系统标准标签：\n" + industry_prompt.strip())
    if image_instruction.strip():
        prompt_parts.append("用户对这一张图片的单独要求如下，优先级最高：\n" + image_instruction.strip())
    prompt_parts.extend(
        [
            "如果用户明确要求这张图不需要打码，返回空数组。",
            "只返回 JSON，格式为：{\"mask_ids\":[1,2,3]}。",
            f"OCR 文本行：\n{json.dumps(lines, ensure_ascii=False)}",
        ]
    )
    prompt = "\n\n".join(prompt_parts)
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "你是严谨的隐私脱敏助手，只返回可解析 JSON。"},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0,
    }
    response = requests.post(
        endpoint,
        headers={"authorization": f"Bearer {api_key}", "content-type": "application/json"},
        json=payload,
        timeout=60,
    )
    response.raise_for_status()
    data = response.json()
    content = data["choices"][0]["message"]["content"]
    parsed = extract_json_object(content)
    return {int(item) for item in parsed.get("mask_ids", [])}


def expand_bbox(bbox: tuple[int, int, int, int], width: int, height: int, padding: int) -> tuple[int, int, int, int]:
    x1, y1, x2, y2 = bbox
    return (
        max(0, x1 - padding),
        max(0, y1 - padding),
        min(width, x2 + padding),
        min(height, y2 + padding),
    )


def apply_mosaic(image: Image.Image, regions: list[tuple[int, int, int, int]], block_size: int) -> Image.Image:
    output = image.copy()
    for region in regions:
        x1, y1, x2, y2 = region
        if x2 <= x1 or y2 <= y1:
            continue
        crop = output.crop(region)
        small_size = (max(1, (x2 - x1) // block_size), max(1, (y2 - y1) // block_size))
        mosaic = crop.resize(small_size, Image.Resampling.BILINEAR).resize(crop.size, Image.Resampling.NEAREST)
        output.paste(mosaic, region)
    return output


def split_instruction_clauses(text: str) -> list[str]:
    return [
        item.strip()
        for item in re.split(r"[\n,，;；。.!！?？]+", text or "")
        if item.strip()
    ]


def clean_instruction_term(text: str) -> str:
    cleaned = re.sub(r"[\"'“”‘’`《》<>【】\[\]()（）]", "", text or "").strip()
    cleaned = re.sub(r"\s+", "", cleaned)
    cleaned = re.sub(r"^(?:请|麻烦|帮我|帮忙|把|将|对|给|再|另外|还有|以及|和|只|仅|全部|所有|补充|新增|追加|继续|同时)+", "", cleaned)
    cleaned = re.sub(r"(?:也|都|全部|一起|统一|需要|要|进行|相关|内容|文字|词|字段|区域|部分|这些|这个|等|也)$", "", cleaned)
    return cleaned.strip()


def extract_redaction_terms_from_instruction(text: str) -> list[str]:
    terms: list[str] = []
    for clause in split_instruction_clauses(text):
        compact = compact_text(clause)
        if re.search(r"(不需要|不用|不要|无需|别).{0,12}(打码|脱敏|马赛克|遮盖|隐藏|屏蔽)", compact):
            continue
        if re.search(r"(保留|保持|沿用|原来|原有|之前|上次|已).{0,8}(打码|脱敏|马赛克).{0,8}(部分|内容|区域)?$", compact):
            continue

        candidates: list[str] = []
        for match in re.finditer(
            r"(?:请|麻烦|帮我|帮忙)?(?:把|将|对|给)?\s*"
            r"[\"'“”‘’`《》<>【】\[\]()（）]?"
            r"(?P<term>[\u4e00-\u9fffA-Za-z0-9_.\-（）()·\s]{2,50}?)"
            r"[\"'“”‘’`《》<>【】\[\]()（）]?\s*"
            r"(?:也|都|全部|一起|统一|需要|要|进行)?\s*"
            r"(?:打码|脱敏|马赛克|遮盖|隐藏|屏蔽)",
            clause,
        ):
            candidates.append(match.group("term"))

        for match in re.finditer(
            r"(?:只|仅|全部|统一|补充|新增|追加|继续|再)?\s*(?:打码|脱敏|马赛克|遮盖|隐藏|屏蔽)"
            r"(?:内容|文字|词|字段|区域|部分)?\s*[:：]?\s*[\"'“”‘’`《》<>【】\[\]()（）]?"
            r"(?P<term>[\u4e00-\u9fffA-Za-z0-9_.\-（）()·\s、/，,;；和以及]{2,80})",
            clause,
        ):
            candidates.append(match.group("term"))

        for candidate in candidates:
            for part in re.split(r"[、/，,;；]+|(?:和|以及)", candidate):
                term = clean_instruction_term(part)
                if 2 <= len(term) <= 40 and not re.search(r"(打码|脱敏|马赛克|遮盖|隐藏|屏蔽|不要|不用|无需|不需要)", term):
                    terms.append(term)

    unique: list[str] = []
    seen = set()
    for term in terms:
        key = term.lower()
        if key not in seen:
            seen.add(key)
            unique.append(term)
    return unique


def is_strict_only_instruction(text: str) -> bool:
    for clause in split_instruction_clauses(text):
        compact = compact_text(clause)
        if not compact:
            continue
        if re.search(r"(其他|其它|其余|剩下|别的).{0,10}(不需要|不用|不要|无需|别).{0,10}(打码|脱敏|马赛克|遮盖|隐藏|屏蔽)", compact):
            return True
        if re.search(r"(只|仅|只需要|仅需要|只用|仅用).{0,10}(打码|脱敏|马赛克|遮盖|隐藏|屏蔽)", compact):
            return True
        if re.search(r"(只|仅|只需要|仅需要|只用|仅用).{0,10}(关注|考虑|处理|识别)", compact):
            return True
        if re.search(r"(不考虑|忽略|不要管).{0,12}(之前|原来|原有|上次|已打码|已有)", compact):
            return True
    return False


def is_additive_instruction(text: str) -> bool:
    if is_strict_only_instruction(text):
        return False
    for clause in split_instruction_clauses(text):
        compact = compact_text(clause)
        if not compact:
            continue
        if re.search(r"(保留|保持|沿用|继续使用).{0,12}(原图|原来|原有|之前|上次|已打码|已有|已识别)", compact):
            return True
        if re.search(r"(补充|新增|追加|再|继续|同时).{0,8}(打码|脱敏|马赛克|遮盖|隐藏|屏蔽)", compact):
            return True
        if re.search(r"(也|一起|一并|同样).{0,4}(打码|脱敏|马赛克|遮盖|隐藏|屏蔽)", compact):
            return True
    return False


def instruction_terms_for_matching(text: str, match_mode: str) -> list[str]:
    prefix = "exact:" if match_mode == "exact" else "fuzzy:"
    return [f"{prefix}{term}" for term in extract_redaction_terms_from_instruction(text)]


def instruction_term_mask_ids(boxes: list[OcrBox], instruction_terms: list[str]) -> set[int]:
    if not instruction_terms:
        return set()
    return {box.id for box in boxes if box.text and term_matches(box.text, instruction_terms)}


def is_no_mask_instruction(text: str) -> bool:
    for clause in split_instruction_clauses(text):
        compact = compact_text(clause)
        if not compact:
            continue
        if re.fullmatch(r"(不需要|不用|不要|无需|别)(任何|全部|所有)?(打码|脱敏|马赛克|遮盖|隐藏|屏蔽)(了|处理)?", compact):
            return True
        if re.search(r"(这张图|本张图|当前图|整张图|整幅图|该图|图片|图中|全部|所有).{0,8}(不需要|不用|不要|无需|别).{0,8}(打码|脱敏|马赛克|遮盖|隐藏|屏蔽)", compact):
            return True
        if re.search(r"\b(no|without|skip)\s+(mask|mosaic|redact)\b", clause, re.IGNORECASE):
            return True
    return False


def process_image(
    ref: str,
    source_path: Path,
    output_images_dir: Path,
    terms: list[str],
    args: argparse.Namespace,
) -> tuple[str, str, int, list[dict[str, Any]]]:
    boxes = load_ocr_boxes(source_path, args.min_confidence)
    enhance_with_prompt = bool(getattr(args, "enhance_with_prompt", False))
    image_instruction = str(getattr(args, "image_instruction", "") or "")
    ai_image_instruction = str(getattr(args, "ai_image_instruction", "") or image_instruction)
    instruction_terms = instruction_terms_for_matching(image_instruction, getattr(args, "match_mode", "fuzzy"))
    extra_terms = list(getattr(args, "extra_terms", []) or [])
    if bool(getattr(args, "strict_terms", False)):
        effective_terms = extra_terms + instruction_terms
    else:
        effective_terms = terms + extra_terms + instruction_terms
    local_mask_ids = find_local_mask_ids(boxes, effective_terms, getattr(args, "mask_all_text", False))
    forced_instruction_ids = instruction_term_mask_ids(boxes, instruction_terms)
    mask_ids = set(local_mask_ids)
    no_mask_instruction = bool(image_instruction and is_no_mask_instruction(image_instruction))
    if no_mask_instruction:
        mask_ids = set()
        enhance_with_prompt = False

    if args.mode == "cpa" and not no_mask_instruction:
        api_key = args.api_key or os.environ.get("CPA_API_KEY", "")
        if not api_key:
            raise RuntimeError("已选择 CPA 模式，但没有提供 API Key。可设置环境变量 CPA_API_KEY，或加 --api-key。")
        try:
            ai_mask_ids = cpa_classify_mask_ids(
                boxes,
                api_key,
                args.base_url,
                args.model,
                effective_terms,
                args.match_mode,
                getattr(args, "industry_prompt", "") or "",
                ai_image_instruction,
                local_mask_ids,
                enhance_with_prompt or bool(ai_image_instruction.strip()),
            )
            if enhance_with_prompt or ai_image_instruction.strip():
                mask_ids = ai_mask_ids
                if bool(getattr(args, "preserve_local_mask_ids", False)):
                    mask_ids.update(local_mask_ids)
                mask_ids.update(forced_instruction_ids)
            else:
                mask_ids.update(ai_mask_ids)
            mask_ids = filter_standard_mask_ids(boxes, mask_ids, effective_terms)
        except Exception as exc:  # noqa: BLE001
            print(f"CPA 判断失败，已继续使用本地规则：{exc}", file=sys.stderr)

    with Image.open(source_path) as opened:
        image = ImageOps.exif_transpose(opened)
        if image.mode not in {"RGB", "RGBA"}:
            image = image.convert("RGBA" if "A" in image.getbands() else "RGB")
        width, height = image.size
        selected = [box for box in boxes if box.id in mask_ids]
        regions = [expand_bbox(box.bbox, width, height, args.padding) for box in selected]
        output = apply_mosaic(image, regions, args.block_size) if regions else image.copy()
        matches = [
            {
                "id": box.id,
                "text": box.text,
                "bbox": list(box.bbox),
            }
            for box in selected
        ]

    output_images_dir.mkdir(parents=True, exist_ok=True)
    source_name = f"{safe_stem(ref)}_original.png"
    source_preview_path = output_images_dir / source_name
    image.save(source_preview_path, format="PNG")
    out_name = f"{safe_stem(ref)}_mosaic.png"
    out_path = output_images_dir / out_name
    output.save(out_path, format="PNG")
    return f"images/{source_name}", f"images/{out_name}", len(regions), matches


def replace_refs(content: str, replacements: dict[str, str]) -> str:
    def md_repl(match: re.Match[str]) -> str:
        url, prefix, suffix = parse_markdown_target(match.group("target"))
        new_ref = replacements.get(url)
        if not new_ref:
            return match.group(0)
        target = rebuild_markdown_target(new_ref, prefix, suffix)
        return f"![{match.group('alt')}]({target})"

    def html_quoted_repl(match: re.Match[str]) -> str:
        new_ref = replacements.get(match.group("src"))
        if not new_ref:
            return match.group(0)
        quote = match.group("quote")
        return f"{match.group('prefix')}{quote}{new_ref}{quote}{match.group('suffix')}"

    def html_unquoted_repl(match: re.Match[str]) -> str:
        new_ref = replacements.get(match.group("src"))
        if not new_ref:
            return match.group(0)
        return f"{match.group('prefix')}{new_ref}{match.group('suffix')}"

    content = MARKDOWN_IMAGE_RE.sub(md_repl, content)
    content = HTML_IMG_QUOTED_RE.sub(html_quoted_repl, content)
    content = HTML_IMG_UNQUOTED_RE.sub(html_unquoted_repl, content)
    return content


def write_report(
    report_path: Path,
    input_path: Path,
    output_path: Path,
    results: list[ProcessResult],
    mode: str,
) -> None:
    total = len(results)
    ok = sum(1 for item in results if item.status == "ok")
    masked = sum(item.masked_regions for item in results)
    failed = [item for item in results if item.status != "ok"]

    lines = [
        "# CSDN 图片脱敏报告",
        "",
        f"- 源文件：`{input_path}`",
        f"- 输出文件：`{output_path}`",
        f"- 处理模式：`{mode}`",
        f"- 图片总数：{total}",
        f"- 成功处理：{ok}",
        f"- 打码区域：{masked}",
        f"- 失败数量：{len(failed)}",
        "",
        "## 图片明细",
        "",
        "| 序号 | 状态 | 打码区域 | 输出图片 | 备注 |",
        "|---:|---|---:|---|---|",
    ]
    for index, item in enumerate(results, start=1):
        note = item.error or ""
        out = item.output_ref or ""
        lines.append(f"| {index} | {item.status} | {item.masked_regions} | `{out}` | {note} |")

    lines.extend(["", "## 打码内容明细", "", "| 图片序号 | 打码内容 | 坐标 |", "|---:|---|---|"])
    any_match = False
    for index, item in enumerate(results, start=1):
        for match in item.matches or []:
            any_match = True
            safe_text = str(match.get("text", "")).replace("|", "\\|")
            lines.append(f"| {index} | {safe_text} | `{match.get('bbox', '')}` |")
    if not any_match:
        lines.append("| - | 未发现需要打码的文字 | - |")

    write_text(report_path, "\n".join(lines) + "\n")


def build_report_image_entry(index: int, item: ProcessResult) -> dict[str, Any]:
    return {
        "index": index,
        "sourceRef": item.original_ref,
        "originalRef": display_ref(item.original_ref),
        "status": item.status,
        "outputRef": item.output_ref,
        "sourcePath": item.source_path,
        "sourceImageRef": item.source_image_ref,
        "rerunInputRef": item.rerun_input_ref,
        "lastRerunSource": item.last_rerun_source,
        "maskedRegions": item.masked_regions,
        "error": item.error,
        "matches": item.matches or [],
        "note": item.note,
        "updatedAt": item.updated_at,
    }


def write_report_json(report_path: Path, results: list[ProcessResult]) -> None:
    payload = {
        "images": [
            build_report_image_entry(index, item)
            for index, item in enumerate(results, start=1)
        ]
    }
    write_text(report_path, json.dumps(payload, ensure_ascii=False, indent=2))


def process_document(args: argparse.Namespace) -> Path:
    progress = getattr(args, "progress_callback", None)

    def report_progress(message: str, event_type: str = "progress", data: dict[str, Any] | None = None) -> None:
        if callable(progress):
            try:
                progress(message, event_type, data)
            except TypeError:
                progress(message)

    def wait_if_paused() -> None:
        pause_event = getattr(args, "pause_event", None)
        if pause_event is None or pause_event.is_set():
            return
        report_progress("任务已暂停。点击“继续”后会从下一步接着处理。", "paused", {"jobId": getattr(args, "job_id", "")})
        while not pause_event.is_set():
            time.sleep(0.2)
        report_progress("任务已继续。", "resumed", {"jobId": getattr(args, "job_id", "")})

    input_path = Path(args.input).expanduser().resolve()
    if not input_path.exists():
        raise FileNotFoundError(f"找不到文件：{input_path}")

    output_dir = Path(args.output_dir).expanduser().resolve() if args.output_dir else input_path.with_name(f"{input_path.stem}_csdn_safe")
    output_dir.mkdir(parents=True, exist_ok=True)
    images_dir = output_dir / "images"
    cache_dir = output_dir / ".source_cache"

    content = read_text(input_path)
    refs = [ref for ref in collect_image_refs(content) if looks_like_image(ref)]
    report_progress(
        f"已读取文章正文，发现 {len(refs)} 张图片。",
        "scan_done",
        {"totalImages": len(refs), "jobId": getattr(args, "job_id", "")},
    )
    terms_path: Path | None = None
    if args.terms:
        candidate = Path(args.terms).expanduser()
        if not candidate.is_absolute() and not candidate.exists():
            script_candidate = Path(__file__).resolve().parent / candidate
            candidate = script_candidate if script_candidate.exists() else candidate
        terms_path = candidate.resolve()
    terms = load_terms(terms_path)

    replacements: dict[str, str] = {}
    results: list[ProcessResult] = []

    for index, ref in enumerate(refs, start=1):
        wait_if_paused()
        report_progress(
            f"开始处理第 {index}/{len(refs)} 张图片。",
            "image_start",
            {"index": index, "total": len(refs), "jobId": getattr(args, "job_id", ""), "originalRef": display_ref(ref)},
        )
        source_path, error = materialize_image(ref, input_path.parent, cache_dir)
        if not source_path:
            report_progress(f"第 {index} 张图片读取失败：{error}")
            failed_result = ProcessResult(original_ref=ref, status="failed", error=error, updated_at=int(time.time()))
            results.append(failed_result)
            report_progress(
                f"第 {index} 张图片读取失败。",
                "image_failed",
                {"image": build_report_image_entry(index, failed_result), "processed": index, "total": len(refs), "jobId": getattr(args, "job_id", "")},
            )
            continue
        try:
            source_image_ref, output_ref, masked_count, matches = process_image(ref, source_path, images_dir, terms, args)
            replacements[ref] = output_ref
            result = ProcessResult(
                original_ref=ref,
                status="ok",
                output_ref=output_ref,
                source_path=str(source_path),
                source_image_ref=source_image_ref,
                masked_regions=masked_count,
                matches=matches,
                updated_at=int(time.time()),
            )
            results.append(result)
            report_progress(
                f"第 {index} 张图片完成，打码 {masked_count} 处。",
                "image_done",
                {"image": build_report_image_entry(index, result), "processed": index, "total": len(refs), "jobId": getattr(args, "job_id", "")},
            )
        except Exception as exc:  # noqa: BLE001
            report_progress(f"第 {index} 张图片处理失败：{exc}")
            failed_result = ProcessResult(original_ref=ref, status="failed", source_path=str(source_path), error=str(exc), updated_at=int(time.time()))
            results.append(failed_result)
            report_progress(
                f"第 {index} 张图片处理失败。",
                "image_failed",
                {"image": build_report_image_entry(index, failed_result), "processed": index, "total": len(refs), "jobId": getattr(args, "job_id", "")},
            )

    wait_if_paused()
    report_progress("正在替换文章里的图片引用，并生成报告。")
    output_content = replace_refs(content, replacements)
    output_path = output_dir / f"{input_path.stem}_csdn_safe{input_path.suffix or '.md'}"
    write_text(output_path, output_content)
    write_report(output_dir / "report.md", input_path, output_path, results, args.mode)
    write_report_json(output_dir / "report.json", results)
    report_progress("文章脱敏输出已生成。")
    return output_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="批量给 CSDN Markdown/HTML 文章里的图片做敏感信息马赛克。")
    parser.add_argument("input", help="要处理的 Markdown 或 HTML 文件路径")
    parser.add_argument("--output-dir", help="输出目录，默认生成在源文件旁边")
    parser.add_argument("--terms", default="sensitive_terms.txt", help="敏感词文件，一行一个词；默认 sensitive_terms.txt")
    parser.add_argument("--mode", choices=["local", "cpa"], default="local", help="local=本地规则；cpa=本地 OCR + CPA 辅助判断")
    parser.add_argument("--api-key", help="CPA API Key；也可用环境变量 CPA_API_KEY")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="CPA Base URL")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="CPA 判断模型")
    parser.add_argument("--match-mode", choices=["fuzzy", "exact"], default="fuzzy", help="敏感词匹配模式")
    parser.add_argument("--mask-all-text", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--enhance-with-prompt", action="store_true", help="结合行业提示词增强判断，减少标准字段标签误伤")
    parser.add_argument("--industry-prompt", default="", help="行业提示词内容")
    parser.add_argument("--image-instruction", default="", help="单张图片重新识别时的用户补充要求")
    parser.add_argument("--min-confidence", type=float, default=0.45, help="OCR 最低置信度，默认 0.45")
    parser.add_argument("--padding", type=int, default=8, help="马赛克向外扩展像素，默认 8")
    parser.add_argument("--block-size", type=int, default=12, help="马赛克颗粒大小，默认 12")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        output_path = process_document(args)
    except Exception as exc:  # noqa: BLE001
        print(f"处理失败：{exc}", file=sys.stderr)
        return 1

    print(f"处理完成：{output_path}")
    print(f"报告位置：{output_path.parent / 'report.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
