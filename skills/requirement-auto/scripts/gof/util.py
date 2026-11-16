"""Shared helpers: refusals, canonical JSON and digests, exclusive file writes, and the allowed targets (util.ts)."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import secrets
from typing import Any, NoReturn
from urllib.parse import urlsplit

from . import js
from .js import canonical  # noqa: F401  (re-exported: every module digests through this one)


class Blocked(Exception):
    """A refusal with a message for the user. The CLI prints it as {"status":"blocked","reason":…} and exits 2."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message

    def __str__(self) -> str:
        return self.message


class WriteSent(Blocked):
    """A refusal after a write went out: what happened has to be read back (reconcile); planning and sending again could append twice."""

    def __init__(self, message: str, batch: str) -> None:
        super().__init__(message)
        self.batch = batch


def need(condition: Any, message: str) -> None:
    if not condition:
        raise Blocked(message)


def fail(message: str) -> NoReturn:
    raise Blocked(message)


NODE = "GH7cwIxzZi3gVdk2s3tc1dDbnQc"
TARGET = f"https://centurygames.feishu.cn/wiki/{NODE}"
FIXED_TAG = "玩法卖点（单选，必选）/展示向/展示向"
CSV_HEADERS = ["脚本名称", "脚本内容", "创意标签"]


def sha256(data: str | bytes) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8", "surrogatepass")
    return hashlib.sha256(data).hexdigest()


def digest(value: Any) -> str:
    return sha256(canonical(value))


def read_json(file: str) -> Any:
    """A JSON file as JSON.parse reads it (a leading BOM is dropped)."""
    with open(file, "r", encoding="utf-8") as handle:
        text = handle.read()
    if text.startswith("\ufeff"):
        text = text[1:]
    return json.loads(text)


def write_json_new(file: str, data: Any) -> None:
    """
    Exclusive create: an existing file is an error, never overwritten. The content is written to a side file first and linked into
    place, so a process that dies half-way leaves no truncated file under the real name (an event log with one cannot be read at all).
    """
    side = f"{file}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
    with open(side, "x", encoding="utf-8", newline="") as handle:
        handle.write(js.stringify(data, 2) + "\n")
    try:
        try:
            os.link(side, file)
        except FileExistsError:
            raise FileExistsError(errno.EEXIST, f"EEXIST: file already exists, link '{side}' -> '{file}'") from None
    finally:
        try:
            os.remove(side)
        except OSError:
            pass


def write_text_new(file: str, text: str) -> None:
    """``fs.writeFileSync(file, text, {flag: 'wx'})`` in UTF-8, the text as it is (no newline translation)."""
    with open(file, "x", encoding="utf-8", newline="") as handle:
        handle.write(text)


def is_inside(directory: str, file: str) -> bool:
    """Is `file` somewhere below `directory`?"""
    base = os.path.abspath(directory)
    target = os.path.abspath(file)
    try:
        relative = os.path.relpath(target, base)
    except ValueError:  # another drive
        return False
    return relative != "." and not relative.startswith("..") and not os.path.isabs(relative)


def is_object(value: Any) -> bool:
    return isinstance(value, dict)


def _parse_url(url: Any) -> Any:
    if not isinstance(url, str):
        return None
    try:
        parts = urlsplit(url)
        _ = parts.port  # a bad port is a bad URL
    except ValueError:
        return None
    if not parts.scheme or (parts.scheme in ("http", "https") and not parts.hostname):
        return None
    return parts


def _port_ok(parts: Any) -> bool:
    try:
        return parts.port in (None, 443)
    except ValueError:
        return False


def check_target(url: str) -> None:
    p = _parse_url(url)
    need(p and p.scheme == "https" and (p.hostname or "") == "centurygames.feishu.cn" and _port_ok(p)
         and not p.username and not p.password and re.sub(r"/+$", "", p.path) == f"/wiki/{NODE}", "实际页面不是获准的测试进度表节点")


def check_cls_url(url: str) -> None:
    p = _parse_url(url)
    need(p and p.scheme == "https" and (p.hostname or "") == "creative.centurygames.cn" and _port_ok(p)
         and not p.username and not p.password, "草稿 URL 必须是实际 CLS 页面")


def feishu_doc_path(url: Any, kinds: str, trailing_slash: bool = False) -> str | None:
    """A Feishu document link: https, the company host, no credentials, /wiki/<token> or /docx/<token>. Returns the path."""
    p = _parse_url(url)
    if not p or p.scheme != "https" or (p.hostname or "") != "centurygames.feishu.cn" or p.username or p.password or not _port_ok(p):
        return None
    path = p.path or "/"
    pattern = f"/({kinds})/[A-Za-z0-9]+{'/?' if trailing_slash else ''}"
    return path if re.fullmatch(pattern, path) else None


def cell_text(value: Any, label: str) -> str:
    need(isinstance(value, str), f"{label} 必须是文本")
    need(not re.search(r"[\t\r\n\x00]", value), f"{label} 不能包含表格分隔符或换行")
    need(not js.trim_start(value).startswith("="), f"{label} 不能作为公式粘贴")
    return value


def safe_filename(value: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value)
    cleaned = re.sub(r"^[ .]+|[ .]+$", "", cleaned)
    return "".join(list(cleaned)[:80])
