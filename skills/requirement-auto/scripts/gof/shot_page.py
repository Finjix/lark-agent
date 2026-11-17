"""What every picture the skill hands back has in common.

This user's own hidden browser (the profile the logins live in) with one page in it, a window of a chosen size, the page's own answers from
CLS counted, and a PNG that is never written over an existing file. The pictures only look: nothing is typed, and a click happens nowhere
(see creative_shot.py, draft_shot.py, sheet_shot.py).
"""

from __future__ import annotations

import base64
import json
import os
import re
import threading
import time
from typing import Any, Callable, Mapping, Optional, TypeVar
from urllib.parse import urlsplit

from .chromium import Cdp, CdpError, find_browser, open_browser
from .cls_session import NO_BROWSER
from .util import Blocked, need

T = TypeVar("T")

#: The current game the CLS app keeps in local storage (DD-CLS-GLOBAL-CURGAME).
GAME_KEY = "DD-CLS-GLOBAL-CURGAME"
EXISTS = "the screenshot file exists already and is not overwritten; give a new .png file name"


def pause(ms: float) -> None:
    time.sleep(ms / 1000)


def not_loaded(what: str, reason: str) -> str:
    """A page that did not load at all: the network or the site, not the login (which a refusal would otherwise send the user to renew)."""
    return f"the {what} did not open ({reason}): the network or the site is unavailable for now, the login has not lapsed; take the screenshot again a little later"


def check_picture_target(target: str) -> str:
    """Checks the target of a picture before anything is started: a .png that does not exist yet."""
    file = os.path.abspath(target)
    need(re.search(r"\.png\Z", file, re.IGNORECASE), "the screenshot file must be a .png")
    need(not os.path.exists(file), EXISTS)
    return file


def write_picture(target: str, png: bytes) -> str:
    """Writes the picture; an existing file is never overwritten."""
    file = check_picture_target(target)
    os.makedirs(os.path.dirname(file), exist_ok=True)
    with open(file, "xb") as handle:
        handle.write(png)
    return file


def _origin_of(url: str) -> str:
    parts = urlsplit(url)
    default = {"http": 80, "https": 443}.get(parts.scheme)
    port = f":{parts.port}" if parts.port and parts.port != default else ""
    return f"{parts.scheme}://{parts.hostname}{port}"


class ShotPage:
    """One page in this user's hidden browser, with what the pictures need to wait for it."""

    def __init__(self, cdp: Cdp, session_id: str, origin: str, wait: int) -> None:
        self.cdp = cdp
        self.session_id = session_id
        self.origin = origin
        self.wait = wait
        #: How many times each CLS /api/ path has been answered with 200 since the page was opened.
        self.answered: dict[str, int] = {}
        self._inflight: set[str] = set()
        self._last_activity = time.monotonic()
        self._failed: Optional[str] = None
        self._lock = threading.Lock()

        def on_response(params: dict, source: Optional[str]) -> None:
            if source != session_id:
                return
            try:
                response = params.get("response") or {}
                url = str(response.get("url") or "")
                if _origin_of(url) == origin and urlsplit(url).path.startswith("/api/") and response.get("status") == 200:
                    path = urlsplit(url).path
                    with self._lock:
                        self.answered[path] = self.answered.get(path, 0) + 1
            except Exception:
                pass  # not a URL this cares about

        def track(start: bool) -> Callable[[dict, Optional[str]], None]:
            def handler(params: dict, source: Optional[str]) -> None:
                if source != session_id:
                    return
                with self._lock:
                    if start:
                        self._inflight.add(str(params.get("requestId")))
                    else:
                        self._inflight.discard(str(params.get("requestId")))
                    self._last_activity = time.monotonic()
            return handler

        cdp.on("Network.responseReceived", on_response)
        cdp.on("Network.requestWillBeSent", track(True))
        cdp.on("Network.loadingFinished", track(False))
        cdp.on("Network.loadingFailed", track(False))

    def count(self, path: str) -> int:
        with self._lock:
            return self.answered.get(path, 0)

    def quiet(self, quiet_ms: int, max_ms: int) -> bool:
        """Waits until the page has had no request in flight for `quiet_ms` (at most `max_ms` in all): its data has come in."""
        end = time.monotonic() + max_ms / 1000
        while time.monotonic() < end:
            with self._lock:
                idle = not self._inflight and time.monotonic() - self._last_activity >= quiet_ms / 1000
            if idle:
                return True
            pause(200)
        return False

    def evaluate(self, expression: str) -> Any:
        result = self.cdp.send("Runtime.evaluate", {"expression": expression, "returnByValue": True, "awaitPromise": True}, self.session_id)
        return (result.get("result") or {}).get("value")

    def where(self) -> str:
        """`host/path` of where the page is now ('' when it cannot be told)."""
        try:
            href = str(self.evaluate("location.href"))
            parts = urlsplit(href)
            if not parts.scheme:
                return ""
            host = parts.hostname or ""
            if parts.port:
                host += f":{parts.port}"
            path = parts.path or ("/" if parts.scheme in ("http", "https") else "")
            return f"{host}{path}"
        except Exception:
            return ""

    def until(self, condition: Callable[[], bool], ms: int) -> bool:
        end = time.monotonic() + ms / 1000
        while time.monotonic() < end:
            if condition():
                return True
            pause(250)
        return bool(condition())

    def navigate(self, url: str) -> None:
        # A page that cannot be reached is not a sign-in page: what the last navigation said is kept for load_error.
        answer = self.cdp.send("Page.navigate", {"url": url}, self.session_id)
        error = answer.get("errorText")
        self._failed = error if error and error != "net::ERR_ABORTED" else None

    def load_error(self) -> Optional[str]:
        """Why the page did not load (the network, a proxy, DNS: Chrome's error page, or a navigation that failed), or None when it did."""
        try:
            href = str(self.evaluate("location.href"))
        except Exception:
            href = ""
        return (self._failed or "the page did not open") if href.startswith("chrome-error:") else self._failed

    def resize(self, width: int, height: int) -> None:
        self.cdp.send("Emulation.setDeviceMetricsOverride", {"width": width, "height": height, "deviceScaleFactor": 1, "mobile": False}, self.session_id)

    def capture(self) -> bytes:
        """The page as it is now, as PNG bytes."""
        return base64.b64decode(self.cdp.send("Page.captureScreenshot", {"format": "png"}, self.session_id)["data"])

    def images_drawn(self, selector: str, ms: int) -> None:
        """Waits for the images on the page to be drawn (up to ms)."""
        self.until(lambda: self.evaluate(f"[...document.querySelectorAll({json.dumps(selector, ensure_ascii=False)})].every(i => i.complete)") is True, ms)

    def open_app(self) -> bool:
        """Opens the CLS app (the requirements page) and waits until it has asked CLS who is logged in and been answered."""
        before = self.count("/api/userinfo")
        self.navigate(f"{self.origin}/requirements")
        if self._failed:
            return False
        return self.until(lambda: self.count("/api/userinfo") > before, self.wait)

    def open_app_for_game(self, game: dict) -> bool:
        """Opens the CLS app with `game` as the current game (the app reads it once, at start); False when the app never got its login answered."""
        if not self.open_app():
            return False
        key = json.dumps(GAME_KEY)
        current = self.evaluate(f"(() => {{ try {{ return JSON.parse(localStorage.getItem({key})); }} catch {{ return null; }} }})()")
        if isinstance(current, dict) and current.get("id") == game.get("id"):
            return True
        # The app reads the current game once, when it starts: set it, then start the app again.
        value = json.dumps(json.dumps(game, ensure_ascii=False, separators=(",", ":")), ensure_ascii=False)
        self.evaluate(f"localStorage.setItem({key}, {value}); true")
        return self.open_app()


def with_shot_page(profile_dir: str, origin: str, width: int, height: int, run: Callable[[ShotPage], T], exe: Optional[str] = None,
                   env: Optional[Mapping[str, str]] = None, wait_ms: Optional[int] = None) -> T:
    exe = exe or find_browser(env)
    if not exe:
        raise Blocked(NO_BROWSER)
    wait = wait_ms if wait_ms is not None else 30_000
    browser = open_browser(exe, profile_dir, env=env)
    try:
        cdp = browser.cdp
        infos = cdp.send("Target.getTargets").get("targetInfos") or []
        target_id = next((t["targetId"] for t in infos if t.get("type") == "page"), None) or cdp.send("Target.createTarget", {"url": "about:blank"})["targetId"]
        session_id = cdp.send("Target.attachToTarget", {"targetId": target_id, "flatten": True})["sessionId"]
        page = ShotPage(cdp, session_id, origin, wait)
        cdp.send("Page.enable", {}, session_id)
        cdp.send("Network.enable", {}, session_id)
        user_agent = cdp.send("Browser.getVersion").get("userAgent")
        if user_agent:
            cdp.send("Network.setUserAgentOverride", {"userAgent": user_agent.replace("HeadlessChrome", "Chrome")}, session_id)
        page.resize(width, height)
        return run(page)
    finally:
        try:
            browser.close()
        except Exception:
            pass


_with_shot_page_untimed = with_shot_page


def with_shot_page(*args: Any, **kwargs: Any) -> Any:
    """Timed as `browser:screenshot`, opening the browser included."""
    from . import timing

    with timing.phase("browser:screenshot"):
        return _with_shot_page_untimed(*args, **kwargs)
