"""Guards around the CLS login, for a host where several users share one computer (cls-guard.ts).

 - The account pin. CLS cannot tell us which Feishu user a company account belongs to, so the first account that logs in on a user's profile
   is remembered (id and name only) and any later login by a different account is refused. It does not prove that the first login was the
   right person, but it stops a wrong account from replacing it unnoticed.
 - The QR picture's record, below: it removes the picture once the login is done, and a browser nobody scanned for.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from . import js
from .util import Blocked


def read_pin(file: str) -> Optional[dict]:
    try:
        with open(file, "r", encoding="utf-8") as handle:
            value = json.load(handle)
    except Exception:
        return None
    if not isinstance(value, dict) or not js.is_safe_int(value.get("id")):
        return None
    name = value.get("name")
    pinned = value.get("pinned_at")
    return {"id": value["id"], "name": "" if name is None else js.to_str(name), "pinned_at": "" if pinned is None else js.to_str(pinned)}


CORRUPT_PIN = "这个用户的 CLS 账号绑定记录损坏（文件存在但读不出），已拒绝登录；需要管理员核对或清除 cls/account.json 后重新登录。"


def pin_state(file: str) -> dict:
    """The pin as it is on disk: absent, good, or present but unreadable (which must never count as "no pin")."""
    if not os.path.exists(file):
        return {"state": "none"}
    pin = read_pin(file)
    return {"state": "ok", "pin": pin} if pin else {"state": "corrupt"}


def _iso(now: Optional[Callable[[], datetime]]) -> str:
    return js.iso(now().timestamp() * 1000) if now else js.iso()


def check_pin(file: str, account: dict, now: Optional[Callable[[], datetime]] = None) -> None:
    """Remembers the first account seen on this profile, and refuses a different one afterwards."""
    present = pin_state(file)
    if present["state"] == "corrupt":
        raise Blocked(CORRUPT_PIN)
    pin = present.get("pin")
    if not pin:
        os.makedirs(os.path.dirname(file), exist_ok=True)
        try:
            with open(file, "x", encoding="utf-8", newline="") as handle:
                handle.write(js.stringify({"id": account["id"], "name": account["name"], "pinned_at": _iso(now)}, 2) + "\n")
            return
        except FileExistsError:
            again = pin_state(file)  # another command of this user pinned it a moment ago
            if again["state"] != "ok":
                raise Blocked(CORRUPT_PIN) from None
            pin = again["pin"]
    if pin and pin["id"] != account["id"]:
        raise Blocked(f"这个用户的 CLS 账号已绑定为“{pin['name']}”，这次登录的是另一个账号，已拒绝。请用“{pin['name']}”的公司账号登录；"
                      "如果确实要更换账号，需要管理员清除该用户的 CLS 登录资料后重新登录。")
    # The same account under another name (an intern taken on, say): the consent page is checked against the name it goes by now. Written
    # aside and moved into place, so that a pin is never left half written.
    if pin and account.get("name") and account["name"] != pin["name"]:
        aside = f"{file}.{os.getpid()}.tmp"
        with open(aside, "w", encoding="utf-8", newline="") as handle:
            handle.write(js.stringify({"id": pin["id"], "name": account["name"], "pinned_at": pin["pinned_at"]}, 2) + "\n")
        os.replace(aside, file)


# The QR picture. `cls login --qr` leaves a hidden browser running on the sign-in page and a picture of its code in the user's delivery
# folder; the record says where the picture is and since when, so that the picture can be removed once the login is done and a browser
# nobody scanned for is not left running for good.

QR_MAX_AGE_MS = 15 * 60_000


def read_qr(file: str) -> Optional[dict]:
    try:
        with open(file, "r", encoding="utf-8") as handle:
            value = json.load(handle)
    except Exception:
        return None
    if isinstance(value, dict) and isinstance(value.get("path"), str) and js.is_num(value.get("created_at")):
        return {"path": value["path"], "created_at": value["created_at"]}
    return None


def write_qr(record_file: str, target: str, png: bytes, now: Optional[Callable[[], float]] = None) -> str:
    """Writes the picture. A file that is not the previous picture is never overwritten."""
    file = os.path.abspath(target)
    if not re.search(r"\.png\Z", file, re.IGNORECASE):
        raise Blocked("二维码文件需要是 .png")
    previous = read_qr(record_file)
    if os.path.exists(file) and (previous or {}).get("path") != file:
        raise Blocked("这个文件已存在，不覆盖；换一个新的 .png 文件名")
    os.makedirs(os.path.dirname(file), exist_ok=True)
    with open(file, "wb") as handle:
        handle.write(png)
    if previous and previous["path"] != file:
        try:
            os.remove(previous["path"])
        except OSError:
            pass
    os.makedirs(os.path.dirname(record_file), exist_ok=True)
    with open(record_file, "w", encoding="utf-8", newline="") as handle:
        handle.write(js.stringify({"path": file, "created_at": int(now()) if now else js.now_ms()}))
    return file


def clear_qr(record_file: str) -> None:
    """The login is done (or given up): the picture and its record go away."""
    record = read_qr(record_file)
    if record and re.search(r"\.png\Z", record["path"], re.IGNORECASE):
        try:
            os.remove(record["path"])
        except OSError:
            pass
    try:
        os.remove(record_file)
    except OSError:
        pass


def reap_stale_qr(record_file: str, profile_dir: str, now: Optional[Callable[[], float]] = None) -> None:
    """A code nobody scanned in a long time: close the hidden browser waiting for it and remove the picture."""
    from .chromium import attach_browser

    record = read_qr(record_file)
    current = now() if now else js.now_ms()
    if not record or current - record["created_at"] < QR_MAX_AGE_MS:
        return
    browser = attach_browser(profile_dir)
    if browser:
        browser.close()
    clear_qr(record_file)


class Online:
    """
    What the commands that go online need (cls-online.ts `Online`): a session, one look for a login already there, the pinned account, and
    a QR login. Tests give their own.
    """

    def session(self) -> dict:
        raise NotImplementedError

    def capture(self, wait_ms: int) -> dict:
        raise NotImplementedError

    def expected_account(self) -> Optional[dict]:
        return None

    login_qr: Optional[Callable[[str], dict]] = None


class GuardedOnline(Online):
    """The real session, with the account pin on every login."""

    def __init__(self, inner: Online, pin_file: str, now: Optional[Callable[[], datetime]] = None, qr_file: Optional[str] = None,
                 profile_dir: Optional[str] = None) -> None:
        self._inner = inner
        self._pin_file = pin_file
        self._now = now
        self._qr_file = qr_file
        self._profile_dir = profile_dir
        if inner.login_qr is not None:
            self.login_qr = lambda file: inner.login_qr(file)  # type: ignore[misc]

    def _reap(self) -> None:
        if self._qr_file and self._profile_dir:
            reap_stale_qr(self._qr_file, self._profile_dir, (lambda: self._now().timestamp() * 1000) if self._now else None)

    def _done(self) -> None:
        if self._qr_file:
            clear_qr(self._qr_file)

    def session(self) -> dict:
        self._reap()
        session = self._inner.session()
        check_pin(self._pin_file, session["account"], self._now)
        self._done()
        return session

    def capture(self, wait_ms: int) -> dict:
        self._reap()
        result = self._inner.capture(wait_ms)
        if result["state"] == "ok":
            check_pin(self._pin_file, result["account"], self._now)
            self._done()
        return result

    def expected_account(self) -> Optional[dict]:
        return read_pin(self._pin_file)


def guard_online(inner: Online, pin_file: str, now: Optional[Callable[[], datetime]] = None, qr_file: Optional[str] = None,
                 profile_dir: Optional[str] = None) -> Online:
    return GuardedOnline(inner, pin_file, now, qr_file, profile_dir)
