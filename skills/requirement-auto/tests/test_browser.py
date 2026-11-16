"""The hidden browser for real: the CLS login, the QR code, the consent page, and the pictures (cls-session.test.ts, shots.test.ts,
creative-shot.test.ts). Skipped when no Chrome or Edge is installed, as the originals were."""

from __future__ import annotations

import base64
import json
import os
import socket
import subprocess
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Optional
from urllib.parse import parse_qs, urlsplit

from tests.fakes import GAME, ScriptedOnline
from tests.helpers import SCRIPTS, fixture, temp_dir, write_file

from gof.batch import prepare, record, report
from gof.chromium import BROWSER_ENV, attach_browser, browser_candidates, find_browser, open_browser
from gof.cli import main
from gof.cls_session import ConsentOptions, NO_BROWSER, account_names, capture_authorization, capture_login_qr, open_session, who_am_i
from gof.creative_shot import list_screenshot
from gof.doc_shot import doc_screenshot
from gof.draft_shot import draft_screenshot
from gof.plan import make_plan
from gof.sheet_shot import located_url, sheet_screenshot
from gof.shot_page import not_loaded
from gof.shots_online import sheet_picture
from gof.snapshot import Snapshot
from gof.util import Blocked, read_json

EXE = find_browser()
need_browser = unittest.skipUnless(EXE, "no Chrome or Edge on this machine")


class Site:
    """A small local web site: `handler(request, url, body)` answers or returns False for a 404."""

    def __init__(self, handler: Callable[["Request"], Optional[bool]]) -> None:
        self.seen: list[str] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: object) -> None:
                pass

            def _any(self) -> None:
                url = urlsplit(self.path)
                outer.seen.append(f"{url.path}{'?' + url.query if url.query else ''}")
                request = Request(self, url)
                if handler(request) is False:
                    request.send("text/plain", "not found", 404)

            do_GET = do_POST = _any

        class Server(ThreadingHTTPServer):
            daemon_threads = True

            def handle_error(self, request: object, client_address: object) -> None:
                pass  # a browser that closes a connection early is no failure of the site

        self.httpd = Server(("127.0.0.1", 0), Handler)
        self.origin = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


class Request:
    def __init__(self, handler: BaseHTTPRequestHandler, url) -> None:
        self.handler = handler
        self.url = url
        self.path = url.path
        self.query = parse_qs(url.query)
        self.headers = handler.headers

    def send(self, kind: str, body: str, status: int = 200, headers: Optional[dict] = None) -> None:
        data = body.encode("utf-8")
        self.handler.send_response(status)
        self.handler.send_header("Content-Type", f"{kind}; charset=utf-8" if "charset" not in kind else kind)
        for key, value in (headers or {}).items():
            self.handler.send_header(key, value)
        self.handler.send_header("Content-Length", str(len(data)))
        self.handler.end_headers()
        self.handler.wfile.write(data)


def closed_port() -> int:
    """A free port nobody listens on: a site that cannot be reached."""
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def fake_cls() -> tuple[Site, dict]:
    """
    A stand-in for CLS: a page whose script calls the API with the token kept in local storage, and an SSO page that stores a token. The
    first call every page makes carries a stale token, like a single-page app that has not noticed yet that its login ran out.
    """
    hits = {"sso": 0, "userinfo": []}

    def handle(r: Request) -> Optional[bool]:
        if r.path == "/api/userinfo":
            auth = r.headers.get("Authorization") or ""
            hits["userinfo"].append(auth)
            if auth == "Bearer good":
                r.send("application/json", json.dumps({"code": 0, "data": {"id": 7, "name": "测试用户", "status": 1}}))
            else:
                r.send("application/json", json.dumps({"code": 401, "msg": "unauthorized"}), 401)
            return None
        if r.path == "/sso":
            hits["sso"] += 1
            r.send("text/html", '<script>localStorage.setItem("token","Bearer good");location.replace("/requirements")</script>')
            return None
        if r.path == "/requirements":
            r.send("text/html", """<script>
              const token = localStorage.getItem("token");
              fetch("/api/userinfo", { headers: { Authorization: "Bearer stale" } })
                .then(() => token ? fetch("/api/userinfo", { headers: { Authorization: token } }) : location.replace("/sso"));
            </script>""")
            return None
        return False

    return Site(handle), hits


def fake_session_cls(scan: bool = False, consent: Optional[dict] = None) -> tuple[Site, dict]:
    """
    A CLS whose single sign-on leaves only a session cookie (no expiry), as the real one does; the page asks for its token with that cookie.
    With `scan`, signing in is not automatic: the page shows a sign-in page (a stand-in for the QR code) until `state["scanned"]` is set.
    """
    state = {"sso": 0, "scanned": False, "decisions": []}

    def handle(r: Request) -> Optional[bool]:
        signed_in = "sid=abc" in (r.headers.get("Cookie") or "")
        if r.path == "/api/token":
            r.send("application/json", json.dumps({"token": "Bearer good"}) if signed_in else "{}", 200 if signed_in else 401)
        elif r.path == "/api/userinfo":
            if r.headers.get("Authorization") == "Bearer good":
                r.send("application/json", json.dumps({"code": 0, "data": {"id": 7, "name": "测试用户", "status": 1}}))
            else:
                r.send("application/json", json.dumps({"code": 401}), 401)
        elif r.path == "/scan-status":
            r.send("application/json", json.dumps({"scanned": state["scanned"]}))
        elif r.path == "/login-page":
            target = "/consent" if consent else "/sso"
            r.send("text/html", '<body style="margin:0"><div style="width:300px;height:300px;margin:60px;background:#000"></div><script>setInterval(() => fetch("/scan-status")'
                                f'.then(r => r.json()).then(s => {{ if (s.scanned) location.replace("{target}"); }}), 200)</script></body>')
        elif r.path == "/consent":
            r.send("text/html", f"""<body><p>应用 {consent['app']} 请求以下账号进行授权</p><p>{consent['account']}</p>
              <button id="no" onclick="fetch('/decision?choice=no')">拒绝</button>
              <button id="yes" onclick="fetch('/decision?choice=yes').then(() => location.replace('/sso'))">授权</button></body>""")
        elif r.path == "/decision":
            state["decisions"].append(r.query.get("choice", [""])[0])
            r.send("application/json", "{}")
        elif r.path == "/sso":
            state["sso"] += 1
            r.send("text/html", '<script>location.replace("/requirements")</script>', headers={"Set-Cookie": "sid=abc; Path=/; HttpOnly"})
        elif r.path == "/requirements":
            fallback = "/login-page" if scan else "/sso"
            r.send("text/html", f"""<script>
              fetch("/api/token").then(r => r.ok ? r.json() : Promise.reject()).then(t => fetch("/api/userinfo", {{ headers: {{ Authorization: t.token }} }})).catch(() => location.replace("{fallback}"));
            </script>""")
        else:
            return False
        return None

    return Site(handle), state


def profile() -> str:
    return os.path.join(temp_dir("cls-profile-"), "browser-profile")


def is_login_page(url) -> bool:
    return url.path == "/login-page"


def is_consent_page(url) -> bool:
    return url.path == "/consent"


class BrowserBasics(unittest.TestCase):
    def test_found(self) -> None:
        """The browser is found from the environment or from the usual places, and nowhere else."""
        self.assertIsNone(find_browser({BROWSER_ENV: "relative/chrome.exe"}, "win32"))
        self.assertIsNone(find_browser({BROWSER_ENV: os.path.join(temp_dir("browser-"), "missing.exe")}, "win32"))
        self.assertTrue(any(p.endswith("msedge.exe") for p in browser_candidates("win32", {"ProgramFiles(x86)": "C:\\Program Files (x86)"})))
        self.assertIn(os.path.join("C:\\Program Files (x86)", "Microsoft", "Edge", "Application", "msedge.exe"), browser_candidates("win32", {}),
                      "a cut-down environment still finds Edge in its usual place")
        self.assertTrue(all(p.startswith("/") for p in browser_candidates("linux", {})))

    def test_token_judged(self) -> None:
        """A token is judged by what CLS says about it."""
        site, _ = fake_cls()
        try:
            self.assertEqual(who_am_i(site.origin, "Bearer good"), {"id": 7, "name": "测试用户"})
            self.assertIsNone(who_am_i(site.origin, "Bearer stale"))
            self.assertIsNone(who_am_i("http://127.0.0.1:9", "Bearer good"), "a server that is not there is not a login")
        finally:
            site.close()

    def test_names_and_variable(self) -> None:
        """The consent page may name the account without the suffix CLS gives its own name, and the browser is named by the shared variable."""
        self.assertIn("钟丰骏", account_names("钟丰骏-实习生"))
        self.assertIn("冯彦蓉", account_names("冯彦蓉－实习生"))
        self.assertIn("张三", account_names("张三 — 外包"))
        self.assertEqual(account_names("张三"), ["张三"])
        self.assertIn(BROWSER_ENV, NO_BROWSER)

    def test_no_browser_named(self) -> None:
        """A browser named that is not there is said, before anything is started (the Python client never sends loopback through a proxy)."""
        missing = os.path.join(temp_dir("cls-proxy-"), "no-such-browser.exe")
        self.assertRaisesRegex(Blocked, "浏览器没有启动成功", capture_authorization, os.path.join(temp_dir("cls-proxy-"), "p"), 1, exe=missing)


@need_browser
class LoginTests(unittest.TestCase):
    def test_page_requests(self) -> None:
        """The login is read from the page's own requests, a stale token is skipped, and the next run reuses the profile without logging in again."""
        site, hits = fake_cls()
        directory = profile()
        try:
            first = capture_authorization(directory, 30_000, site.origin, EXE)
            self.assertEqual(first["state"], "ok", first)
            self.assertEqual([first["authorization"], first["account"]], ["Bearer good", {"id": 7, "name": "测试用户"}])
            self.assertIn("Bearer stale", hits["userinfo"], "the page sent its stale token first, as the test intends")
            self.assertEqual(hits["sso"], 1)
            # A new browser process on the same profile: the login was written to the profile when the first one was closed.
            second = capture_authorization(directory, 30_000, site.origin, EXE)
            self.assertEqual(second["state"], "ok")
            self.assertEqual(hits["sso"], 1, "no second login: the stored one was enough")
        finally:
            site.close()

    def test_session_cookie(self) -> None:
        """A login that lives only in a session cookie survives the browser being closed and started again."""
        site, state = fake_session_cls()
        directory = profile()
        try:
            self.assertEqual(capture_authorization(directory, 30_000, site.origin, EXE)["state"], "ok")
            self.assertEqual(state["sso"], 1)
            self.assertEqual(capture_authorization(directory, 30_000, site.origin, EXE)["state"], "ok")
            self.assertEqual(state["sso"], 1, "the second run found the login the first one left in the profile")
        finally:
            site.close()

    def test_qr(self) -> None:
        """The sign-in QR code is photographed, the hidden browser stays for the scan, and the next command takes the login from it."""
        site, state = fake_session_cls(scan=True)
        directory = profile()
        try:
            qr = capture_login_qr(directory, site.origin, EXE, is_login_page=is_login_page, settle_ms=300)
            self.assertEqual(qr["state"], "qr")
            self.assertEqual(qr["png"][:4].hex(), "89504e47", "a PNG")
            self.assertGreater(len(qr["png"]), 2_000, "a picture of a page, not an empty file")
            waiting = attach_browser(directory)
            self.assertIsNotNone(waiting, "the browser that shows the code is still running for the scan")
            waiting.cdp.close()
            self.assertEqual(state["sso"], 0, "nobody has signed in yet")
            # Without a scan, looking again finds nobody logged in and leaves the browser where it is.
            self.assertEqual(capture_authorization(directory, 1_500, site.origin, EXE)["state"], "login_required")
            still = attach_browser(directory)
            self.assertIsNotNone(still, "a look that found nothing does not close the browser waiting for the scan")
            still.cdp.close()
            # The person scans with their phone; the next command picks the login up from the same browser and closes it.
            state["scanned"] = True
            self.assertEqual(capture_authorization(directory, 20_000, site.origin, EXE)["state"], "ok")
            self.assertEqual(state["sso"], 1)
            self.assertIsNone(attach_browser(directory), "the login is done, so the browser is gone")
            # And the login stays: the session cookie was kept when the browser was closed.
            self.assertEqual(capture_authorization(directory, 20_000, site.origin, EXE)["state"], "ok")
            self.assertEqual(state["sso"], 1)
        finally:
            site.close()

    def scanned(self, site: Site, state: dict, directory: str) -> None:
        """Photographs the code, then lets the scan happen, so that the browser is left on the consent page."""
        self.assertEqual(capture_login_qr(directory, site.origin, EXE, is_login_page=is_login_page, settle_ms=300)["state"], "qr")
        state["scanned"] = True

    def test_consent_approved(self) -> None:
        """After the scan the consent page of the CLS application is approved, and only the button that says 授权 is pressed."""
        site, state = fake_session_cls(scan=True, consent={"app": "dsc_admin", "account": "张三"})
        directory = profile()
        try:
            self.scanned(site, state, directory)
            result = capture_authorization(directory, 20_000, site.origin, EXE, consent=ConsentOptions(app="dsc_admin", account="张三", is_consent_page=is_consent_page))
            self.assertEqual(result["state"], "ok", result)
            self.assertEqual(state["decisions"], ["yes"], "approved once, and the other button was left alone")
        finally:
            site.close()

    def test_consent_first_login(self) -> None:
        """With no account pinned yet, the first login is approved for whoever the page names."""
        site, state = fake_session_cls(scan=True, consent={"app": "dsc_admin", "account": "李四"})
        directory = profile()
        try:
            self.scanned(site, state, directory)
            result = capture_authorization(directory, 20_000, site.origin, EXE, consent=ConsentOptions(app="dsc_admin", is_consent_page=is_consent_page))
            self.assertEqual(result["state"], "ok", result)
            self.assertEqual(state["decisions"], ["yes"])
        finally:
            site.close()

    def test_consent_other_app(self) -> None:
        """A consent page for another application is not approved, and the browser is left as it is."""
        site, state = fake_session_cls(scan=True, consent={"app": "some_other_app", "account": "张三"})
        directory = profile()
        try:
            self.scanned(site, state, directory)
            result = capture_authorization(directory, 20_000, site.origin, EXE, consent=ConsentOptions(app="dsc_admin", account="张三", is_consent_page=is_consent_page))
            self.assertEqual(result["state"], "consent_refused")
            self.assertRegex(result["reason"], "dsc_admin")
            self.assertEqual(state["decisions"], [], "nothing was pressed")
            still = attach_browser(directory)
            self.assertIsNotNone(still, "the page stays where the person can be told about it")
            still.close()
        finally:
            site.close()

    def test_consent_other_account(self) -> None:
        """A consent page for another account than the pinned one is not approved."""
        site, state = fake_session_cls(scan=True, consent={"app": "dsc_admin", "account": "李四"})
        directory = profile()
        try:
            self.scanned(site, state, directory)
            result = capture_authorization(directory, 20_000, site.origin, EXE, consent=ConsentOptions(app="dsc_admin", account="张三", is_consent_page=is_consent_page))
            self.assertEqual(result["state"], "consent_refused")
            self.assertRegex(result["reason"], "张三")
            self.assertEqual(state["decisions"], [])
            browser = attach_browser(directory)
            if browser:
                browser.close()
        finally:
            site.close()

    def test_no_consent_option(self) -> None:
        """Without the consent option nothing is ever pressed."""
        site, state = fake_session_cls(scan=True, consent={"app": "dsc_admin", "account": "张三"})
        directory = profile()
        try:
            self.scanned(site, state, directory)
            self.assertEqual(capture_authorization(directory, 4_000, site.origin, EXE)["state"], "login_required")
            self.assertEqual(state["decisions"], [])
            browser = attach_browser(directory)
            if browser:
                browser.close()
        finally:
            site.close()

    def test_consent_feishu_name(self) -> None:
        """A consent page naming the Feishu name of a pinned "-实习生" account is approved."""
        site, state = fake_session_cls(scan=True, consent={"app": "dsc_admin", "account": "钟丰骏"})
        directory = profile()
        try:
            self.scanned(site, state, directory)
            result = capture_authorization(directory, 20_000, site.origin, EXE, consent=ConsentOptions(app="dsc_admin", account="钟丰骏-实习生", is_consent_page=is_consent_page))
            self.assertEqual(result["state"], "ok", result)
            self.assertEqual(state["decisions"], ["yes"])
        finally:
            site.close()

    def test_not_login_page(self) -> None:
        """A page that is not a sign-in page is reported as that, not photographed."""
        site, _ = fake_session_cls()
        directory = profile()
        try:
            # This server signs in by itself, so the page never stops on a sign-in page.
            result = capture_login_qr(directory, site.origin, EXE, is_login_page=is_login_page, wait_ms=4_000, settle_ms=100)
            self.assertEqual(result["state"], "not_login_page")
            self.assertIsNone(attach_browser(directory), "a browser started for nothing is not left running")
        finally:
            site.close()

    def test_tabs_do_not_pile_up(self) -> None:
        """Starting the browser again and again on one profile does not pile up tabs."""
        site, _ = fake_cls()
        directory = profile()
        counts = []
        try:
            for _ in range(4):
                browser = open_browser(EXE, directory)
                try:
                    time.sleep(0.5)
                    infos = browser.cdp.send("Target.getTargets")["targetInfos"]
                    counts.append(len([t for t in infos if t["type"] == "page"]))
                    if not any(t["type"] == "page" and t["url"].startswith(site.origin) for t in infos):
                        browser.cdp.send("Target.createTarget", {"url": f"{site.origin}/requirements"})
                    time.sleep(0.5)
                finally:
                    browser.close()
            self.assertLessEqual(counts[3], counts[1], f"the number of tabs at each start stays put: {counts}")
            self.assertLessEqual(max(counts), 2, f"a restored page and one blank tab at most: {counts}")
        finally:
            site.close()

    def test_nothing_logged_in(self) -> None:
        """Nothing logged in in a hidden browser is reported as a login that is needed, not as a failure."""
        site, _ = fake_cls()
        try:
            # A server that never accepts any token.
            self.assertEqual(capture_authorization(os.path.join(temp_dir("cls-profile-"), "p"), 4_000, f"{site.origin}/nothing-here", EXE)["state"], "login_required")
        finally:
            site.close()

    def test_joined(self) -> None:
        """A browser left running on the profile is joined by the next command instead of a second one being started."""
        directory = os.path.join(temp_dir("cls-profile-"), "p")
        first = open_browser(EXE, directory)
        try:
            self.assertTrue(first.launched)
            second = open_browser(EXE, directory)
            self.assertFalse(second.launched)
            self.assertRegex(second.cdp.send("Browser.getVersion")["product"], "Chrome|Edg")
            second.cdp.close()
        finally:
            first.close()
        self.assertTrue(first.cdp.closed)

    def leave_browser(self, directory: str, mode: str) -> tuple[Optional[int], str]:
        """Starts a browser from a process of its own, lets go of it unclosed, and reports whether that process ended by itself."""
        code = ("import sys; sys.path.insert(0, sys.argv[1]); from gof.chromium import open_browser; "
                "b = open_browser(sys.argv[2], sys.argv[3], detach=sys.argv[4] == 'detach'); b.cdp.close(); print('left')")
        try:
            done = subprocess.run([sys.executable, "-c", code, SCRIPTS, EXE, directory, mode], capture_output=True, text=True, timeout=20)
            return done.returncode, done.stdout
        except subprocess.TimeoutExpired as error:
            return None, (error.stdout or b"").decode() if isinstance(error.stdout, bytes) else (error.stdout or "")

    def test_detached_outlives(self) -> None:
        """A command that leaves a browser waiting for a scan ends at once, and the browser is still there for the next command to join."""
        directory = os.path.join(temp_dir("cls-profile-"), "p")
        code, out = self.leave_browser(directory, "detach")
        still = attach_browser(directory)
        try:
            self.assertEqual([code, out.strip()], [0, "left"], "the process ended on its own instead of waiting for the window to close")
            self.assertIsNotNone(still, "the browser it started outlived it")
        finally:
            if still:
                still.close()

    def test_hidden_ends_with_command(self) -> None:
        """A hidden browser does not outlive the command that started it, so nothing is left holding the profile."""
        directory = os.path.join(temp_dir("cls-profile-"), "p")
        code, out = self.leave_browser(directory, "attach")
        self.assertEqual([code, out.strip()], [0, "left"])
        time.sleep(1)
        self.assertIsNone(attach_browser(directory))

    def test_out_of_reach(self) -> None:
        """CLS out of reach is no lapsed login: neither a token CLS cannot be asked about nor a page that does not load sends anybody to scan."""
        # The page is logged in, but the token check from here gets a gateway's 503 (an outage, or a proxy this process does not go through).
        def handle(r: Request) -> Optional[bool]:
            if r.path == "/api/userinfo":
                if "Chrome" not in (r.headers.get("User-Agent") or "") and "Edg" not in (r.headers.get("User-Agent") or ""):
                    r.send("text/plain", "upstream unavailable", 503)
                else:
                    r.send("application/json", json.dumps({"code": 0, "data": {"id": 7, "name": "张三", "status": 1}}))
                return None
            if r.path == "/requirements":
                r.send("text/html", '<script>fetch("/api/userinfo", { headers: { Authorization: "Bearer good" } })</script>')
                return None
            return False

        site = Site(handle)
        try:
            seen = capture_authorization(os.path.join(temp_dir("cls-profile-"), "p"), 4_000, site.origin, EXE)
            self.assertEqual(seen["state"], "unreachable", seen)
            with self.assertRaises(Blocked) as caught:
                open_session(os.path.join(temp_dir("cls-profile-"), "p"), 4_000, site.origin, EXE)
            message = caught.exception.message
            self.assertTrue("暂时无法访问" in message and "不要让用户扫码" in message and "cls login" not in message)
        finally:
            site.close()
        # CLS's page itself does not load.
        down = capture_authorization(os.path.join(temp_dir("cls-profile-"), "p"), 3_000, f"http://127.0.0.1:{closed_port()}", EXE)
        self.assertEqual(down["state"], "unreachable", down)
        self.assertRegex(down["reason"], "ERR_CONNECTION_REFUSED|连接失败")


def is_png(test: unittest.TestCase, file: str) -> None:
    with open(file, "rb") as handle:
        test.assertEqual(handle.read(4), b"\x89PNG", "a PNG")


def fake_sheet(signed_in: bool = True) -> Site:
    """The progress sheet as far as the picture needs it: a page with a drawing surface; `signed_in=False` sends to the sign-in page."""
    def handle(r: Request) -> Optional[bool]:
        if r.path.startswith("/wiki/"):
            if signed_in:
                r.send("text/html", '<!doctype html><body><canvas width="600" height="300" style="background:#cde"></canvas></body>')
            else:
                r.send("text/html", '<script>location.replace("/accounts/page/login")</script>')
            return None
        if r.path == "/accounts/page/login":
            r.send("text/html", "<p>扫码登录</p>")
            return None
        return False

    return Site(handle)


def range_asked(site: Site) -> str:
    asked = next(s for s in site.seen if s.startswith("/wiki/"))
    return base64.b64decode(parse_qs(urlsplit(asked).query)["range"][0]).decode("utf-8")


def fake_app(lands_at: Optional[str] = None) -> Site:
    """The CLS app as far as the draft picture needs it: it asks who is logged in, keeps its game, and its router draws the draft page (or lands elsewhere)."""
    page = """<!doctype html><body><div id="app"></div><script>
      fetch('/api/userinfo', { headers: { Authorization: 'Bearer good' } }).then(() => {
        window.DD_VUE_INSTANCE = { $router: { push: async (to) => {
          const game = JSON.parse(localStorage.getItem('DD-CLS-GLOBAL-CURGAME') || '{}');
          const landed = LANDS || to;
          history.pushState({}, '', landed);
          if (landed !== to) { document.getElementById('app').innerText = '无权限'; return; }
          const r = await fetch('/api/workflow/script/5001?game_id=' + game.id);
          const { data } = await r.json();
          document.getElementById('app').innerHTML = '<h1>' + data.script.name + '</h1><p>脚本 ' + data.script.sub_scripts.length + ' 条</p>';
        } } };
      });
    </script></body>""".replace("LANDS", json.dumps(lands_at))

    def handle(r: Request) -> Optional[bool]:
        if r.path == "/requirements":
            r.send("text/html", page)
        elif r.path == "/api/userinfo":
            r.send("application/json", json.dumps({"code": 0, "data": {"id": 1565, "status": 1}}))
        elif r.path == "/api/workflow/script/5001":
            r.send("application/json", json.dumps({"code": 0, "data": {"script": {"id": 5001, "name": "GOFCN66001-GOFCN66002", "sub_scripts": [{}, {}]}}}))
        else:
            return False
        return None

    return Site(handle)


def fake_list_app(logged_in: bool = True) -> Site:
    """The CLS app as far as the list picture needs it; `logged_in=False` stops on a sign-in page."""
    page = """<!doctype html><body><div id="app"></div><script>
      fetch('/api/userinfo', { headers: { Authorization: 'Bearer good' } }).then(() => {
        window.DD_VUE_INSTANCE = { $router: { push: async () => {
          history.pushState({}, '', '/creativities');
          const game = JSON.parse(localStorage.getItem('DD-CLS-GLOBAL-CURGAME') || '{}');
          const filter = JSON.parse(localStorage.getItem('DD_CLS_CREATIVES_CONFIRMED_SEARCH_CONDITIONS') || '{}');
          const r = await fetch('/api/creatives?game_id=' + game.id + (filter.upload_users || []).map(u => '&upload_users[]=' + u).join(''));
          const { data } = await r.json();
          document.getElementById('app').innerHTML = '<div class="el-table__body-wrapper"><table><tbody>'
            + data.creatives.map(c => '<tr><td><img src="data:image/gif;base64,R0lGODlhAQABAAAAACw="></td><td>' + c.name + '</td></tr>').join('')
            + '</tbody></table></div><span class="el-pagination__total">共 ' + data.total + ' 条</span>';
        } } };
      });
    </script></body>"""

    def handle(r: Request) -> Optional[bool]:
        if r.path == "/requirements":
            r.send("text/html", page if logged_in else '<script>location.replace("/feishu-login")</script>')
        elif r.path == "/feishu-login":
            r.send("text/html", "<p>扫码登录</p>")
        elif r.path == "/api/userinfo":
            r.send("application/json", json.dumps({"code": 0, "data": {"id": 1565, "status": 1}}))
        elif r.path == "/api/creatives":
            r.send("application/json", json.dumps({"code": 0, "data": {"total": 2, "creatives": [{"id": 2, "name": "GOFCN66367_片头"}, {"id": 1, "name": "封面"}]}}))
        else:
            return False
        return None

    return Site(handle)


ONLINE = ScriptedOnline(session={"authorization": "Bearer good", "account": {"id": 7, "name": "张三"}, "origin": "http://127.0.0.1:1"})


def new_batch() -> str:
    base = Snapshot(fixture("base.xlsx"))
    return prepare(make_plan(base, read_json(os.path.join(fixture("requests"), "basic.json"))), base, output_root=temp_dir("shots-batch-"))["batch"]


class ShotChecks(unittest.TestCase):
    def test_located_url(self) -> None:
        """The sheet link names the appended rows the way Feishu's own range link does."""
        parsed = urlsplit(located_url("https://centurygames.feishu.cn/wiki/NODE?sheet=abc123", "A120:AB131"))
        query = parse_qs(parsed.query)
        self.assertEqual(query["sheet"], ["abc123"])
        self.assertEqual(base64.b64decode(query["range"][0]).decode(), "A120:AB131")
        self.assertEqual(located_url("https://centurygames.feishu.cn/wiki/NODE?sheet=abc123", None), "https://centurygames.feishu.cn/wiki/NODE?sheet=abc123")

    def test_batch_decides(self) -> None:
        """The batch decides what is photographed: no picture before the rows are appended, none of a draft that does not exist."""
        directory = new_batch()
        output = os.path.join(directory, "progress.png")
        early = main(["feishu", "screenshot", "--batch", directory, "--output", output], None, ONLINE)
        self.assertEqual(early["code"], 2)
        self.assertRegex(early["result"]["reason"], "还没有追加并验收")
        no_draft = main(["cls", "screenshot", "--batch", directory, "--output", os.path.join(directory, "draft.png")], None, ONLINE)
        self.assertEqual(no_draft["code"], 2)
        self.assertRegex(no_draft["result"]["reason"], "还没有保存任何 CLS 草稿")
        self.assertFalse(os.path.exists(output))
        self.assertRegex(not_loaded("进度表", "net::ERR_CONNECTION_REFUSED"), "^进度表打不开（net::ERR_CONNECTION_REFUSED）：.*不是登录失效")


@need_browser
class ShotTests(unittest.TestCase):
    def test_sheet_picture(self) -> None:
        """The progress sheet picture opens the sheet on the appended rows and never overwrites a file."""
        site = fake_sheet()
        try:
            output = os.path.join(temp_dir("sheet-shot-"), "progress.png")
            url = f"{site.origin}/wiki/NODE?sheet=abc123"
            result = sheet_screenshot(temp_dir("sheet-profile-"), output, url, "A120:AB131", EXE, wait_ms=15_000)
            self.assertEqual(result["state"], "ok", result)
            is_png(self, output)
            self.assertEqual(range_asked(site), "A120:AB131")
            self.assertRaisesRegex(Blocked, "不覆盖", sheet_screenshot, temp_dir("sheet-profile-"), output, url, None, EXE)
        finally:
            site.close()

    def test_sheet_signed_out(self) -> None:
        """Without a Feishu web login the sheet picture is not taken and the sign-in page is reported."""
        site = fake_sheet(False)
        try:
            output = os.path.join(temp_dir("sheet-shot-"), "progress.png")
            result = sheet_screenshot(temp_dir("sheet-profile-"), output, f"{site.origin}/wiki/NODE?sheet=abc123", None, EXE, wait_ms=8_000)
            self.assertEqual(result["state"], "login_required", result)
            self.assertFalse(os.path.exists(output))
        finally:
            site.close()

    def test_draft_picture(self) -> None:
        """The draft picture opens the draft in the app for GOFCN and never overwrites a file."""
        site = fake_app()
        try:
            output = os.path.join(temp_dir("draft-shot-"), "draft.png")
            result = draft_screenshot(temp_dir("draft-profile-"), output, 5001, GAME, site.origin, EXE, wait_ms=15_000)
            self.assertEqual(result["state"], "ok", result)
            is_png(self, output)
            self.assertIn("/api/workflow/script/5001?game_id=94", site.seen, f"the draft was asked for in GOFCN: {site.seen}")
            self.assertRaisesRegex(Blocked, "不覆盖", draft_screenshot, temp_dir("draft-profile-"), output, 5001, GAME, site.origin, EXE)
        finally:
            site.close()

    def test_not_loaded(self) -> None:
        """A page that does not load is told apart from a sign-in page: no picture, and nothing said about the login."""
        dead = f"http://127.0.0.1:{closed_port()}"
        output = os.path.join(temp_dir("dead-shot-"), "x.png")
        sheet = sheet_screenshot(temp_dir("sheet-profile-"), output, f"{dead}/wiki/NODE?sheet=abc", None, EXE, wait_ms=8_000)
        self.assertEqual(sheet["state"], "unreachable", sheet)
        self.assertRegex(sheet["reason"], "ERR_CONNECTION_REFUSED")
        doc = doc_screenshot(temp_dir("doc-profile-"), output, f"{dead}/docx/ABC123", None, EXE, wait_ms=8_000)
        self.assertEqual(doc["state"], "unreachable", doc)
        draft = draft_screenshot(temp_dir("draft-profile-"), output, 5001, GAME, dead, EXE, wait_ms=8_000)
        self.assertEqual(draft["state"], "unreachable", draft)
        self.assertFalse(os.path.exists(output))

    def test_draft_turned_away(self) -> None:
        """A draft page the app turns away is reported and no picture is written."""
        site = fake_app("/no-permission")
        try:
            output = os.path.join(temp_dir("draft-shot-"), "draft.png")
            result = draft_screenshot(temp_dir("draft-profile-"), output, 5001, GAME, site.origin, EXE, wait_ms=10_000)
            self.assertEqual(result["state"], "no_access", result)
            self.assertFalse(os.path.exists(output))
        finally:
            site.close()

    def test_after_append(self) -> None:
        """After the append the sheet picture is taken on the batch's rows."""
        directory = new_batch()
        record(directory, "append_started", None)
        record(directory, "append_verified", None, {}, Snapshot(fixture("applied-complete.xlsx")))
        site = fake_sheet()
        try:
            output = os.path.join(temp_dir("sheet-shot-"), "progress.png")
            rng = report(directory)["range"]
            result = sheet_picture(ONLINE, directory, output, {"sheet": {"url": f"{site.origin}/wiki/NODE?sheet=abc", "profile_dir": temp_dir("sheet-profile-"), "exe": EXE, "wait_ms": 15_000}})
            self.assertEqual(result["status"], "screenshot_saved")
            self.assertEqual(result["range"], rng)
            is_png(self, output)
            self.assertEqual(range_asked(site), rng)
        finally:
            site.close()

    def test_list_picture(self) -> None:
        """The list picture shows GOFCN and this account's uploads, newest first, and never overwrites a file."""
        site = fake_list_app()
        try:
            output = os.path.join(temp_dir("shot-"), "creatives.png")
            result = list_screenshot(temp_dir("shot-profile-"), output, 1565, GAME, rows=3, origin=site.origin, exe=EXE, wait_ms=15_000)
            self.assertEqual(result["state"], "ok", result)
            self.assertEqual([result["rows"], result["total"]], [2, "共 2 条"])
            is_png(self, output)
            self.assertIn("/api/creatives?game_id=94&upload_users[]=1565", site.seen, f"the list asked for GOFCN and this account: {site.seen}")
            self.assertRaisesRegex(Blocked, "不覆盖", list_screenshot, temp_dir("shot-profile-"), output, 1565, GAME, origin=site.origin, exe=EXE)
        finally:
            site.close()

    def test_list_not_loaded(self) -> None:
        """A list that does not load is reported as that, not as a lapsed login."""
        output = os.path.join(temp_dir("shot-"), "creatives.png")
        result = list_screenshot(temp_dir("shot-profile-"), output, 1565, GAME, origin=f"http://127.0.0.1:{closed_port()}", exe=EXE, wait_ms=5_000)
        self.assertEqual(result["state"], "unreachable", result)
        self.assertFalse(os.path.exists(output))

    def test_list_signed_out(self) -> None:
        """Without a login the picture is not taken and the sign-in page is reported."""
        site = fake_list_app(False)
        try:
            output = os.path.join(temp_dir("shot-"), "creatives.png")
            result = list_screenshot(temp_dir("shot-profile-"), output, 1565, GAME, origin=site.origin, exe=EXE, wait_ms=3_000)
            self.assertEqual(result["state"], "login_required")
            self.assertFalse(os.path.exists(output))
        finally:
            site.close()


if __name__ == "__main__":
    unittest.main()
