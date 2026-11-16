"""The restricted CLS client: the only code that talks to CLS for requirements (cls-adapter.ts).

It is built for one logged-in session and can do exactly what the skill is allowed to do there: read the options, create an unpublished
draft, import this batch's CSV into that draft, and read the draft back. Publishing, deleting, updating existing requirements and uploading
materials have no method and no allowed path (the material upload is a separate client with its own allowed paths: creative_adapter.py).
Every write is checked against a sealed operation file first and is never retried; whatever it answers is read back and compared.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import re
from typing import Any, Callable, Optional

from . import js
from .cls import TAG_NAMES, parse_csv
from .util import Blocked, CSV_HEADERS, canonical
from .web import FetchError, Fetcher, MultipartForm, Response, fetch as default_fetch


class ClsError(Blocked):
    """`kind` is 'unknown' after a write whose outcome is not known: the draft has to be read back before anything else is done."""

    def __init__(self, message: str, kind: str = "blocked") -> None:
        super().__init__(message)
        self.kind = kind
        self.draft_id: Optional[int] = None
        self.operation_id: Optional[str] = None


FIELDS = ["game_id", "direct_id", "asset_data", "name", "type", "company", "departments",
          "marketing_user_id", "third_user_id", "currency", "price", "price_reason", "expect_end_date",
          "storyboard_end_date", "storyboard_attaches", "draft_end_date", "draft_attaches", "film_end_date", "film_attaches", "status"]
READ_PATHS = ["/api/games", "/api/users", "/api/userinfo", "/api/user/roles", "/api/permissions", "/api/workflow/filters", "/api/workflow/directs"]
WRITE_PATHS = ["/api/workflow/script/save", "/api/workflow/sub-script/multiple-add"]


def _assert(condition: Any, message: str) -> None:
    if not condition:
        raise ClsError(message)


def _id(value: Any) -> int:
    _assert(js.is_safe_int(value) and value > 0, "ID 无效")
    return int(value)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def draft_id_of(created: Any) -> Optional[int]:
    """The draft ID of a save response: a positive integer, bare or as a digit string, at the top or under `script` (as the read-back has it)."""
    candidates = []
    if isinstance(created, dict):
        candidates.append(created.get("id"))
        script = created.get("script")
        candidates.append(script.get("id") if isinstance(script, dict) else None)
    for raw in candidates:
        value = int(raw) if isinstance(raw, str) and re.fullmatch(r"[1-9][0-9]*", raw) else raw
        if js.is_safe_int(value) and value > 0:
            return int(value)
    return None


def json_or_none(response: Response) -> Any:
    try:
        return response.json()
    except Exception:
        return None


class ClsAdapter:
    def __init__(self, origin: str, authorization: str, timeout_ms: int = 30_000, fetch: Optional[Fetcher] = None,
                 before_write: Optional[Callable[[dict], None]] = None) -> None:
        if not origin or not authorization:
            raise ClsError("必须使用 CLS 当前登录会话")
        self._origin = origin
        self._authorization = authorization
        self._timeout = timeout_ms / 1000
        self._fetch = fetch or default_fetch
        self._before_write = before_write
        self.calls: list[dict] = []
        self._attempted: set[str] = set()

    def _request(self, method: str, path: str, body: Any = None) -> Any:
        allowed = (method == "GET" and (path in READ_PATHS or re.fullmatch(r"/api/workflow/script/[1-9][0-9]*", path))) or (method == "POST" and path in WRITE_PATHS)
        _assert(allowed, "发布、素材上传、删除和未知路径均被禁止")
        start = js.now_ms()
        headers = {"Authorization": self._authorization}
        payload: Any = None
        if isinstance(body, MultipartForm):
            payload = body
        elif body is not None:
            headers["Content-Type"] = "application/json"
            payload = js.stringify(body)
        try:
            response = self._fetch(f"{self._origin}{path}", method=method, headers=headers, body=payload, timeout=self._timeout, redirect="error")
        except FetchError as error:
            reason = "请求超时" if error.timeout else "连接失败"
            raise ClsError(f"写入结果未知（{reason}）；先回读，不重试" if method == "POST" else f"CLS 读取失败（{reason}）", "unknown" if method == "POST" else "blocked") from None
        data = json_or_none(response)
        code = data.get("code", js.UNDEFINED) if isinstance(data, dict) else js.UNDEFINED
        self.calls.append({"method": method, "path": path, "http_status": response.status, "business_code": code, "elapsed_ms": js.now_ms() - start})
        # The reason the user is told when CLS refuses: the HTTP status, CLS's own business code and its message.
        server_message = next((x for x in ((data.get("msg"), data.get("message")) if isinstance(data, dict) else ()) if isinstance(x, str) and js.trim(x)), None)
        detail = f"HTTP {response.status}{f'，业务码 {js.to_str(code)}' if code is not js.UNDEFINED else ''}{'，' + js.u16slice(js.trim(server_message), 0, 200) if server_message else ''}"
        # A gateway or server failure after a write was sent says nothing about whether the write took effect.
        if method == "POST" and (response.status >= 500 or response.status in (408, 429)):
            raise ClsError(f"写入结果未知（服务端或网关错误，{detail}）；先回读，不重试", "unknown")
        # A 2xx answer to a write without a business code (an HTML page from a gateway, say) is no refusal: the write may have landed.
        if method == "POST" and 200 <= response.status < 300 and not js.is_num(code):
            raise ClsError(f"写入结果未知（{detail}，响应没有业务码）；先回读，不重试", "unknown")
        _assert(200 <= response.status < 300 and code == 0 and js.is_num(code), f"CLS 请求失败（{detail}）；停止并核对现状")
        return data.get("data")

    def read_catalog(self) -> dict:
        games, users, account, roles, permissions, filters, directions = [self._request("GET", p) for p in READ_PATHS]
        _assert(isinstance(games, list) and isinstance(users, list) and isinstance(directions, list) and isinstance((roles or {}).get("roles"), list),
                "CLS 选项结构变化，停止")
        account = account or {}
        _assert(account.get("status") == 1 and isinstance(account.get("game_ids"), list)
                and isinstance(account.get("roles"), list) and all(x in roles["roles"] for x in account["roles"]), "当前账号/角色无法核对")
        granted = {p for role in roles["roles"] for p in ((permissions or {}).get(role) or [])}
        filters = filters or {}
        return {
            "schema": 1, "observed_at": js.iso(),
            "account": {"id": account.get("id"), "name": account.get("name"), "status": account.get("status"), "roles": roles["roles"], "game_ids": account["game_ids"]},
            "can_create": "workflow.script.save" in granted, "can_import": "workflow.script.edit" in granted,
            "games": [{"id": g.get("id", js.UNDEFINED), "name": g.get("name", js.UNDEFINED), "code": g.get("code", js.UNDEFINED)} for g in games],
            "users": [{"id": u.get("id", js.UNDEFINED), "name": u.get("name", js.UNDEFINED), "status": u.get("status", js.UNDEFINED), "roles": u.get("roles", js.UNDEFINED)} for u in users],
            "directions": [{"id": d.get("id", js.UNDEFINED), "name": d.get("name", js.UNDEFINED), "game_id": d.get("game_id", js.UNDEFINED)} for d in directions],
            "types": filters.get("types", js.UNDEFINED), "currencies": filters.get("currencies", js.UNDEFINED), "companies": filters.get("company", js.UNDEFINED),
            "departments": filters.get("departments", js.UNDEFINED),
        }

    def _validate_operation(self, op: dict) -> None:
        _assert(isinstance(op, dict) and op.get("schema") == 1 and op.get("action") in ("save_draft", "import_scripts"), "只允许新建草稿及本批脚本导入")
        unsealed = {k: v for k, v in op.items() if k != "operation_id"}
        _assert(_sha(canonical(unsealed).encode("utf-8")) == op.get("operation_id"), "操作文件摘要错误")
        payload = op.get("payload")
        _assert(isinstance(payload, dict) and len(payload) == len(FIELDS) and all(k in payload for k in FIELDS), "草稿字段未知或缺失")
        _assert(payload["status"] == -2 and "id" not in payload and payload["asset_data"] == "", "只允许创建未发布草稿，禁止更新已有需求")
        _assert(all(isinstance(payload[k], list) and len(payload[k]) == 0 for k in ("storyboard_attaches", "draft_attaches", "film_attaches")), "素材/附件上传不在范围内")
        expected = op.get("expected") or {}
        names = expected.get("names") or []
        _assert(len(names) == expected.get("quantity") and len(set(names)) == expected.get("quantity") and js.is_num(expected.get("quantity")) and expected["quantity"] > 0,
                "脚本数量/名称不符")
        _id(op.get("account_id"))
        _id(expected.get("creator_id"))
        _id(payload["game_id"])
        _id(payload["direct_id"])
        _id(payload["third_user_id"])
        current = self.read_catalog()
        _assert(current["account"]["id"] == op["account_id"] and current["can_create"], "账号或建单权限变化")
        _assert(payload["game_id"] in current["account"]["game_ids"] and any(g.get("id") == payload["game_id"] and g.get("code") == "GOFCN" for g in current["games"]),
                "不是获批 GOFCN 项目")
        _assert(any(d.get("id") == payload["direct_id"] and d.get("game_id") == payload["game_id"] for d in current["directions"]), "创意方向变化")
        _assert(any(u.get("id") == payload["third_user_id"] and u.get("status") == 1 and "outsource" in (u.get("roles") or []) for u in current["users"])
                and any(u.get("id") == expected["creator_id"] and u.get("status") == 1 for u in current["users"]), "供应商/创意人失效")
        if op["action"] == "import_scripts":
            _id(op.get("draft_id"))
            _assert(current["can_import"], "没有脚本编辑/导入权限")

    def read_draft(self, draft_id: int) -> dict:
        data = self._request("GET", f"/api/workflow/script/{_id(draft_id)}")
        source = data.get("script") if isinstance(data, dict) else None
        _assert(isinstance(source, dict) and source.get("id") == draft_id and isinstance(source.get("sub_scripts"), list), "草稿回读结构变化")
        parent: dict[str, Any] = {"id": source["id"], "number": source.get("number", js.UNDEFINED), "is_edit": source.get("is_edit", js.UNDEFINED),
                                  "user_id": source.get("user_id", js.UNDEFINED), "for_internal": source.get("for_internal", js.UNDEFINED)}
        for key in FIELDS:
            if key != "asset_data":
                parent[key] = source.get(key, js.UNDEFINED)
        subs = []
        for s in source["sub_scripts"]:
            tags = s.get("tags")
            subs.append({"id": s.get("id", js.UNDEFINED), "script_id": s.get("script_id", js.UNDEFINED), "name": s.get("name", js.UNDEFINED),
                         "data": s.get("data", js.UNDEFINED), "user_id": s.get("user_id", js.UNDEFINED),
                         "tags": [{"id": t.get("id", js.UNDEFINED), "full_name": t.get("full_name", js.UNDEFINED)} for t in tags] if isinstance(tags, list) else js.UNDEFINED})
        parent["sub_scripts"] = subs
        # What the record keeps is plain JSON: undefined fields are dropped as JSON.stringify drops them.
        return json.loads(js.stringify(parent))

    def _assert_parent(self, parent: dict, op: dict) -> None:
        _assert(parent.get("status") == -2, "需求不是未发布草稿")
        _assert(parent.get("user_id") == op["account_id"] and parent.get("for_internal") in (0, False) and parent.get("for_internal") is not True,
                "不是本账号的外包需求草稿")
        for key in FIELDS:
            if key == "asset_data":
                continue
            value = parent.get(key)
            wanted = op["payload"][key]
            if key == "price":
                equal = value is not None and math.isfinite(js.number(value)) and js.number(value) == js.number(wanted)
            elif wanted == "" and value is None:
                equal = True
            else:
                equal = canonical(value) == canonical(wanted)
            _assert(equal, f"草稿回读字段不符：{key}")

    def _assert_scripts(self, parent: dict, op: dict) -> None:
        scripts = parent["sub_scripts"]
        expected = op["expected"]
        _assert(len(scripts) == expected["quantity"] and len({canonical(s.get("id")) for s in scripts}) == len(scripts), "导入条数/脚本 ID 不符")
        for i, s in enumerate(scripts):
            _assert(s.get("script_id") == parent["id"] and s.get("name") == expected["names"][i] and s.get("user_id") == expected["creator_id"]
                    and s.get("data") == (expected["link"] if i == 0 else "同上") and any(t.get("full_name") in TAG_NAMES for t in (s.get("tags") or [])),
                    "导入名称/归属/链接/标签不符")

    def verify(self, op: dict, draft_id: int, start: Optional[int] = None) -> dict:
        start = len(self.calls) if start is None else start
        parent = self.read_draft(draft_id)
        self._assert_parent(parent, op)
        if op["action"] == "save_draft":
            _assert(len(parent["sub_scripts"]) == 0, "新草稿已有脚本，停止")
        else:
            self._assert_scripts(parent, op)
        return {"schema": 1, "transport": "cls_api", "status": "verified", "action": op["action"], "operation_id": op["operation_id"], "unpublished": True,
                "parent": parent, "calls": self.calls[start:]}

    def _write(self, op: dict, path: str, body: Any) -> Any:
        """A write whose failure is reported with the operation it belonged to, so it can be found again."""
        try:
            if self._before_write:
                self._before_write(op)
            return self._request("POST", path, body)
        except ClsError as error:
            error.operation_id = op.get("operation_id")
            raise

    @staticmethod
    def _after_write(error: Exception, draft_id: int, op: dict) -> None:
        """What went wrong after a write was sent: the draft exists (or may), so the caller must be told which one to read back."""
        if isinstance(error, ClsError):
            error.draft_id = draft_id
            error.operation_id = op.get("operation_id")

    def create_draft(self, op: dict) -> dict:
        _assert(isinstance(op, dict) and op.get("action") == "save_draft", "请求动作不符")
        self._validate_operation(op)
        _assert(op["operation_id"] not in self._attempted, "本会话已尝试此请求，不自动重试")
        self._attempted.add(op["operation_id"])
        start = len(self.calls)
        created = self._write(op, "/api/workflow/script/save", op["payload"])
        # The server accepted the request but did not say which draft it made: one may exist, so this is an unknown result, not a refusal.
        draft_id = draft_id_of(created)
        if draft_id is None:
            error = ClsError("创建响应没有草稿 ID；草稿可能已创建，先回读核对，不重试", "unknown")
            error.operation_id = op["operation_id"]
            raise error
        try:
            status = created.get("status") if isinstance(created, dict) and created.get("status") is not None else (
                created.get("script", {}).get("status") if isinstance(created, dict) and isinstance(created.get("script"), dict) else None)
            # the read-back below is the real check; this only stops on a plain contradiction
            _assert(status is None or status == -2, "创建结果不是草稿，停止")
            return self.verify(op, draft_id, start)
        except Exception as error:
            self._after_write(error, draft_id, op)
            raise

    def import_scripts(self, op: dict) -> dict:
        _assert(isinstance(op, dict) and op.get("action") == "import_scripts", "请求动作不符")
        self._validate_operation(op)
        start = len(self.calls)
        parent = self.read_draft(op["draft_id"])
        self._assert_parent(parent, op)
        _assert(parent.get("is_edit") is True or parent.get("is_edit") == 1, "当前草稿不可编辑")
        _assert(len(parent["sub_scripts"]) == 0, "草稿已有脚本；先核对，不导入")
        _assert(op["operation_id"] not in self._attempted, "本会话已尝试此导入，不自动重试")
        csv = op.get("csv") if isinstance(op.get("csv"), dict) else {}
        _assert(isinstance(csv.get("filename"), str) and re.fullmatch(r"[^/\\\r\n]+\.csv", csv["filename"], re.IGNORECASE), "只允许本批 CSV 脚本文件")
        data = base64.b64decode(csv.get("base64") or "")
        _assert(data[:3] == b"\xef\xbb\xbf" and _sha(data) == csv.get("sha256"), "CSV 编码/摘要错误")
        try:
            text = data[3:].decode("utf-8")
        except UnicodeDecodeError:
            raise ClsError("CSV 编码/摘要错误") from None
        rows = parse_csv(text)
        expected = [CSV_HEADERS, *[[n, op["expected"]["link"] if i == 0 else "同上", TAG_NAMES[0]] for i, n in enumerate(op["expected"]["names"])]]
        _assert(canonical(rows) == canonical(expected), "CSV 与获批需求不一致")
        form = MultipartForm()
        form.append("script_id", js.to_str(op["draft_id"]))
        form.append("file", data, filename=csv["filename"], content_type="text/csv")
        form.append("user_id", js.to_str(op["expected"]["creator_id"]))
        self._attempted.add(op["operation_id"])
        self._write(op, "/api/workflow/sub-script/multiple-add", form)
        try:
            return self.verify(op, op["draft_id"], start)
        except Exception as error:
            self._after_write(error, op["draft_id"], op)
            raise


def create_cls_adapter(origin: str, authorization: str, **options: Any) -> ClsAdapter:
    return ClsAdapter(origin, authorization, **options)
