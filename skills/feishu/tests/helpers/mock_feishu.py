"""A local stand-in for the Feishu endpoints the skill talks to. Records every request it receives."""

from __future__ import annotations

import base64
import json
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, parse_qsl, urlsplit

from feishu_sdk.config import Endpoints


@dataclass
class Seen:
    method: str
    url: str
    headers: dict[str, str]
    body: str


class Request:
    """What an override sees of a request, and how it answers."""

    def __init__(self, handler: BaseHTTPRequestHandler, body: str) -> None:
        self.handler = handler
        self.method = handler.command
        self.url = handler.path
        self.body = body

    def json(self, status: int, value: Any) -> None:
        data = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.handler.send_response(status)
        self.handler.send_header("Content-Type", "application/json")
        self.handler.send_header("Content-Length", str(len(data)))
        self.handler.end_headers()
        self.handler.wfile.write(data)

    def drop(self) -> None:
        """No answer at all: the connection is cut."""
        self.handler.close_connection = True
        try:
            self.handler.connection.shutdown(2)
        except OSError:
            pass


class MockFeishu:
    def __init__(self) -> None:
        self.server: ThreadingHTTPServer | None = None
        self.url = ""
        self.seen: list[Seen] = []
        self.open_id = "ou_alice"
        self.name = "李小明"
        self.app_id = "cli_test"
        self.app_secret = "secret-test-123456"
        #: What the token endpoint answers to successive device-code polls; pending when the script is empty.
        self.device_script: list[Any] = []
        self.device_interval = 1
        self.scope = "wiki:wiki:readonly sheets:spreadsheet offline_access"
        self.with_refresh_token = True
        self.refresh_count = 0
        self.refresh_delay_ms = 0
        self.access_ttl = 7200
        self.revision = 7
        self.write_reply: tuple[int, Any] | None = None
        self.export_job: list[dict[str, Any]] = [{"job_status": 1}, {"job_status": 0, "file_token": "ft1"}]
        self.reject_token_once = False
        #: How many Open API calls in a row are turned down for too many requests.
        self.rate_limited = 0
        #: More Open API answers for a test: the `data` for a path, or None to fall through to the routes below.
        self.more: Callable[[str, dict[str, str]], Any] = lambda pathname, query: None
        #: Takes over a request before anything else when it returns True.
        self.override: Callable[[Request], bool] | None = None
        self._counter = 0
        self._access = ""
        self._refresh = ""
        self._lock = threading.Lock()

    def start(self) -> "MockFeishu":
        mock = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:
                pass

            def _any(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length).decode("utf-8") if length else ""
                mock._handle(Request(self, body))

            do_GET = do_POST = do_PATCH = do_PUT = do_DELETE = _any

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def stop(self) -> None:
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.server = None

    def revoke_access(self) -> None:
        """The server forgets the access token it issued; the refresh token still works, so a renewal succeeds."""
        self._access = "revoked-access-token"

    def revoke_all(self) -> None:
        """The server forgets both tokens, as when the user withdraws the authorization."""
        self._access = "revoked-access-token"
        self._refresh = "revoked-refresh-token"

    @property
    def endpoints(self) -> Endpoints:
        return Endpoints(open=self.url, accounts=self.url)

    @property
    def current_access(self) -> str:
        return self._access

    @property
    def current_refresh(self) -> str:
        return self._refresh

    def requests(self, prefix: str) -> list[Seen]:
        return [item for item in list(self.seen) if item.url.startswith(prefix)]

    def issue(self) -> dict[str, Any]:
        """Issues a fresh access/refresh pair, as the token endpoint does."""
        with self._lock:
            self._counter += 1
            self._access = f"u-access{self._counter}{'x' * 24}"
            body: dict[str, Any] = {"access_token": self._access, "token_type": "Bearer", "expires_in": self.access_ttl, "scope": self.scope}
            if self.with_refresh_token:
                self._refresh = f"ur-refresh{self._counter}{'y' * 24}"
                body["refresh_token"] = self._refresh
                body["refresh_token_expires_in"] = 604800
            return body

    def _handle(self, req: Request) -> None:
        h = req.handler
        self.seen.append(Seen(req.method, req.url, {k.lower(): v for k, v in h.headers.items()}, req.body))
        if self.override and self.override(req):
            return
        pathname = req.url.split("?")[0]
        query = dict(parse_qsl(urlsplit(req.url).query, keep_blank_values=True))

        if pathname == "/oauth/v1/device_authorization":
            basic = "Basic " + base64.b64encode(f"{self.app_id}:{self.app_secret}".encode()).decode()
            if h.headers.get("Authorization") != basic:
                return req.json(401, {"error": "invalid_client", "error_description": "bad credentials"})
            return req.json(200, {"device_code": "dev-code-1", "user_code": "ABCD-1234", "verification_uri": f"{self.url}/verify",
                                  "verification_uri_complete": f"{self.url}/verify?user_code=ABCD-1234", "expires_in": 240,
                                  "interval": self.device_interval})
        if pathname == "/oauth/v3/token":
            if "json" in (h.headers.get("Content-Type") or ""):
                grant = json.loads(req.body)
            else:
                grant = {k: v[0] for k, v in parse_qs(req.body).items()}
            if grant.get("client_id") != self.app_id or grant.get("client_secret") != self.app_secret:
                return req.json(401, {"error": "invalid_client"})
            if grant.get("grant_type") == "urn:ietf:params:oauth:grant-type:device_code":
                nxt = self.device_script.pop(0) if self.device_script else None
                if nxt is None:
                    return req.json(400, {"error": "authorization_pending"})
                if nxt == "issue":
                    return req.json(200, self.issue())
                return req.json(nxt[0], nxt[1])
            if grant.get("grant_type") == "refresh_token":
                with self._lock:
                    self.refresh_count += 1
                if self.refresh_delay_ms:
                    time.sleep(self.refresh_delay_ms / 1000)
                if grant.get("refresh_token") != self._refresh:
                    return req.json(400, {"error": "invalid_grant", "error_description": "refresh token used"})
                return req.json(200, self.issue())
            return req.json(400, {"error": "unsupported_grant_type"})

        # Everything below is an Open API call with a user access token.
        if h.headers.get("Authorization") != f"Bearer {self._access}":
            return req.json(401, {"code": 99991668, "msg": "invalid access token"})
        if self.reject_token_once:
            self.reject_token_once = False
            return req.json(200, {"code": 99991677, "msg": "token expired"})
        if self.rate_limited > 0:
            self.rate_limited -= 1
            return req.json(429, {"code": 99991400, "msg": "request trigger frequency limit"})
        if pathname == "/open-apis/authen/v1/user_info":
            return req.json(200, {"code": 0, "data": {"open_id": self.open_id, "name": self.name}})
        if pathname.startswith("/open-apis/sheets/v2/spreadsheets/") and pathname.endswith("/metainfo"):
            return req.json(200, {"code": 0, "data": {"properties": {"revision": self.revision}}})
        if pathname.endswith("/tools/invoke_write"):
            if self.write_reply:
                return req.json(self.write_reply[0], self.write_reply[1])
            return req.json(200, {"code": 0, "data": {"ok": True}})
        if pathname == "/open-apis/drive/v1/export_tasks" and req.method == "POST":
            return req.json(200, {"code": 0, "data": {"ticket": "tk1"}})
        if pathname == "/open-apis/drive/v1/export_tasks/tk1":
            job = self.export_job.pop(0) if len(self.export_job) > 1 else self.export_job[0]
            return req.json(200, {"code": 0, "data": {"result": job}})
        if pathname == "/open-apis/drive/v1/export_tasks/file/ft1/download":
            # The smallest valid ZIP: an empty central directory end record.
            data = bytes([0x50, 0x4B, 0x05, 0x06]) + bytes(18)
            h.send_response(200)
            h.send_header("Content-Type", "application/octet-stream")
            h.send_header("Content-Length", str(len(data)))
            h.end_headers()
            h.wfile.write(data)
            return None
        if pathname.startswith("/open-apis/docx/v1/documents/doxNOACCESS1"):
            return req.json(403, {"code": 1770032, "msg": "forBidden"})
        extra = self.more(pathname, query)
        if extra is not None:
            return req.json(200, {"code": 0, "data": extra})
        if pathname == "/open-apis/wiki/v2/spaces/get_node":
            return req.json(200, {"code": 0, "data": {"node": {"node_token": "wiki1", "obj_type": "sheet", "obj_token": "shtABCDEFGH"}}})
        return req.json(404, {"code": 404, "msg": f"no route {pathname}"})
