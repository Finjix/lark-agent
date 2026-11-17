"""The restricted CLS client: the only code that talks to CLS for requirements.

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
    _assert(js.is_safe_int(value) and value > 0, "invalid ID")
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


def fetch_failed(error: FetchError, write: bool) -> ClsError:
    """A request that got no answer: after a write nobody knows whether it took effect."""
    reason = "timed out" if error.timeout else "connection failed"
    if write:
        return ClsError(f"write result unknown ({reason}): read back first, never retry", "unknown")
    return ClsError(f"CLS request failed ({reason})")


def answer_of(response: Response, method: str, path: str, write: bool, start: int, calls: list[dict]) -> Any:
    """
    The `data` of a CLS answer, the call logged in `calls`. A refusal names the HTTP status, CLS's business code and its message. A
    gateway or server failure after a write, or a 2xx answer to a write without a business code (an HTML page from a gateway, say), says
    nothing about whether the write took effect: an unknown result, not a refusal.
    """
    data = json_or_none(response)
    code = data.get("code", js.UNDEFINED) if isinstance(data, dict) else js.UNDEFINED
    calls.append({"method": method, "path": path, "http_status": response.status, "business_code": code, "elapsed_ms": js.now_ms() - start})
    server_message = next((x for x in ((data.get("msg"), data.get("message")) if isinstance(data, dict) else ()) if isinstance(x, str) and js.trim(x)), None)
    detail = (f"HTTP {response.status}{f', business code {js.to_str(code)}' if code is not js.UNDEFINED else ''}"
              f"{', ' + js.u16slice(js.trim(server_message), 0, 300) if server_message else ''}")
    if write and (response.status >= 500 or response.status in (408, 429)):
        raise ClsError(f"write result unknown (server or gateway error, {detail}): read back first, never retry", "unknown")
    if write and 200 <= response.status < 300 and not js.is_num(code):
        raise ClsError(f"write result unknown ({detail}, the answer has no business code): read back first, never retry", "unknown")
    _assert(200 <= response.status < 300 and js.is_num(code) and code == 0, f"CLS request failed ({detail})")
    return data.get("data")


class ClsAdapter:
    def __init__(self, origin: str, authorization: str, timeout_ms: int = 30_000, fetch: Optional[Fetcher] = None,
                 before_write: Optional[Callable[[dict], None]] = None) -> None:
        if not origin or not authorization:
            raise ClsError("a logged-in CLS session is needed")
        self._origin = origin
        self._authorization = authorization
        self._timeout = timeout_ms / 1000
        self._fetch = fetch or default_fetch
        self._before_write = before_write
        self.calls: list[dict] = []
        self._attempted: set[str] = set()

    def _request(self, method: str, path: str, body: Any = None) -> Any:
        allowed = (method == "GET" and (path in READ_PATHS or re.fullmatch(r"/api/workflow/script/[1-9][0-9]*", path))) or (method == "POST" and path in WRITE_PATHS)
        _assert(allowed, "publishing, uploading materials, deleting and unknown paths are not allowed")
        start = js.now_ms()
        headers = {"Authorization": self._authorization}
        payload: Any = None
        if isinstance(body, MultipartForm):
            payload = body
        elif body is not None:
            headers["Content-Type"] = "application/json"
            payload = js.stringify(body)
        write = method == "POST"
        try:
            response = self._fetch(f"{self._origin}{path}", method=method, headers=headers, body=payload, timeout=self._timeout, redirect="error")
        except FetchError as error:
            raise fetch_failed(error, write) from None
        return answer_of(response, method, path, write, start, self.calls)

    def read_catalog(self) -> dict:
        games, users, account, roles, permissions, filters, directions = [self._request("GET", p) for p in READ_PATHS]
        _assert(isinstance(games, list) and isinstance(users, list) and isinstance(directions, list) and isinstance((roles or {}).get("roles"), list),
                "the shape of CLS's options changed: stopped")
        account = account or {}
        _assert(account.get("status") == 1 and isinstance(account.get("game_ids"), list)
                and isinstance(account.get("roles"), list) and all(x in roles["roles"] for x in account["roles"]), "the account or its roles cannot be checked")
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
        _assert(isinstance(op, dict) and op.get("schema") == 1 and op.get("action") in ("save_draft", "import_scripts"), "only a new draft and the import of this batch's scripts are allowed")
        unsealed = {k: v for k, v in op.items() if k != "operation_id"}
        _assert(_sha(canonical(unsealed).encode("utf-8")) == op.get("operation_id"), "the operation file's digest is wrong")
        payload = op.get("payload")
        _assert(isinstance(payload, dict) and len(payload) == len(FIELDS) and all(k in payload for k in FIELDS), "unknown or missing draft fields")
        _assert(payload["status"] == -2 and "id" not in payload and payload["asset_data"] == "", "only an unpublished draft may be created; existing requirements are never updated")
        _assert(all(isinstance(payload[k], list) and len(payload[k]) == 0 for k in ("storyboard_attaches", "draft_attaches", "film_attaches")), "uploading materials or attachments is out of scope")
        expected = op.get("expected") or {}
        names = expected.get("names") or []
        _assert(len(names) == expected.get("quantity") and len(set(names)) == expected.get("quantity") and js.is_num(expected.get("quantity")) and expected["quantity"] > 0,
                "the scripts' quantity or names differ")
        _id(op.get("account_id"))
        _id(expected.get("creator_id"))
        _id(payload["game_id"])
        _id(payload["direct_id"])
        _id(payload["third_user_id"])
        current = self.read_catalog()
        _assert(current["account"]["id"] == op["account_id"] and current["can_create"], "the account or its permission to create requirements changed")
        _assert(payload["game_id"] in current["account"]["game_ids"] and any(g.get("id") == payload["game_id"] and g.get("code") == "GOFCN" for g in current["games"]),
                "not the approved GOFCN project")
        _assert(any(d.get("id") == payload["direct_id"] and d.get("game_id") == payload["game_id"] for d in current["directions"]), "the direction changed")
        _assert(any(u.get("id") == payload["third_user_id"] and u.get("status") == 1 and "outsource" in (u.get("roles") or []) for u in current["users"])
                and any(u.get("id") == expected["creator_id"] and u.get("status") == 1 for u in current["users"]), "the supplier or the creator is no longer active")
        if op["action"] == "import_scripts":
            _id(op.get("draft_id"))
            _assert(current["can_import"], "no permission to edit or import scripts")

    def read_draft(self, draft_id: int) -> dict:
        data = self._request("GET", f"/api/workflow/script/{_id(draft_id)}")
        source = data.get("script") if isinstance(data, dict) else None
        _assert(isinstance(source, dict) and source.get("id") == draft_id and isinstance(source.get("sub_scripts"), list), "the draft read-back changed shape")
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
        _assert(parent.get("status") == -2, "the requirement is not an unpublished draft")
        _assert(parent.get("user_id") == op["account_id"] and parent.get("for_internal") in (0, False) and parent.get("for_internal") is not True,
                "not an outsource requirement draft of this account")
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
            _assert(equal, f"draft read-back differs: {key}")

    def _assert_scripts(self, parent: dict, op: dict) -> None:
        scripts = parent["sub_scripts"]
        expected = op["expected"]
        _assert(len(scripts) == expected["quantity"] and len({canonical(s.get("id")) for s in scripts}) == len(scripts), "the imported count or script IDs differ")
        for i, s in enumerate(scripts):
            _assert(s.get("script_id") == parent["id"] and s.get("name") == expected["names"][i] and s.get("user_id") == expected["creator_id"]
                    and s.get("data") == (expected["link"] if i == 0 else "同上") and any(t.get("full_name") in TAG_NAMES for t in (s.get("tags") or [])),
                    "an imported name, owner, link or tag differs")

    def verify(self, op: dict, draft_id: int, start: Optional[int] = None) -> dict:
        start = len(self.calls) if start is None else start
        parent = self.read_draft(draft_id)
        self._assert_parent(parent, op)
        if op["action"] == "save_draft":
            _assert(len(parent["sub_scripts"]) == 0, "the new draft has scripts already: stopped")
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
        _assert(isinstance(op, dict) and op.get("action") == "save_draft", "wrong action for this request")
        self._validate_operation(op)
        _assert(op["operation_id"] not in self._attempted, "this request was attempted in this session already; it is not retried")
        self._attempted.add(op["operation_id"])
        start = len(self.calls)
        created = self._write(op, "/api/workflow/script/save", op["payload"])
        # The server accepted the request but did not say which draft it made: one may exist, so this is an unknown result, not a refusal.
        draft_id = draft_id_of(created)
        if draft_id is None:
            error = ClsError("the save answer has no draft ID; a draft may have been created: read back first, never retry", "unknown")
            error.operation_id = op["operation_id"]
            raise error
        try:
            status = created.get("status") if isinstance(created, dict) and created.get("status") is not None else (
                created.get("script", {}).get("status") if isinstance(created, dict) and isinstance(created.get("script"), dict) else None)
            # the read-back below is the real check; this only stops on a plain contradiction
            _assert(status is None or status == -2, "what was created is not a draft: stopped")
            return self.verify(op, draft_id, start)
        except Exception as error:
            self._after_write(error, draft_id, op)
            raise

    def import_scripts(self, op: dict) -> dict:
        _assert(isinstance(op, dict) and op.get("action") == "import_scripts", "wrong action for this request")
        self._validate_operation(op)
        start = len(self.calls)
        parent = self.read_draft(op["draft_id"])
        self._assert_parent(parent, op)
        _assert(parent.get("is_edit") is True or parent.get("is_edit") == 1, "the draft cannot be edited")
        _assert(len(parent["sub_scripts"]) == 0, "the draft has scripts already: check it first; nothing is imported")
        _assert(op["operation_id"] not in self._attempted, "this import was attempted in this session already; it is not retried")
        csv = op.get("csv") if isinstance(op.get("csv"), dict) else {}
        _assert(isinstance(csv.get("filename"), str) and re.fullmatch(r"[^/\\\r\n]+\.csv", csv["filename"], re.IGNORECASE), "only this batch's CSV script file is allowed")
        data = base64.b64decode(csv.get("base64") or "")
        _assert(data[:3] == b"\xef\xbb\xbf" and _sha(data) == csv.get("sha256"), "the CSV's encoding or digest is wrong")
        try:
            text = data[3:].decode("utf-8")
        except UnicodeDecodeError:
            raise ClsError("the CSV's encoding or digest is wrong") from None
        rows = parse_csv(text)
        expected = [CSV_HEADERS, *[[n, op["expected"]["link"] if i == 0 else "同上", TAG_NAMES[0]] for i, n in enumerate(op["expected"]["names"])]]
        _assert(canonical(rows) == canonical(expected), "the CSV differs from the approved requirement")
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
