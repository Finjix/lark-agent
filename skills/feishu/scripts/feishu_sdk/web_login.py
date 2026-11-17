"""The Feishu login by device authorization, completed in the hidden browser instead of in the user's own browser.

The device flow (login start / login poll) needs somebody to open a link on accounts.feishu.cn, be logged in to Feishu there, and press
【授权】. The user's browser is not the only place that can do that: this user's hidden browser can too. If it is not logged in to Feishu, the
link lands on Feishu's QR sign-in page, which is shown to the user as a picture to scan with their phone. That one scan gives the browser a
Feishu web session, which also covers the company's single sign-on of sites other skills open on the same profile (requirement-auto's CLS),
so the user scans once for all of them.

The user has authorised this: the page is only ever reached through the verification link of this user's own pending device authorization,
【授权】 is the only thing pressed (never 【拒绝】), once per page, and who ends up logged in is still decided by the login itself (the account
has to be the user this process serves).
"""

from __future__ import annotations

import base64
import os
import re
import shutil
import time
from typing import Any, Mapping, Protocol
from urllib.parse import SplitResult, urlunsplit

from .browser import CdpError, find_browser, lock_profile, open_browser
from .consent import approve_consent
from .errors import Blocked, need
from .jsutil import host_of, now_ms, num, parse_url
from .paths import DataPaths
from .qr import check_qr_target, clear_qr, close_browser, reap_stale_qr, write_qr

Result = dict[str, Any]
FEISHU_HOST = re.compile(r"accounts\.(feishu\.cn|larksuite\.com)")


def _feishu(parts: SplitResult) -> bool:
    return FEISHU_HOST.fullmatch(parts.hostname or "") is not None


def is_login_page(parts: SplitResult) -> bool:
    return _feishu(parts) and "login" in (parts.path or "/")


def is_consent_page(parts: SplitResult) -> bool:
    return _feishu(parts) and (parts.path or "/").startswith("/open-apis/authen/v1/authorize")


def check_verification_url(raw: str, user_code: str) -> str:
    """The verification link of a pending device authorization, and nothing else: Feishu's accounts host, its device page, this request's code."""
    url = parse_url(raw) if isinstance(raw, str) else None
    if url is None:
        raise Blocked("The Feishu authorization link is not a valid address")
    need(url.scheme.lower() == "https" and _feishu(url) and (url.path or "/").startswith("/oauth/v1/device/verify"),
         "The Feishu authorization link is not the device-authorization page of the Feishu accounts service: refusing to open it")
    need(not user_code or user_code in raw, "The Feishu authorization link does not match the current user code: refusing to open it")
    return urlunsplit((url.scheme.lower(), url.netloc.lower(), url.path or "/", url.query, url.fragment))


def _sleep(ms: float) -> None:
    time.sleep(ms / 1000)


def drive_device_authorization(*, profile_dir: str, url: str, mode: str, wait_ms: int, settle_ms: int = 2_500, after_click_ms: int = 3_000,
                               exe: str | None = None, env: Mapping[str, str] | None = None) -> Result:
    """Opens (`start`: in a fresh page, taking a picture of the sign-in page) or watches (`finish`) the authorization page.

    Outcomes: {"state": "approved"} 【授权】 was pressed and the page moved on; {"state": "qr", "png": bytes} the browser is on the Feishu
    sign-in page, left running, and this is a picture of its code; {"state": "login_required"} still on the sign-in page: nobody has
    scanned (or the code has expired); {"state": "stuck", "where": host and path} neither a sign-in page nor an authorisation page, or the
    button did nothing.
    """
    exe = exe or find_browser(env)
    if not exe:
        raise Blocked("Neither Chrome nor Edge was found: install one, or set FEISHU_BROWSER to the browser's absolute path")
    start = mode == "start"
    browser = open_browser(exe, profile_dir, detach=start, env=env)
    keep_running = False
    try:
        cdp = browser.cdp
        targets = cdp.send("Target.getTargets").get("targetInfos") or []
        pages = [t for t in targets if t.get("type") == "page"]

        def host(raw: Any) -> str:
            parts = parse_url(str(raw or ""))
            return (parts.hostname or "") if parts else ""

        chosen = next((p for p in pages if FEISHU_HOST.fullmatch(host(p.get("url")))), None) if not start else None
        chosen = chosen or (pages[0] if pages else None)
        target_id = chosen["targetId"] if chosen else cdp.send("Target.createTarget", {"url": "about:blank"})["targetId"]
        # A page left over from an earlier session could be a second consent or sign-in page: this run looks at one page only.
        if start:
            for page in pages:
                if page.get("targetId") != target_id:
                    try:
                        cdp.send("Target.closeTarget", {"targetId": page.get("targetId")})
                    except CdpError:
                        pass
        session_id = cdp.send("Target.attachToTarget", {"targetId": target_id, "flatten": True})["sessionId"]
        cdp.send("Page.enable", {}, session_id)
        user_agent = cdp.send("Browser.getVersion").get("userAgent")
        if user_agent:
            cdp.send("Network.setUserAgentOverride", {"userAgent": str(user_agent).replace("HeadlessChrome", "Chrome")}, session_id)
        # A narrow, sharp page: the code fills most of the picture, which is read off a phone screen.
        if start:
            cdp.send("Emulation.setDeviceMetricsOverride", {"width": 640, "height": 720, "deviceScaleFactor": 2, "mobile": False}, session_id)

        def where() -> SplitResult | None:
            try:
                history = cdp.send("Page.getNavigationHistory", {}, session_id)
                entries = history.get("entries") or []
                index = history.get("currentIndex")
                entry = entries[index] if isinstance(index, int) and 0 <= index < len(entries) else {}
                return parse_url(str(entry.get("url") or ""))
            except (CdpError, AttributeError, TypeError):
                return None

        first = where() if chosen else None
        if start or not first or not _feishu(first):
            cdp.send("Page.navigate", {"url": url}, session_id)

        login_since: float | None = None
        last: SplitResult | None = None
        deadline = now_ms() + wait_ms
        while now_ms() < deadline:
            current = where()
            last = current or last
            if current and is_consent_page(current):
                attempt = approve_consent(cdp, session_id, is_consent_page)
                if attempt["state"] == "clicked":
                    # Feishu's page may stay on the same address after the answer, so there is nothing reliable to wait for: give it a moment,
                    # then leave nothing on a Feishu address behind (the next command that uses this profile starts from a blank page, and
                    # would otherwise find this page restored and take it for a consent request of its own). `login poll` confirms the login.
                    _sleep(after_click_ms)
                    try:
                        cdp.send("Page.navigate", {"url": "about:blank"}, session_id)
                    except CdpError:
                        pass
                    _sleep(300)
                    return {"state": "approved"}
                login_since = None
            elif current and is_login_page(current):
                if login_since is None:
                    login_since = now_ms()
                if start and now_ms() - login_since >= settle_ms:
                    shot = cdp.send("Page.captureScreenshot", {"format": "png"}, session_id)
                    keep_running = True
                    return {"state": "qr", "png": base64.b64decode(shot.get("data") or "")}
            else:
                login_since = None
            _sleep(500)
        if last and is_login_page(last):
            keep_running = not browser.launched
            return {"state": "login_required"}
        return {"state": "stuck", "where": f"{host_of(last)}{last.path or '/'}" if last else ""}
    finally:
        if keep_running:
            browser.cdp.close()
        else:
            try:
                browser.close()
            except Exception:  # noqa: BLE001 - the browser is going either way
                pass


class DeviceWeb(Protocol):
    def drive(self, *, url: str, mode: str, profile_dir: str) -> Result: ...


class RealDeviceWeb:
    def drive(self, *, url: str, mode: str, profile_dir: str) -> Result:
        return drive_device_authorization(profile_dir=profile_dir, url=url, mode=mode, wait_ms=20_000 if mode == "start" else 30_000)


real_device_web = RealDeviceWeb()


def authorize(status: Result, paths: DataPaths, web: DeviceWeb, qr_file: str | None) -> Result:
    """`authorize [--qr file.png]`.

    With --qr: open the pending authorization's link in the hidden browser; if that browser is not logged in to Feishu the sign-in QR goes
    to the file, otherwise 【授权】 is pressed at once. Without --qr: after the user has scanned, press 【授权】. Either way `login poll` then
    collects the login.
    """
    # A code nobody scanned in a long time goes first, its browser with it, whether or not its login is still pending.
    reap_stale_qr(paths.qr, paths.browser_profile)
    pending = status.get("pending_login")
    if not pending or not num(pending.get("expires_at")) > now_ms():
        logged_in = status.get("logged_in") is True
        return {"status": "no_pending", "logged_in": logged_in,
                "message": "Already logged in to Feishu; no authorization is pending" if logged_in
                else "No Feishu authorization is pending (or it expired): start with `login qr`"}
    url = check_verification_url(pending.get("verification_uri_complete"), str(pending.get("user_code") or ""))
    # Before the browser is left waiting for a scan: a picture that cannot be written would leave it running for nobody.
    if qr_file is not None:
        check_qr_target(paths.qr, qr_file)

    outcome = web.drive(url=url, mode="start" if qr_file is not None else "finish", profile_dir=paths.browser_profile)
    state = outcome.get("state")
    if state == "approved":
        clear_qr(paths.qr)
        return {"status": "approved", "message": "Pressed 【授权】 in the browser: `login poll` collects the login now"}
    if state == "qr":
        assert qr_file is not None
        try:
            file = write_qr(paths.qr, os.path.abspath(qr_file), outcome["png"])
        except BaseException:
            try:
                close_browser(paths.browser_profile)
            except Exception:  # noqa: BLE001
                pass
            raise
        return {
            "status": "qr_ready", "qr_file": file,
            "message": "QR saved: the user scans it with the Feishu phone app and confirms on the phone; it expires in a few minutes",
        }
    if state == "login_required":
        return {"status": "login_required", "message": "Still on the sign-in page: the user has not scanned yet, or the QR expired. Have the user scan it, or run `login qr` for a new one"}
    return {"status": "stuck", "where": outcome.get("where", ""),
            "message": "The page is neither the sign-in nor the consent page, or 【授权】 did nothing: see whether `login poll` reports it authorized; if not, run `login qr` again"}


def sign_out_browser(paths: DataPaths) -> None:
    """Signs the hidden browser out of Feishu web: its profile goes, with the session of whoever scanned, so the next `authorize --qr` shows a
    fresh sign-in code. Needed when that was somebody else: the browser would otherwise press 【授权】 for them at every login."""
    release = lock_profile(paths.browser_profile)
    try:
        close_browser(paths.browser_profile)
        _remove_tree(paths.browser_profile)
        clear_qr(paths.qr)
    finally:
        release()


def _remove_tree(folder: str, retries: int = 5, delay_ms: int = 200) -> None:
    for attempt in range(retries + 1):
        if not os.path.lexists(folder):
            return
        try:
            shutil.rmtree(folder)
            return
        except FileNotFoundError:
            return
        except OSError:
            if attempt >= retries:
                raise
            time.sleep(delay_ms / 1000)
