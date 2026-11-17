"""The CLS login.

CLS sits behind the company's single sign-on and offers no device flow or callback, so the one way to obtain a session is for a person to log
in on the normal web page. This module opens that page in a browser with this user's profile folder (so each user keeps their own login),
watches the page's ordinary API requests, and takes the Authorization header of the first one the server accepts. The token lives in this
process only: it is never written down, printed or put in a record. The browser is closed as soon as it has been seen, and the HTTP work is
done from here with the token.

What is never done: reading cookies, local storage or the profile files, typing into the page, or changing the page. The one thing that is
done to a page is on Feishu's consent page for the CLS application, after the person has scanned the code with their own phone: see
approve_consent.
"""

from __future__ import annotations

import base64
import os
import queue
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional

from .chromium import BROWSER_ENV, BrowserHandle, Cdp, CdpError, find_browser, open_browser
from .util import Blocked, parse_url
from .web import FetchError, Fetcher, fetch as default_fetch

CLS_ORIGIN = "https://creative.centurygames.cn"
ENTRY = "/requirements"
NO_BROWSER = f"no Chrome or Edge found: install one, or set {BROWSER_ENV} to the browser's absolute path"


@dataclass
class ConsentOptions:
    """
    After the scan, Feishu asks the person to let the CLS application (the company's own single sign-on app) use their account. The person
    has just scanned the code on their own phone for exactly this login, and has said once, for good, that this consent is to be given, so it
    is given here. The approval is narrow: only the named application, only when the page names the pinned account (if there is one), only
    the "授权" button, and once per page. Anything else is left alone and reported.
    """

    #: The only application whose request is approved; the page has to name it. Without it any application's page is approved.
    app: Optional[str] = None
    #: The account the login is pinned to, if any; the page has to name it too.
    account: Optional[str] = None
    #: The login is pinned to an account whose name is not known: the page cannot be checked, so nothing is clicked.
    unnamed_pin: bool = False
    #: Is this address a page that may hold the consent? Defaults to Feishu's authorize page.
    is_consent_page: Optional[Callable[[Any], bool]] = None


def _feishu_consent_page(url: Any) -> bool:
    return bool(re.fullmatch(r"accounts\.(feishu\.cn|larksuite\.com)", url.hostname or "")) and url.path.startswith("/open-apis/authen/v1/authorize")


# Runs in the consent page: its text (compared here, never printed or stored) and where its "授权" button is.
CONSENT_PROBE = """(() => {
  const norm = s => (s || '').replace(/\\s+/g, '');
  const candidates = [...document.querySelectorAll('button, [role="button"], a, div, span')].filter(e => e.children.length === 0 && norm(e.textContent) === '授权');
  const element = candidates.map(e => e.closest('button, [role="button"]') || e).find(e => {
    const r = e.getBoundingClientRect();
    return r.width > 0 && r.height > 0 && !e.disabled && e.getAttribute('aria-disabled') !== 'true';
  });
  let button = null;
  if (element) {
    element.scrollIntoView({ block: 'center' });
    const r = element.getBoundingClientRect();
    const x = r.left + r.width / 2, y = r.top + r.height / 2;
    const hit = document.elementFromPoint(x, y);
    // Only a button that really is under the pointer: a banner or dialog over it would take the click.
    if (hit && (element === hit || element.contains(hit))) button = { x, y };
  }
  return { href: location.href, text: document.body ? document.body.innerText : '', button };
})()"""


def account_names(name: str) -> list[str]:
    """
    The names a company account may be shown under: the whole name, the parts of "name（alias）", and the name before a suffix CLS adds to
    its own account names ("钟丰骏-实习生"): Feishu's consent page shows the Feishu name.
    """
    parts = [name, *[p.strip() for p in re.split(r"[（()）]", name)], re.sub(r"\s*[-－—][^-－—]*\Z", "", name).strip()]
    return list(dict.fromkeys(p for p in parts if len(p) >= 2))


def approve_consent(cdp: Cdp, session_id: str, options: ConsentOptions, before_click: Callable[[], None] = lambda: None) -> dict:
    probe = cdp.send("Runtime.evaluate", {"expression": CONSENT_PROBE, "returnByValue": True}, session_id)
    page = (probe.get("result") or {}).get("value")
    if not isinstance(page, dict) or not page.get("button"):
        return {"state": "not_ready"}  # not a consent page (it also shows the sign-in code), or not drawn yet
    # The page may have moved on since the targets were listed: look at where it is now.
    here = parse_url(page.get("href"))
    if here is None:
        return {"state": "not_ready"}
    if not (options.is_consent_page or _feishu_consent_page)(here):
        return {"state": "not_ready"}
    text = str(page.get("text") or "")
    if options.app and options.app not in text:
        return {"state": "refused", "reason": f"the Feishu consent page asks for another app than {options.app}: nothing was clicked; the user has to look at that request themselves"}
    if options.unnamed_pin:
        return {"state": "refused", "reason": "the bound CLS account has no name, so the account on the consent page cannot be checked: nothing was clicked; an administrator has to fix the binding"}
    if options.account and not any(name in text for name in account_names(options.account)):
        return {"state": "refused", "reason": f"the account on the Feishu consent page is not the bound \"{options.account}\": nothing was clicked; scan with the bound account"}
    x, y = page["button"]["x"], page["button"]["y"]
    before_click()  # from here on this login counts as approved once, whatever the click does
    try:
        cdp.send("Page.bringToFront", {}, session_id)
    except CdpError:
        pass
    cdp.send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y}, session_id)
    cdp.send("Input.dispatchMouseEvent", {"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1}, session_id)
    cdp.send("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1}, session_id)
    return {"state": "clicked"}


def who_am_i(origin: str, authorization: str, fetcher: Optional[Fetcher] = None) -> Optional[dict]:
    """Asks CLS who the token belongs to; None when it is not (or no longer) accepted."""
    result = check_token(origin, authorization, fetcher)
    return result.get("account")


def check_token(origin: str, authorization: str, fetcher: Optional[Fetcher] = None) -> dict:
    """Like who_am_i, but says whether the server turned the token down or simply could not be asked (network, proxy, 5xx, timeout)."""
    fetcher = fetcher or default_fetch
    try:
        response = fetcher(f"{origin}/api/userinfo", method="GET", headers={"Authorization": authorization}, timeout=10.0, redirect="error")
        if response.status >= 500 or response.status in (429, 408):
            return {"unreachable": True}
        if not response.ok:
            return {"rejected": True}
        try:
            body = response.json()
        except ValueError:
            return {"unreachable": True}
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(body, dict) or body.get("code") != 0 or isinstance(body.get("code"), bool) or not isinstance(data, dict) or data.get("status") != 1:
            return {"rejected": True}
        ident = data.get("id")
        if not isinstance(ident, int) or isinstance(ident, bool) or abs(ident) > 2 ** 53 - 1:
            return {"rejected": True}
        name = data.get("name")
        return {"account": {"id": ident, "name": "" if name is None else str(name)}}
    except Exception:
        return {"unreachable": True}


def _cls_trouble(origin: str) -> Optional[str]:
    """Why CLS cannot be asked anything right now (the network, a proxy, a gateway, an outage), or None when it answers, whatever it says."""
    try:
        response = default_fetch(f"{origin}/api/userinfo", method="GET", timeout=10.0, redirect="manual")
        return f"HTTP {response.status}" if response.status >= 500 or response.status in (429, 408) else None
    except FetchError as error:
        return "timed out" if error.timeout else "connection failed"


def _lower(headers: Any, name: str) -> Optional[str]:
    if not isinstance(headers, dict):
        return None
    for key, value in headers.items():
        if key.lower() == name:
            return value
    return None


def capture_authorization(profile_dir: str, wait_ms: int, origin: Optional[str] = None, exe: Optional[str] = None, env: Optional[Mapping[str, str]] = None,
                          consent: Optional[ConsentOptions] = None) -> dict:
    """
    Opens the CLS page in the profile's browser and returns the first Authorization the server accepts:
    {"state": "ok", "authorization", "account"} | closed | login_required | {"state": "unreachable", "reason"} | {"state": "consent_refused", "reason"}.
    A browser already running on the profile (a login window an earlier command left open) is joined rather than started again.
    """
    origin = origin or CLS_ORIGIN
    exe = exe or find_browser(env)
    if not exe:
        raise Blocked(NO_BROWSER)
    browser = open_browser(exe, profile_dir, env=env)
    try:
        result = _watch(browser, origin, wait_ms, consent)
    except BaseException:
        try:
            browser.close()
        except Exception:
            pass
        raise
    # A browser an earlier call left waiting for a scan stays open when this look found no login in it (it may be exactly the login in
    # progress). Anything else has done its job.
    keep_open = not browser.launched and result["state"] in ("login_required", "consent_refused", "unreachable")
    if keep_open:
        browser.cdp.close()
    else:
        browser.close()
    return result


def _watch(browser: BrowserHandle, origin: str, wait_ms: int, consent: Optional[ConsentOptions]) -> dict:
    cdp = browser.cdp
    tried: set[str] = set()
    #: Tokens CLS could not be asked about (the network, a gateway, an outage): while one is left, running out of time says nothing about the login.
    unsettled: set[str] = set()
    #: Why CLS's own page did not load in the browser, as the browser saw it: no lapsed login either.
    trouble: list[Optional[str]] = [None]
    urls: dict[str, str] = {}
    sessions: dict[str, str] = {}
    events: "queue.Queue[tuple]" = queue.Queue()
    good: list[Optional[dict]] = [None]

    def attach(target_id: str) -> str:
        """Attaches to a page once and turns its Network events on, so nothing navigates before it is listening."""
        known = sessions.get(target_id)
        if known:
            return known
        session_id = cdp.send("Target.attachToTarget", {"targetId": target_id, "flatten": True})["sessionId"]
        sessions[target_id] = session_id
        cdp.send("Network.enable", {}, session_id)
        # A hidden browser announces itself as headless; the site should see an ordinary one.
        user_agent = cdp.send("Browser.getVersion").get("userAgent")
        if user_agent:
            cdp.send("Network.setUserAgentOverride", {"userAgent": user_agent.replace("HeadlessChrome", "Chrome")}, session_id)
        return session_id

    def consider(url: Optional[str], authorization: Optional[str]) -> None:
        if not authorization or not isinstance(url, str) or not url.startswith(f"{origin}/api/") or authorization in tried:
            return
        tried.add(authorization)
        # One at a time, so the first token that is good wins and a stale one that the page sent before it noticed it was logged out is skipped.
        checked = check_token(origin, authorization)
        # Not the server's no: the next request with this token asks again.
        if "unreachable" in checked:
            unsettled.add(authorization)
            tried.discard(authorization)
            return
        unsettled.discard(authorization)
        if "account" in checked and good[0] is None:
            good[0] = {"authorization": authorization, "account": checked["account"]}

    def own_page(url: Any) -> bool:
        """The last CLS page the browser opened: one that failed to load (network, proxy, DNS) or that a gateway answered with 5xx."""
        return str(url or "").startswith(f"{origin}/")

    cdp.on("Target.targetCreated", lambda p, s: events.put(("created", p)))
    cdp.on("Network.requestWillBeSent", lambda p, s: events.put(("request", p)))
    cdp.on("Network.requestWillBeSentExtraInfo", lambda p, s: events.put(("extra", p)))
    cdp.on("Network.loadingFailed", lambda p, s: events.put(("failed", p)))
    cdp.on("Network.responseReceived", lambda p, s: events.put(("response", p)))
    cdp.on_close(lambda: events.put(("closed", None)))

    def handle(kind: str, params: Any) -> Optional[str]:
        if kind == "closed":
            return "closed"
        params = params or {}
        if kind == "created":
            info = params.get("targetInfo") or {}
            if info.get("type") == "page":
                try:
                    attach(info["targetId"])
                except (CdpError, KeyError):
                    pass
        elif kind == "request":
            request = params.get("request") or {}
            url = request.get("url")
            if isinstance(params.get("requestId"), str) and url:
                urls[params["requestId"]] = url
            consider(url, _lower(request.get("headers"), "authorization"))
        elif kind == "extra":
            consider(urls.get(params["requestId"]) if isinstance(params.get("requestId"), str) else None, _lower(params.get("headers"), "authorization"))
        elif kind == "failed":
            if (params.get("type") == "Document" and not params.get("canceled") and params.get("errorText") != "net::ERR_ABORTED"
                    and own_page(urls.get(str(params.get("requestId"))))):
                trouble[0] = str(params.get("errorText") or "the page did not open")
        elif kind == "response":
            response = params.get("response") or {}
            if params.get("type") == "Document" and own_page(response.get("url")):
                try:
                    status = float(response.get("status"))
                except (TypeError, ValueError):
                    status = float("nan")
                trouble[0] = f"HTTP {int(status)}" if status >= 500 else None
        return None

    # The consent page is looked for on a short interval for as long as the login is awaited. One login is approved once: after the first
    # click nothing else is pressed, even if another tab shows the same page.
    clicked: set[str] = set()
    refused: list[Optional[str]] = [None]

    def look_for_consent() -> None:
        if not consent or clicked:
            return
        try:
            infos = cdp.send("Target.getTargets").get("targetInfos") or []
            for target in infos:
                if target.get("type") != "page":
                    continue
                url = parse_url(target.get("url"))
                if url is None or not (consent.is_consent_page or _feishu_consent_page)(url):
                    continue
                result = approve_consent(cdp, attach(target["targetId"]), consent, lambda t=target["targetId"]: clicked.add(t))
                if result["state"] == "clicked":
                    break
                if result["state"] == "refused" and refused[0] is None:
                    refused[0] = result["reason"]
        except CdpError:
            pass  # the next look tries again

    deadline = time.monotonic() + wait_ms / 1000
    next_look = time.monotonic() + 0.7
    try:
        cdp.send("Target.setDiscoverTargets", {"discover": True})
        infos = cdp.send("Target.getTargets").get("targetInfos") or []
        pages = [t for t in infos if t.get("type") == "page"]
        for page in pages:
            try:
                attach(page["targetId"])
            except CdpError:
                pass
        opened = next((p for p in pages if str(p.get("url", "")).startswith(origin)), None)
        blank = next((p for p in pages if p.get("url") == "about:blank" or str(p.get("url", "")).startswith(("chrome://newtab", "edge://newtab"))), None)
        entry = f"{origin}{ENTRY}"
        try:
            if opened:
                cdp.send("Page.reload", {}, attach(opened["targetId"]))
            elif blank:
                answer = cdp.send("Page.navigate", {"url": entry}, attach(blank["targetId"]))
                if answer.get("errorText") and answer["errorText"] != "net::ERR_ABORTED":
                    trouble[0] = answer["errorText"]
            elif not any(re.search(r"sso|login|oauth|auth", str(p.get("url", "")), re.IGNORECASE) for p in pages):
                cdp.send("Target.createTarget", {"url": entry})
        except CdpError:
            pass
    except CdpError:
        if cdp.closed:
            return {"state": "closed"}
        raise

    while True:
        if good[0] is not None:
            return {"state": "ok", **good[0]}
        if refused[0] is not None:
            return {"state": "consent_refused", "reason": refused[0]}
        now = time.monotonic()
        if now >= deadline:
            break
        if consent and now >= next_look:
            look_for_consent()
            next_look = time.monotonic() + 0.7
            continue
        wait = max(0.0, min(deadline, next_look if consent else deadline) - now)
        try:
            kind, params = events.get(timeout=wait)
        except queue.Empty:
            continue
        if handle(kind, params) == "closed":
            return {"state": "closed"}
    # A token being asked about may be the good one: what came in until now is still looked at. Otherwise nothing logged in was seen, which
    # is a login to ask for only if CLS answered: when it could not be asked, nobody is to be sent to scan.
    while good[0] is None:
        try:
            kind, params = events.get_nowait()
        except queue.Empty:
            break
        if kind in ("request", "extra", "failed", "response"):
            handle(kind, params)
    if good[0] is not None:
        return {"state": "ok", **good[0]}
    why = "CLS did not answer the login check" if unsettled else trouble[0] if trouble[0] is not None else (None if tried else _cls_trouble(origin))
    return {"state": "unreachable", "reason": why} if why else {"state": "login_required"}


def _feishu_login_page(url: Any) -> bool:
    return bool(re.fullmatch(r"accounts\.(feishu\.cn|larksuite\.com)", url.hostname or "")) and "login" in url.path


def capture_login_qr(profile_dir: str, origin: Optional[str] = None, exe: Optional[str] = None, env: Optional[Mapping[str, str]] = None,
                     is_login_page: Optional[Callable[[Any], bool]] = None, wait_ms: Optional[int] = None, settle_ms: Optional[int] = None) -> dict:
    """
    Takes a picture of the sign-in page's QR code so that it can be sent to the person who has to scan it with their phone: {"state": "qr",
    "png"} or {"state": "not_login_page", "where"} (host and path only). This is the one place a screenshot of a sign-in is taken, and what it
    shows is the sign-in challenge itself: nothing of the page's content is read. The browser is hidden and stays running afterwards (it
    outlives this process), so that the scan can finish the login there; the next `cls session` joins it, takes the token, and closes it.
    """
    origin = origin or CLS_ORIGIN
    exe = exe or find_browser(env)
    if not exe:
        raise Blocked(NO_BROWSER)
    is_login_page = is_login_page or _feishu_login_page
    browser = open_browser(exe, profile_dir, detach=True, env=env)
    leave_running = True
    try:
        cdp = browser.cdp
        infos = cdp.send("Target.getTargets").get("targetInfos") or []
        target_id = next((t["targetId"] for t in infos if t.get("type") == "page"), None) or cdp.send("Target.createTarget", {"url": "about:blank"})["targetId"]
        session_id = cdp.send("Target.attachToTarget", {"targetId": target_id, "flatten": True})["sessionId"]
        cdp.send("Page.enable", {}, session_id)
        user_agent = cdp.send("Browser.getVersion").get("userAgent")
        if user_agent:
            cdp.send("Network.setUserAgentOverride", {"userAgent": user_agent.replace("HeadlessChrome", "Chrome")}, session_id)
        # A narrow, sharp page: the code fills most of the picture, which is read off a phone screen.
        cdp.send("Emulation.setDeviceMetricsOverride", {"width": 640, "height": 720, "deviceScaleFactor": 2, "mobile": False}, session_id)
        # Always start from the entry page, so that the code is a fresh one.
        cdp.send("Page.navigate", {"url": f"{origin}{ENTRY}"}, session_id)

        def where() -> Any:
            """Right after a navigation starts the page is being swapped and the question fails for a moment: that is "not there yet"."""
            try:
                history = cdp.send("Page.getNavigationHistory", {}, session_id)
                entries = history.get("entries") or []
                index = history.get("currentIndex")
                url = entries[index]["url"] if isinstance(index, int) and 0 <= index < len(entries) else ""
                return parse_url(url)
            except (CdpError, KeyError, TypeError):
                return None

        def shown(url: Any) -> str:
            if url is None:
                return ""
            host = url.hostname or ""
            if url.port:
                host += f":{url.port}"
            return f"{host}{url.path or '/'}"

        current = None
        deadline = time.monotonic() + (wait_ms if wait_ms is not None else 20_000) / 1000
        while time.monotonic() < deadline:
            current = where()
            if current is not None and is_login_page(current):
                break
            current = None
            time.sleep(0.25)
        if current is None:
            url = where()
            # A browser this call started and that is not on a sign-in page has nothing to wait for.
            if browser.launched:
                leave_running = False
            return {"state": "not_login_page", "where": shown(url)}
        time.sleep((settle_ms if settle_ms is not None else 2_500) / 1000)
        now = where()
        if now is None or not is_login_page(now):
            if browser.launched:
                leave_running = False
            return {"state": "not_login_page", "where": shown(now)}
        shot = cdp.send("Page.captureScreenshot", {"format": "png"}, session_id)
        return {"state": "qr", "png": base64.b64decode(shot["data"])}
    except BaseException:
        leave_running = False
        raise
    finally:
        if leave_running:
            browser.cdp.close()
        else:
            try:
                browser.close()
            except Exception:
                pass


def cls_profile_dir(env: Optional[Mapping[str, str]] = None) -> str:
    """
    The browser this user's CLS login lives in. It is the feishu skill's (under the user's own directory): CLS signs in through the company's
    Feishu single sign-on, and that browser holds the user's Feishu web session from the Feishu login, so the user scans once for both.
    """
    from .agent_feishu import browser_profile_dir

    return browser_profile_dir(env)


def cls_qr_file(env: Optional[Mapping[str, str]] = None) -> str:
    """Where the QR picture that is waiting to be scanned is recorded (see cls_guard.py)."""
    from .paths import resolve_data_paths

    return os.path.join(resolve_data_paths(env).cls, "login-qr.json")


def cls_account_file(env: Optional[Mapping[str, str]] = None) -> str:
    """Which CLS account this user's login is pinned to (id and name only; see cls_guard.py)."""
    from .paths import resolve_data_paths

    return os.path.join(resolve_data_paths(env).cls, "account.json")


def unreachable_reason(why: str) -> str:
    """What a command says when CLS could not be asked whether the login is good."""
    return f"CLS cannot be reached for now ({why}): the login has not lapsed, so ask for no scan; try again a little later"


def open_session(profile_dir: Optional[str] = None, wait_ms: Optional[int] = None, origin: Optional[str] = None, exe: Optional[str] = None,
                 env: Optional[Mapping[str, str]] = None, consent: Optional[ConsentOptions] = None) -> dict:
    """A session for the commands that talk to CLS: whoever is logged in on this user's profile, or a refusal that says to log in."""
    origin = origin or CLS_ORIGIN
    result = capture_authorization(profile_dir or cls_profile_dir(), wait_ms if wait_ms is not None else 25_000, origin, exe, env, consent)
    if result["state"] == "consent_refused":
        raise Blocked(result["reason"])
    if result["state"] == "unreachable":
        raise Blocked(unreachable_reason(result["reason"]))
    if result["state"] != "ok":
        raise Blocked("CLS is not logged in or the login has expired: run gof cls login --qr <file.png> and give the QR image to the user to scan with the Feishu mobile app")
    return {"authorization": result["authorization"], "account": result["account"], "origin": origin}
