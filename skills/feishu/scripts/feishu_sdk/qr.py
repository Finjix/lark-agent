"""The sign-in QR picture.

`authorize --qr` leaves the hidden browser running on Feishu's sign-in page and a picture of its code in the user's delivery folder (the
workspace); the record says where the picture is and since when, so that the picture can be removed once the login is done, and a browser
nobody scanned for is not left running for good.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Callable

from .browser import attach_browser
from .errors import Blocked
from .jsutil import now_ms

QR_MAX_AGE_MS = 15 * 60_000
_PNG = re.compile(r"\.png\Z", re.IGNORECASE)


def read_qr(file: str) -> dict[str, Any] | None:
    try:
        with open(file, encoding="utf-8") as handle:
            value = json.loads(handle.read())
    except (OSError, ValueError):
        return None
    if isinstance(value, dict) and isinstance(value.get("path"), str) and isinstance(value.get("created_at"), (int, float)) and not isinstance(value.get("created_at"), bool):
        return {"path": value["path"], "created_at": value["created_at"]}
    return None


def check_qr_target(record_file: str, target: str) -> None:
    """Whether the picture may go to this file: a new one, or the previous picture. Asked before a browser is left waiting for a scan."""
    file = os.path.abspath(target)
    if not _PNG.search(file):
        raise Blocked("The QR file must be a .png")
    record = read_qr(record_file)
    if os.path.exists(file) and (record or {}).get("path") != file:
        raise Blocked("That file already exists and is not overwritten: use a new .png file name")


def write_qr(record_file: str, target: str, png: bytes, now: Callable[[], float] = now_ms) -> str:
    """Writes the picture. A file that is not the previous picture is never overwritten."""
    check_qr_target(record_file, target)
    file = os.path.abspath(target)
    previous = read_qr(record_file)
    os.makedirs(os.path.dirname(file), exist_ok=True)
    with open(file, "wb") as handle:
        handle.write(png)
    if previous and previous["path"] != file:
        _remove(previous["path"])
    os.makedirs(os.path.dirname(record_file), exist_ok=True)
    with open(record_file, "w", encoding="utf-8", newline="") as handle:
        handle.write(json.dumps({"path": file, "created_at": int(now())}, ensure_ascii=False, separators=(",", ":")))
    return file


def clear_qr(record_file: str) -> None:
    """The login is done (or given up): the picture and its record go away."""
    record = read_qr(record_file)
    if record and _PNG.search(record["path"]):
        _remove(record["path"])
    _remove(record_file)


def close_browser(profile_dir: str) -> None:
    """Closes the hidden browser of this profile if one is running (left waiting for a scan, say)."""
    browser = attach_browser(profile_dir)
    if browser:
        browser.close()


def reap_stale_qr(record_file: str, profile_dir: str, now: Callable[[], float] = now_ms) -> None:
    """A code nobody scanned in a long time: close the hidden browser waiting for it and remove the picture."""
    record = read_qr(record_file)
    if not record or now() - record["created_at"] < QR_MAX_AGE_MS:
        return
    close_browser(profile_dir)
    clear_qr(record_file)


def _remove(file: str) -> None:
    try:
        os.remove(file)
    except FileNotFoundError:
        pass
