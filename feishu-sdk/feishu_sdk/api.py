"""The allowlisted Feishu calls made with the user's token."""

from __future__ import annotations

import os
import random
import re
import shutil
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping
from urllib.parse import quote

from . import transport
from .auth import FeishuAuth
from .errors import BridgeError
from .jsutil import get, is_number, is_safe_integer, js_string, truthy

TOKEN = re.compile(r"[A-Za-z0-9]{8,64}")
SHEET_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
A1 = re.compile(r"[A-Za-z]{1,3}[0-9]{1,7}(:[A-Za-z]{1,3}[0-9]{1,7})?")
OCCUPIED = re.compile(r"non[- ]?empty|occupied|cannot overwrite|not allowed to overwrite|already (has|contains)|非空|已有内容|不为空|禁止覆盖", re.IGNORECASE)
#: Feishu codes: the user token is invalid or expired (a renewal may fix it), the permission is missing, too many requests.
TOKEN_REJECTED = {99991668, 99991677}
SCOPE_MISSING = {99991679, 99991672}
#: Too many requests: the platform's code, and the Sheets service's own (90217, "too many request").
RATE_LIMITED = {99991400, 90217}
#: A read turned down for too many requests is asked again until this many tries, waiting 1, 2, 4, 8 s (plus a little jitter) between them.
READ_TRIES = 5
MAX_WRITES = 100
#: A document read whole: larger ones are not edited here.
MAX_DOC_BLOCKS = 50_000
#: At most this many blocks created by one insert, and text blocks set by one update.
MAX_DOC_INSERT = 1000
MAX_DOC_UPDATES = 200
CLIENT_TOKEN = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
COLOR = re.compile(r"#[0-9a-fA-F]{6}")

#: The only read endpoints the workflow uses. Anything else is refused before it is sent.
READ_ALLOWLIST = (
    re.compile(r"/open-apis/wiki/v2/spaces/get_node"),
    re.compile(r"/open-apis/sheets/v3/spreadsheets/[A-Za-z0-9]+"),
    re.compile(r"/open-apis/sheets/v3/spreadsheets/[A-Za-z0-9]+/sheets/query"),
    re.compile(r"/open-apis/sheets/v2/spreadsheets/[A-Za-z0-9]+/(dataValidation|values_batch_get|metainfo)"),
    re.compile(r"/open-apis/docx/v1/documents/[A-Za-z0-9]+/raw_content"),
)


def _matches(pattern: re.Pattern[str], value: Any) -> bool:
    """`pattern.test(value)` of an anchored JavaScript expression: the value as a string, matched whole."""
    return pattern.fullmatch(value if isinstance(value, str) else js_string(value)) is not None


def _encode(value: str) -> str:
    """`encodeURIComponent`."""
    return quote(value, safe="!*'()")


@dataclass
class Call:
    method: str
    url: str
    params: Mapping[str, Any] | None = None
    data: Any = None
    stream: bool = False
    #: A write is never retried and a failure without an answer is reported as an unknown result.
    write: bool = False
    has_data: bool = field(default=False)


class FeishuApi:
    def __init__(self, auth: FeishuAuth, sleep: Callable[[float], None] | None = None) -> None:
        self.auth = auth
        self.sleep: Callable[[float], None] = sleep or (lambda ms: time.sleep(ms / 1000))

    def _send(self, call: Call, again: bool = True, attempt: int = 1) -> Any:
        token = self.auth.access_token()
        self.auth.credentials()
        url = f"{self.auth.endpoints.open}{call.url}"
        try:
            result = transport.request(call.method, url, params=call.params, json_body=call.data,
                                       headers={"Authorization": f"Bearer {token}"}, stream=call.stream)
        except transport.HttpError as error:
            # A stream body of an error response is not read: the status says enough.
            body = error.body if isinstance(error.body, dict) else {}
            return self._failure(call, body, error.status, again, attempt, token)
        except transport.NoResponse:
            if call.write:
                raise BridgeError("unknown_result", "The Feishu write got no answer and may have been applied: read back to check, never resend") from None
            raise BridgeError("network", "Cannot reach Feishu") from None
        if call.stream:
            return result
        if isinstance(result, dict) and is_number(result.get("code")) and result["code"] != 0:
            return self._failure(call, result, 200, again, attempt, token)
        return result["data"] if isinstance(result, dict) and "data" in result else result

    def _failure(self, call: Call, body: dict[str, Any], status: int, again: bool, attempt: int, token: str) -> Any:
        code = body.get("code") if is_number(body.get("code")) else None
        raw = next((body[key] for key in ("msg", "message", "error_description") if body.get(key) is not None), None)
        message = js_string(raw) if raw is not None else f"HTTP {status}"
        if code is not None and code in TOKEN_REJECTED:
            # Feishu turns the token down before it looks at the request, so even a write did not happen and may go once more. The renewal
            # is skipped when another call has renewed the token meanwhile (the stored one is no longer the one turned down).
            if again:
                self.auth.refresh(True, token)
                return self._send(call, False)
            raise BridgeError("auth_required", "The Feishu login is no longer valid: log in again", code)
        if not call.write and (status == 429 or code in RATE_LIMITED) and attempt < READ_TRIES:
            self.sleep(1000 * 2 ** (attempt - 1) + random.randint(0, 250))
            return self._send(call, again, attempt + 1)
        if code is not None and code in SCOPE_MISSING:
            raise BridgeError("scope_missing", f"The Feishu login lacks a permission this call needs: enable it in the Feishu app and log in again ({message})", code)
        if call.write and OCCUPIED.search(message):
            raise BridgeError("occupied", "Feishu refused to write into a non-empty target", code)
        if call.write and status >= 500:
            raise BridgeError("unknown_result", f"The Feishu write returned {status} and may have been applied: read back to check, never resend", code)
        shown = f"（HTTP {status}）" if status != 200 and not message.startswith("HTTP ") else ""
        raise BridgeError("api", f"Feishu API error{shown}: {message}", code)

    def verify_user(self) -> dict[str, str]:
        """Asks Feishu who the stored login belongs to.

        It is a real call with the user's token, so a login that has lapsed, been revoked or been used up shows up here (after one renewal
        attempt) instead of in the middle of a write; the renewal also keeps an idle login alive.
        """
        data = self._send(Call("GET", "/open-apis/authen/v1/user_info"))
        open_id = get(data, "open_id") if isinstance(get(data, "open_id"), str) else ""
        if not open_id:
            raise BridgeError("api", "Cannot tell which Feishu account is logged in")
        name = get(data, "name")
        return {"open_id": open_id, **({"name": name} if isinstance(name, str) and name else {})}

    def get(self, url_path: Any, params: Mapping[str, Any] | None = None) -> Any:
        """A read-only call to one of the endpoints the workflow uses."""
        if (not isinstance(url_path, str) or ".." in url_path or "?" in url_path or not url_path.startswith("/open-apis/")
                or not any(pattern.fullmatch(url_path) for pattern in READ_ALLOWLIST)):
            raise BridgeError("invalid_request", "Feishu read path not allowed")
        query: dict[str, Any] = {}
        for key, value in (params or {}).items():
            if not isinstance(value, (str, int, float)):
                raise BridgeError("invalid_request", f"Unsupported type of parameter {key}")
            query[key] = value
        return self._send(Call("GET", url_path, params=query))

    def revision(self, token: Any) -> int:
        """The workbook version. The same endpoint is read before and after, so only the change matters, not what the number means."""
        self._check_token(token)
        data = self._send(Call("GET", f"/open-apis/sheets/v2/spreadsheets/{token}/metainfo"))
        value = get(get(data, "properties"), "revision")
        if not is_safe_integer(value) or value < 0:
            raise BridgeError("api", "Cannot read the document revision")
        return int(value)

    def export_xlsx(self, token: Any, output: Any) -> dict[str, Any]:
        """Exports the whole workbook as a new XLSX file; an existing file is never replaced."""
        self._check_token(token)
        if not isinstance(output, str) or not os.path.isabs(output) or not output.lower().endswith(".xlsx"):
            raise BridgeError("invalid_request", "The export path must be an absolute .xlsx path")
        if os.path.exists(output):
            raise BridgeError("invalid_request", "The export file already exists and is not overwritten")
        os.makedirs(os.path.dirname(output), exist_ok=True)

        created = self._send(Call("POST", "/open-apis/drive/v1/export_tasks", data={"file_extension": "xlsx", "token": token, "type": "sheet"}))
        ticket = get(created, "ticket")
        if not isinstance(ticket, str) or not ticket:
            raise BridgeError("api", "The export task returned no ticket")
        file_token = ""
        attempt = 0
        while attempt < 40 and not file_token:
            if attempt:
                self.sleep(min(3000, 500 + attempt * 250))
            polled = self._send(Call("GET", f"/open-apis/drive/v1/export_tasks/{_encode(ticket)}", params={"token": token}))
            job = get(polled, "result")
            status = get(job, "job_status")
            if status == 0 and not isinstance(status, bool) and isinstance(get(job, "file_token"), str):
                file_token = job["file_token"]
            elif truthy(job) and status not in (1, 2):
                detail = get(job, "job_error_msg")
                raise BridgeError("api", f"Export failed: {js_string(detail if detail is not None else status)}")
            attempt += 1
        if not file_token:
            raise BridgeError("api", f"The export task is not finished yet (ticket={ticket}): only query it later, do not submit it again")

        response = self._send(Call("GET", f"/open-apis/drive/v1/export_tasks/file/{_encode(file_token)}/download", stream=True))
        try:
            try:
                handle = open(output, "xb")
            except FileExistsError:
                # Another run made this file in the meantime; it is not ours to delete.
                raise BridgeError("invalid_request", "The export file already exists and is not overwritten") from None
            try:
                with handle:
                    try:
                        shutil.copyfileobj(response, handle)
                    except OSError:
                        raise BridgeError("network", "Cannot reach Feishu") from None
                size = os.path.getsize(output)
                with open(output, "rb") as check:
                    head = check.read(2)
                if size < 22 or head != b"PK":
                    raise BridgeError("api", "The exported file is not a valid XLSX")
                return {"path": output, "bytes": size}
            except BaseException:
                try:
                    os.remove(output)
                except OSError:
                    pass
                raise
        finally:
            response.close()

    def cells_set(self, token: Any, writes: Any) -> Any:
        """Writes cell ranges without ever overwriting.

        The server is told allow_overwrite=false for every range and refuses when a target cell is not empty. Same request as
        `lark-cli sheets +cells-set --allow-overwrite=false` (captured with its --dry-run).
        """
        self._check_token(token)
        if not isinstance(writes, list) or len(writes) < 1 or len(writes) > MAX_WRITES:
            raise BridgeError("invalid_request", f"The number of write ranges must be between 1 and {MAX_WRITES}")
        operations = []
        for write in writes:
            if get(write, "allow_overwrite") is not False:
                raise BridgeError("invalid_request", "Overwrite protection missing (allow_overwrite must be false)")
            sheet_id = write.get("sheet_id") if write.get("sheet_id") is not None else ""
            cell_range = write.get("range") if write.get("range") is not None else ""
            if not _matches(SHEET_ID, sheet_id) or not _matches(A1, cell_range):
                raise BridgeError("invalid_request", "Invalid write range")
            cells = write.get("cells")
            if not isinstance(cells, list) or not cells or not all(isinstance(row, list) and row for row in cells):
                raise BridgeError("invalid_request", "Write contents must be a two-dimensional array")
            operations.append({"input": {"allow_overwrite": False, "cells": cells, "excel_id": token, "range": write.get("range"),
                                         "sheet_id": write.get("sheet_id")}, "tool_name": "set_cell_range"})
        from .jsutil import dumps

        return self._send(Call("POST", f"/open-apis/sheet_ai/v2/spreadsheets/{token}/tools/invoke_write", write=True,
                               data={"input": dumps({"excel_id": token, "operations": operations}), "tool_name": "batch_update"}))

    def create_workbook(self, title: Any) -> dict[str, Any]:
        """Creates a new, empty workbook. Only the isolated safety probe uses it."""
        if not isinstance(title, str) or not title.strip() or len(title) > 200:
            raise BridgeError("invalid_request", "Invalid spreadsheet title")
        data = self._send(Call("POST", "/open-apis/sheets/v3/spreadsheets", write=True, data={"title": title}))
        sheet = get(data, "spreadsheet")
        if not isinstance(get(sheet, "spreadsheet_token"), str):
            raise BridgeError("unknown_result", "Whether the spreadsheet was created is unknown: do not create it again")
        return {"spreadsheet_token": sheet["spreadsheet_token"], **({"url": sheet["url"]} if sheet.get("url") else {})}

    def add_dropdown_option(self, token: Any, sheet_id: str, column: str, first_row: float, last_row: float, option: str, color: str) -> dict[str, Any]:
        """Adds one option to the existing dropdown of a column span.

        Every rule variant found there keeps its own options, colors and mode (single or multiple choice) and only gains the new option,
        range by range, so no cell changes its look and nothing is merged. Cell values are not touched. Never creates a dropdown where there
        is none. A range whose rule already has the option is left alone. Returns what was sent, for a read-back.
        """
        self._check_token(token)
        if not SHEET_ID.fullmatch(sheet_id) or not re.fullmatch(r"[A-Za-z]{1,3}", column):
            raise BridgeError("invalid_request", "Invalid sheet or column")
        if not is_safe_integer(first_row) or not is_safe_integer(last_row) or first_row < 2 or last_row < first_row or last_row - first_row >= 5000:
            raise BridgeError("invalid_request", "Invalid row range (from row 2 on, at most 5000 rows at a time)")
        first, last = int(first_row), int(last_row)
        if not isinstance(option, str) or not option.strip() or option != option.strip() or len(option) > 100 or re.search(r"[,\n\r]", option):
            raise BridgeError("invalid_request", "Invalid option name")
        if not COLOR.fullmatch(color):
            raise BridgeError("invalid_request", "Invalid colour")
        url = f"/open-apis/sheets/v2/spreadsheets/{token}/dataValidation"
        requested = f"{sheet_id}!{column}{first}:{column}{last}"
        before = self._send(Call("GET", url, params={"range": requested, "dataValidationType": "list"}))
        rules = get(before, "dataValidations") if isinstance(get(before, "dataValidations"), list) else []
        if not rules:
            raise BridgeError("invalid_request", "The range has no dropdown; refusing to create one")
        plan: list[dict[str, Any]] = []
        for rule in rules:
            values = get(rule, "conditionValues")
            mode = get(rule, "options")
            mode = mode if mode is not None else {}
            color_map = get(mode, "colorValueMap")
            color_map = color_map if color_map is not None else {}
            if (get(rule, "dataValidationType") != "list" or not isinstance(values, list) or not all(isinstance(v, str) for v in values)
                    or not isinstance(get(rule, "ranges"), list) or not isinstance(color_map, dict)):
                raise BridgeError("api", "Unsupported dropdown rule")
            multiple = get(mode, "multipleValues")
            highlight_raw = get(mode, "highlightValidData")
            if not isinstance(False if multiple is None else multiple, bool) or not isinstance(False if highlight_raw is None else highlight_raw, bool):
                raise BridgeError("api", "The dropdown mode cannot be kept reliably")
            highlight = highlight_raw is True
            if highlight and not all(isinstance(color_map.get(v), str) and COLOR.fullmatch(color_map[v]) for v in values):
                raise BridgeError("api", "The dropdown colours cannot be kept reliably")
            if option in values:
                continue
            options = [*values, option]
            colors = [*(color_map[v] for v in values), color] if highlight else []
            for item in rule["ranges"]:
                if not isinstance(item, str) or not item.startswith(f"{sheet_id}!"):
                    raise BridgeError("api", "Cannot parse the dropdown range")
                plan.append({"range": item, "options": options, "multiple": multiple is True, "highlight": highlight, "colors": colors})
        if not plan:
            raise BridgeError("invalid_request", "Every dropdown in the range already has this option")
        done: list[str] = []
        for item in plan:
            options_sent: dict[str, Any] = {"multipleValues": item["multiple"], "highlightValidData": item["highlight"]}
            if item["highlight"]:
                options_sent["colors"] = item["colors"]
            self._send(Call("POST", url, write=True, data={
                "range": item["range"], "dataValidationType": "list",
                "dataValidation": {"conditionValues": item["options"], "options": options_sent},
            }))
            done.append(item["range"])
        return {"requested": requested, "option": option, "updated_ranges": len(done), "ranges": done}

    def docx_blocks(self, document_id: Any) -> dict[str, Any]:
        """Every block of a docx, page by page, with the revision they were read at.

        The revision is read before and after: a document edited in between is read again (twice at most), so the blocks are all of one
        revision.
        """
        self._check_token(document_id, "document")

        def head() -> Any:
            document = get(self._send(Call("GET", f"/open-apis/docx/v1/documents/{document_id}")), "document")
            return document if document is not None else {}

        for _ in range(3):
            before = head()
            blocks: list[Any] = []
            page = ""
            while True:
                params: dict[str, Any] = {"page_size": 500, "document_revision_id": -1}
                if page:
                    params["page_token"] = page
                data = self._send(Call("GET", f"/open-apis/docx/v1/documents/{document_id}/blocks", params=params))
                items = get(data, "items")
                if not isinstance(items, list):
                    raise BridgeError("api", "Unexpected shape of the document blocks")
                blocks.extend(items)
                more = get(data, "has_more")
                token = get(data, "page_token")
                page = js_string(token if token is not None else "") if more else ""
                if more and not page:
                    raise BridgeError("api", "Document block paging returned no next-page token")
                if len(blocks) > MAX_DOC_BLOCKS:
                    raise BridgeError("api", f"Documents of more than {MAX_DOC_BLOCKS} blocks are not supported")
                if not page:
                    break
            after = head()
            revision = get(before, "revision_id")
            if is_safe_integer(revision) and revision == get(after, "revision_id"):
                title = get(before, "title")
                return {"revision": int(revision), "title": js_string(title if title is not None else ""), "blocks": blocks}
        raise BridgeError("api", "The document kept changing while it was read: read it again later")

    def docx_insert(self, document_id: Any, parent_id: Any, index: float, children_ids: list[str], descendants: Any, client_token: Any) -> Any:
        """Creates blocks (with their children: table cells, their text) under `parent_id` at `index`, in one request.

        Only ever adds: nothing existing is replaced or moved. `client_token` makes the same request, sent again, a no-op on Feishu's side.
        """
        self._check_token(document_id, "document")
        self._check_token(parent_id, "block")
        if not is_safe_integer(index) or index < 0:
            raise BridgeError("invalid_request", "Invalid insert position")
        if not isinstance(children_ids, list) or not children_ids or not isinstance(descendants, list) or len(descendants) > MAX_DOC_INSERT:
            raise BridgeError("invalid_request", f"Insert contents are empty or more than {MAX_DOC_INSERT} blocks")
        if not isinstance(client_token, str) or not CLIENT_TOKEN.fullmatch(client_token):
            raise BridgeError("invalid_request", "client_token missing")
        return self._send(Call("POST", f"/open-apis/docx/v1/documents/{document_id}/blocks/{parent_id}/descendant", write=True,
                               params={"document_revision_id": -1, "client_token": client_token},
                               data={"children_id": children_ids, "index": int(index), "descendants": descendants}))

    def docx_set_texts(self, document_id: Any, updates: Any, client_token: Any) -> Any:
        """Sets the text of existing text blocks (the caller has checked they are empty): one batch_update request."""
        self._check_token(document_id, "document")
        if not isinstance(updates, list) or not updates or len(updates) > MAX_DOC_UPDATES:
            raise BridgeError("invalid_request", f"The number of updates must be between 1 and {MAX_DOC_UPDATES}")
        for update in updates:
            self._check_token(get(update, "block_id"), "block")
            elements = update.get("elements")
            if not isinstance(elements, list) or not elements:
                raise BridgeError("invalid_request", "Update contents are empty")
        if not isinstance(client_token, str) or not CLIENT_TOKEN.fullmatch(client_token):
            raise BridgeError("invalid_request", "client_token missing")
        return self._send(Call("PATCH", f"/open-apis/docx/v1/documents/{document_id}/blocks/batch_update", write=True,
                               params={"document_revision_id": -1, "client_token": client_token},
                               data={"requests": [{"block_id": u["block_id"], "update_text_elements": {"elements": u["elements"]}} for u in updates]}))

    @staticmethod
    def _check_token(token: Any, what: str = "spreadsheet") -> None:
        if not isinstance(token, str) or not TOKEN.fullmatch(token):
            raise BridgeError("invalid_request", f"Invalid {what} token")
