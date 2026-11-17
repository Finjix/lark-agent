"""Stand-ins for CLS and for the object storage its upload links point at."""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, quote, unquote, urlsplit

from gof.cls import parse_csv


class _Server:
    def __init__(self, handle: Callable[[BaseHTTPRequestHandler, str, bytes], None]) -> None:
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:
                pass

            def _any(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                handle(self, self.command, body)

            do_GET = do_POST = do_PUT = do_DELETE = _any

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.origin = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def _send(handler: BaseHTTPRequestHandler, data: Any, status: int = 200, code: int = 0, message: Optional[str] = None) -> None:
    payload: dict[str, Any] = {"code": code, "data": data}
    if message is not None:
        payload = {"code": code, "type": "message", "message": message, "data": data}
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _hang_up(handler: BaseHTTPRequestHandler) -> None:
    """Hangs up without an answer: an outcome the client cannot know."""
    handler.close_connection = True
    try:
        handler.connection.shutdown(2)
    except OSError:
        pass


class FakeCls:
    """
    A stand-in for the CLS API with the shapes the restricted adapter reads: the option lists, drafts and CSV imports. It answers only to
    `Bearer good`, and it keeps every request so a test can see what was sent.
    """

    def __init__(self, catalog: dict) -> None:
        self.requests: list[dict] = []
        self.drafts: dict[int, dict] = {}
        #: What the next POST does: answer normally, or hang up without an answer.
        self.post_mode = "normal"
        self._next_id = 5001
        self._next_script = 9001
        account = {"id": catalog["account"]["id"], "name": catalog["account"]["name"], "status": 1, "game_ids": catalog["account"]["game_ids"],
                   "roles": catalog["account"]["roles"]}
        permissions = {catalog["account"]["roles"][0]: [*(["workflow.script.save"] if catalog["can_create"] else []), *(["workflow.script.edit"] if catalog["can_import"] else [])]}
        reads = {
            "/api/games": catalog["games"], "/api/users": catalog["users"], "/api/userinfo": account, "/api/user/roles": {"roles": catalog["account"]["roles"]},
            "/api/permissions": permissions,
            "/api/workflow/filters": {"types": catalog["types"], "currencies": catalog["currencies"], "company": catalog["companies"], "departments": catalog["departments"]},
            "/api/workflow/directs": catalog["directions"],
        }

        def handle(h: BaseHTTPRequestHandler, method: str, body: bytes) -> None:
            path = urlsplit(h.path).path
            self.requests.append({"method": method, "path": path, "authorization": h.headers.get("Authorization")})
            if h.headers.get("Authorization") != "Bearer good":
                return _send(h, None, 401, 401)
            if method == "GET" and path in reads:
                return _send(h, reads[path])
            draft = re.fullmatch(r"/api/workflow/script/([0-9]+)", path)
            if method == "GET" and draft:
                script = self.drafts.get(int(draft.group(1)))
                return _send(h, {"script": script}) if script else _send(h, None, 200, 404)
            if method == "POST":
                if self.post_mode == "hang-up":
                    return _hang_up(h)
                if path == "/api/workflow/script/save":
                    payload = json.loads(body.decode("utf-8"))
                    script = {**payload, "id": self._next_id, "number": "GOFCN20260100009", "is_edit": True, "user_id": account["id"], "for_internal": 0, "sub_scripts": []}
                    self._next_id += 1
                    self.drafts[script["id"]] = script
                    return _send(h, {"id": script["id"], "status": script["status"]})
                if path == "/api/workflow/sub-script/multiple-add":
                    text = body.decode("utf-8")

                    def field(name: str) -> Optional[str]:
                        m = re.search(f'name="{name}"\\r\\n\\r\\n([^\\r]*)\\r\\n', text)
                        return m.group(1) if m else None

                    m = re.search(r'name="file"; filename="[^"]+"\r\nContent-Type: [^\r]+\r\n\r\n([\s\S]*?)\r\n--', text)
                    script = self.drafts.get(int(field("script_id") or 0))
                    if not script or not m:
                        return _send(h, None, 200, 400)
                    rows = parse_csv(re.sub("^\ufeff", "", m.group(1)))[1:]
                    subs = []
                    for row in rows:
                        subs.append({"id": self._next_script, "script_id": script["id"], "name": row[0], "data": row[1], "user_id": int(field("user_id") or 0),
                                     "tags": [{"id": 1, "full_name": row[2]}]})
                        self._next_script += 1
                    script["sub_scripts"] = subs
                    return _send(h, {"added": len(rows)})
            return _send(h, None, 404, 404)

        self._server = _Server(handle)
        self.origin = self._server.origin

    def close(self) -> None:
        self._server.close()


ACCOUNT = {"id": 1565, "name": "钟丰骏-实习生"}
GAME = {"id": 94, "name": "无尽冬日", "code": "GOFCN", "app_id": "gof.cn.prod", "icon_url": "https://example.invalid/icon.jpg"}
DIRS = [{"id": 1572252, "name": "无尽冬日 - NEW", "children": [
    {"id": 1573620, "name": "常规素材", "children": [
        {"id": 1573621, "name": "图片", "children": [{"id": 1573700, "name": "横版", "children": []}]},
        {"id": 1585089, "name": "双端共用-视频", "children": []}]},
    {"id": 1573590, "name": "营销素材", "children": [{"id": 1585181, "name": "双端共通", "children": []}, {"id": 1585190, "name": "图片", "children": []}]}]}]
PEOPLE = {
    "creative_users": [{"id": 653, "name": "张天伟"}, {"id": 1104, "name": "雷佳威"}, {"id": 1565, "name": "钟丰骏-实习生"}, {"id": 1233, "name": "李晨阳"},
                       {"id": 492, "name": "李晨阳"}, {"id": 2001, "name": "金莉"}],
    "producers": [{"id": 663, "name": "北京柒曦文化传媒有限公司（丁杰）"}, {"id": 1405, "name": "谢振文"}, {"id": 1163, "name": "盂县丰创智慧技术服务工作室（个体工商户）-杨燃"},
                  {"id": 653, "name": "张天伟"},
                  # The progress sheet calls this supplier 廿十一.
                  {"id": 2002, "name": "长沙廿拾一文化传媒有限公司"}],
}
TYPES = [{"id": "video", "name": "视频"}, {"id": "2d_video", "name": "2D视频"}, {"id": "image", "name": "图片"}, {"id": "aigc", "name": "AI生成"}, {"id": "other", "name": "其他"}]


class FakeCreative:
    """
    A stand-in for the CLS calls of the page 上传创意 and for the object storage its upload links point at, with the shapes the real ones
    answered with (2026-10-21). It answers only to `Bearer good`, keeps every request, and can be told to misbehave the ways a test needs: a
    create whose answer is lost (after or before it took effect), an annotation that is accepted but not applied, or refused.
    """

    def __init__(self, existing: Optional[list[dict]] = None, can_upload: bool = True) -> None:
        self.requests: list[dict] = []
        self.creatives: dict[int, dict] = {c["id"]: c for c in (existing or [])}
        #: Objects put to storage, by path, with their MD5.
        self.objects: dict[str, dict] = {}
        self.logs: list[str] = []
        self.create_mode = "normal"  # normal | lose-answer | hang-up | error500 | refuse | refuse-after-first
        self.annotate_mode = "normal"  # normal | ignore-once | refuse
        #: The signing call hands out links to a host the client must refuse.
        self.sign_elsewhere = False
        #: The Authorization values CLS takes; a test takes one away to have a login run out.
        self.tokens = {"Bearer good"}
        self._next_id = 556000
        self._ignored = False
        #: The latest link handed out for each "folder/file" key: what a create of that folder takes.
        self._signed_for: dict[str, str] = {}
        self._lock = threading.Lock()

        def handle(h: BaseHTTPRequestHandler, method: str, raw: bytes) -> None:
            with self._lock:
                self._handle(h, method, raw, can_upload)

        self._server = _Server(handle)
        self.origin = self._server.origin

    def _handle(self, h: BaseHTTPRequestHandler, method: str, raw: bytes, can_upload: bool) -> None:
        url = urlsplit(h.path)
        path = unquote(url.path)
        body: Optional[dict] = None
        try:
            body = json.loads(raw.decode("utf-8")) if raw and "json" in (h.headers.get("Content-Type") or "") else None
        except ValueError:
            body = None
        entry: dict[str, Any] = {"method": method, "path": path, "query": f"?{url.query}" if url.query else "", "authorization": h.headers.get("Authorization")}
        if body is not None:
            entry["body"] = body
        self.requests.append(entry)

        def send(data: Any, status: int = 200, code: int = 0, message: str = "OK") -> None:
            _send(h, data, status, code, message)

        if method == "PUT" and path.startswith("/cos/"):
            if h.headers.get("Authorization"):
                return send(None, 400, 400, "no credentials expected")
            digest = hashlib.md5(raw).hexdigest()
            self.objects[path] = {"md5": digest, "bytes": len(raw), "content_type": h.headers.get("Content-Type")}
            h.send_response(200)
            h.send_header("ETag", f'"{digest}"')
            h.send_header("Content-Length", "0")
            h.end_headers()
            return
        if h.headers.get("Authorization") not in self.tokens:
            return send(None, 401, 401, "unauthorized")
        q = parse_qs(url.query, keep_blank_values=True)

        def one(name: str) -> Optional[str]:
            return q[name][0] if name in q else None

        if method == "GET":
            if path == "/api/creative/games":
                return send([GAME])
            if path == "/api/userinfo":
                return send({**ACCOUNT, "status": 1, "roles": ["creative_plan"], "game_ids": [94],
                             "group_permissions": [{"project_ids": [94], "role_id": "creative_plan_junior", "permissions": {"pages": ["PAGE_MODULE_CREATIVE"]}}]})
            if path == "/api/permissions":
                return send({"creative_plan_junior": ["workflow.scripts"] if not can_upload else ["creative.upload", "creative.delete"], "creative_plan": ["creative.upload"]})
            if path == "/api/game/adsdesk/dirs":
                return send(DIRS)
            if path == "/api/game/recommend-filters":
                return send(PEOPLE)
            if path == "/api/material/filters":
                return send({"types": TYPES})
            if path == "/api/creatives":
                keywords = one("keywords")
                uploaders = [int(u) for u in q.get("upload_users[]", [])]
                confirmed = one("confirmed") != "0"
                found = [c for c in self.creatives.values() if (c.get("confirmed") is not False) == confirmed]
                if keywords:
                    found = [c for c in found if keywords in c["name"] or str(c["id"]) == keywords]
                if uploaders:
                    found = [c for c in found if c.get("upload_user_id") in uploaders]
                found.sort(key=lambda c: -c["id"])
                size, page = int(one("page_size") or 50), int(one("page") or 1)
                return send({"creatives": [{"id": c["id"], "name": c["name"], "upload_user_id": c.get("upload_user_id"), "user_id": c.get("user_id"),
                                            "user_ids": c.get("user_ids"), "sync_status": {"adsdesk": {"status": 0, "total": 1, "succ": 0, "fail": 0}}}
                                           for c in found[(page - 1) * size:page * size]], "total": len(found)})
            single = re.fullmatch(r"/api/creative/([0-9]+)", path)
            if single:
                c = self.creatives.get(int(single.group(1)))
                return send(c) if c else send(None, 200, 403, "创意不存在")
            return send(None, 404, 404, "not found")
        if method == "POST":
            if path == "/api/report/log":
                self.logs.append(str((body or {}).get("msg")))
                return send(None)
            if path == "/api/qcloud/signed-url":
                files = (body or {}).get("files") or []
                taken = [f.split("/")[0] for f in files if any(c["name"] == f.split("/")[0] for c in self.creatives.values())]
                if taken:
                    return send(None, 200, 403, f"以下创意已存在: {','.join(dict.fromkeys(taken))}")
                now = int(time.time())
                sign = quote(f"q-sign-algorithm=sha1&q-ak=AKIDtest&q-sign-time={now};{now + 3660}&q-key-time={now};{now + 3660}&q-header-list=host&q-url-param-list=&q-signature=abc", safe="-_.!~*'()")
                urls: dict[str, str] = {}
                mapping: dict[str, str] = {}
                for key in files:
                    file = key.split("/")[1] if "/" in key else ""
                    if not re.search(r"\.(mp4|mov|avi|m4v|jpe?g|png|gif|webp|bmp|html|mp3|wav|zip)$", file, re.IGNORECASE):
                        continue
                    target = f"/cos/creatives/2026/10/21/{uuid.uuid4().hex[:8]}/{file}"
                    base = "https://evil.example.com" if self.sign_elsewhere else self.origin
                    urls[key] = f"{base}{quote(target, safe='-_.!~*()/;:@&=+$,#')}?sign={sign}"
                    mapping[key] = key
                    self._signed_for[key] = target
                return send({"urls": urls, "mapping": mapping})
            if path == "/api/creative/multiple-upload":
                if self.create_mode == "hang-up":
                    return _hang_up(h)
                if self.create_mode == "error500":
                    return send(None, 502, 502, "bad gateway")
                if self.create_mode == "refuse":
                    return send(None, 200, 403, "素材处理失败")
                succ: list[dict] = []
                fail: list[str] = []
                for name in (body or {}).get("names") or []:
                    key = next((k for k in self._signed_for if k.split("/")[0] == name), None)
                    obj = self.objects.get(self._signed_for[key]) if key else None
                    if not obj or any((c.get("materials") or [{}])[0].get("file_hash") == obj["md5"] for c in self.creatives.values() if c.get("materials")):
                        fail.append(name)
                        continue
                    ident = self._next_id
                    self._next_id += 1
                    self.creatives[ident] = {"id": ident, "name": name, "game_id": (body or {}).get("game_id"), "user_id": None, "user_ids": [],
                                             "upload_user_id": ACCOUNT["id"], "params": {"types": [], "langs": []},
                                             "adsdesk_dir_id": ((body or {}).get("adsdesk_dir_ids") or {}).get(name), "status": 1, "confirmed": True,
                                             "materials": [{"id": ident * 10, "name": name, "file_hash": obj["md5"], "size": obj["bytes"], "file_path": self._signed_for[key]}]}
                    succ.append({"name": name, "id": ident})
                    if self.create_mode == "refuse-after-first":
                        return send(None, 200, 403, "部分素材处理失败")
                if self.create_mode == "lose-answer":
                    return _hang_up(h)
                return send({"succ": succ, "fail": fail})
            if path == "/api/creative/multiple-edit":
                if self.annotate_mode == "refuse":
                    return send(None, 200, 403, "没有编辑权限")
                if self.annotate_mode == "ignore-once" and not self._ignored:
                    self._ignored = True
                    return send(None)
                for c in (body or {}).get("creatives") or []:
                    found_creative = self.creatives.get(c["id"])
                    if not found_creative:
                        return send(None, 200, 403, f"创意不存在: {c['id']}")
                    found_creative.update({"user_id": c["sponsor"], "user_ids": c["producers"], "params": {**found_creative["params"], "types": c["types"]},
                                           "custom_tags": c["custom_tags"]})
                return send(None)
        return send(None, 404, 404, "not found")

    def close(self) -> None:
        self._server.close()


class ScriptedOnline:
    """An Online for tests: a session (or a refusal), one look's answer, optionally a QR step."""

    def __init__(self, session: Any = None, capture: Any = None, login_qr: Any = None, expected: Any = None) -> None:
        self._session = session
        self._capture = capture
        self._expected = expected
        self.login_qr = login_qr

    def session(self) -> dict:
        value = self._session() if callable(self._session) else self._session
        if isinstance(value, Exception):
            raise value
        return value

    def capture(self, wait_ms: int) -> dict:
        value = self._capture(wait_ms) if callable(self._capture) else self._capture
        return value if value is not None else {"state": "login_required"}

    def expected_account(self) -> Any:
        return self._expected


def session_of(origin: str, authorization: str = "Bearer good", account: Optional[dict] = None) -> ScriptedOnline:
    return ScriptedOnline(session={"authorization": authorization, "account": account or {"id": 7, "name": "张三"}, "origin": origin})
