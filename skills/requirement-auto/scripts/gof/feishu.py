"""The allowlisted Feishu transport.

It calls the feishu skill's bridge (see agent_feishu.py) and orchestrates validate → write → read back → verify. No generic write, no v2 values
write, no implicit retry, no target override.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
import time
from typing import Any, Callable, Optional

from . import js
from .a1 import CellRange, column_letter
from .agent_feishu import BRIDGE_VERSION, feishu_serve
from .seal import check_seal, seal
from .snapshot import FIELD_HEADERS, Snapshot, header_matches
from .util import Blocked, NODE, TARGET, WriteSent, canonical, digest, need, read_json, write_json_new

SCOPES = ["wiki:wiki:readonly", "sheets:spreadsheet:readonly", "sheets:spreadsheet:read", "sheets:spreadsheet:write_only",
          "sheets:spreadsheet", "drive:export:readonly", "docx:document:readonly"]


class OccupiedRefusal(Blocked):
    """An explicit non-empty-target refusal, not a timeout or permission failure."""


class WriteUnknown(Blocked):
    """A write whose outcome is not known (no answer, or a network failure after it was sent): it has to be read back, never sent again."""


UNKNOWN_WRITE = "write result unknown: read back first, never retry"


# Bridge failures whose message is ours (fixed text, already scrubbed of credentials) and may be shown as is.
SHOWN_KINDS = {"auth_required", "config", "identity_mismatch", "scope_missing", "invalid_request", "login_denied", "login_expired"}

BridgeRunner = Callable[[str, Any], dict]


class Bridge:
    def __init__(self, run: Optional[BridgeRunner] = None) -> None:
        self.calls: list[dict] = []
        self._run = run if run is not None else (lambda command, input_: feishu_serve().call(command, input_))

    def call(self, name: str, payload: Any = None, write: bool = False, path: Optional[str] = None) -> Any:
        started = js.now_ms()
        try:
            reply = self._run(name, {} if payload is None else payload)
        except Blocked:
            # A refusal before anything went out (the feishu skill is missing, FEISHU_CLI is wrong) says what is to be done about it.
            raise
        except Exception:
            raise (WriteUnknown(UNKNOWN_WRITE) if write else Blocked("the Feishu bridge call failed")) from None
        need(isinstance(reply, dict) and isinstance(reply.get("ok"), bool), "the Feishu bridge answered in an unexpected shape")
        self.calls.append({"command": [name], "path": path, "write": write, "elapsed_ms": js.now_ms() - started, "ok": reply["ok"]})
        # The bridge has already redacted credentials from the message, so its reason can be passed on.
        if not reply["ok"]:
            error = reply.get("error") if isinstance(reply.get("error"), dict) else {}
            kind = error.get("kind")
            message = error.get("message")
            if kind == "occupied":
                raise OccupiedRefusal("the server refused to write into a non-empty target")
            if kind == "unknown_result" or (write and kind == "network"):
                raise WriteUnknown(UNKNOWN_WRITE)
            if kind in SHOWN_KINDS:
                raise Blocked(f"Feishu unavailable ({kind}): {js.to_str(message)}")
            code = error.get("code", js.UNDEFINED)
            reason = js.u16slice(js.trim(js.to_str(message if message is not None else "")), 0, 300)
            code_text = f", code {js.to_str(code)}" if code is not js.UNDEFINED else ""
            raise Blocked(f"Feishu API call failed ({js.to_str(kind)}{code_text}){': ' + reason if reason else ''}{'; read the write back to check it' if write else ''}")
        data = reply.get("data")
        Bridge.complete(data)
        return data

    @staticmethod
    def complete(value: Any) -> None:
        """A reply that mentions a failed item or truncated data is never taken as complete."""
        if isinstance(value, list):
            for v in value:
                Bridge.complete(v)
        elif isinstance(value, dict):
            errors = value.get("errors")
            need(value.get("ok") is not False and value.get("success") is not False
                 and not (len(errors) if isinstance(errors, list) else js.truthy(errors)) and not js.truthy(value.get("failed_count")),
                 "the answer contains a failed item: stopped; read back")
            need(value.get("status") not in ("partial_success", "failure", "failed", "error"), "the answer reports partial success or failure: stopped; read back, nothing is written to make up for it")
            need(not any(js.truthy(value.get(k)) for k in ("truncated", "is_truncated", "has_more", "hasMore")), "the answer is truncated and cannot be checked")
            for v in value.values():
                Bridge.complete(v)

    def check_login(self) -> dict:
        """
        Is the Feishu login good right now? Unlike `doctor` this asks Feishu, and it says what is wrong instead of stopping:
        ok | login_required | identity_mismatch | scopes_missing | unreachable | blocked.
        """
        try:
            reply = self._run("auth-verify", {})
        except Blocked as error:
            # A feishu skill that cannot be found is not a moment's outage: checking again later does not help.
            return {"status": "blocked", "reason": error.message}
        except Exception:
            return {"status": "unreachable", "reason": "the Feishu bridge call failed"}
        if not isinstance(reply, dict) or not isinstance(reply.get("ok"), bool):
            return {"status": "blocked", "reason": "the Feishu bridge answered in an unexpected shape"}
        if reply["ok"]:
            data = reply.get("data") if isinstance(reply.get("data"), dict) else {}
            missing = data.get("missing_scopes") if isinstance(data.get("missing_scopes"), list) else []
            who = {"name": data["name"]} if js.truthy(data.get("name")) else {}
            if missing:
                return {"status": "scopes_missing", **who, "missing_scopes": missing, "reason": "the Feishu login lacks permissions this skill needs: log in again with the missing scopes"}
            return {"status": "ok", **who}
        error = reply.get("error") if isinstance(reply.get("error"), dict) else {}
        kind = error.get("kind")
        message = error.get("message")
        if kind == "auth_required":
            return {"status": "login_required", "reason": message}
        if kind == "identity_mismatch":
            return {"status": "identity_mismatch", "reason": message}
        if kind == "scope_missing":
            return {"status": "scopes_missing", "reason": message}
        if kind == "network":
            return {"status": "unreachable", "reason": "Feishu cannot be reached for now: check again a little later; the login has not lapsed"}
        if kind in SHOWN_KINDS:
            return {"status": "blocked", "reason": message}
        return {"status": "blocked", "reason": "the Feishu API call failed: the login state is not known"}

    def doctor(self) -> dict:
        info = self.call("info")
        need(isinstance(info, dict) and info.get("bridge") == BRIDGE_VERSION, "the feishu skill's bridge version does not match this skill")
        need(info.get("app_configured") is True, "no Feishu app configured (FEISHU_APP_ID / FEISHU_APP_SECRET): the host or an administrator sets it")
        auth = self.call("auth-status")
        need(isinstance(auth, dict) and auth.get("logged_in") is True, "Feishu is not logged in: log the user in with the feishu skill (login qr, then login finish)")
        self.check_scopes(SCOPES)
        return {"version": BRIDGE_VERSION, "node": info.get("node", js.UNDEFINED), "logged_in": True, "identity": "user",
                "user_name": auth.get("name", js.UNDEFINED), "open_id": auth.get("open_id", js.UNDEFINED), "storage": auth.get("storage", js.UNDEFINED),
                "required_scopes": SCOPES}

    def check_scopes(self, required: list[str]) -> None:
        checked = self.call("auth-check-scopes", {"required": required})
        checked = checked if isinstance(checked, dict) else {}
        need(checked.get("complete") is True and not (checked.get("missing") or []) and all(s in (checked.get("granted") or []) for s in required),
             "the Feishu login lacks permissions this skill needs: log in again")

    def api_read(self, api_path: str, params: Optional[dict] = None) -> Any:
        need(api_path.startswith("/open-apis/") and ".." not in api_path and "?" not in api_path, "invalid API path")
        return self.call("api-get", {"path": api_path, "params": params or {}}, path=api_path)

    def revision(self, token: str) -> Any:
        need(isinstance(token, str) and re.fullmatch(r"[A-Za-z0-9]+", token), "invalid spreadsheet token")
        return self.call("sheets-revision", {"token": token})

    def export_sheet(self, token: str, output: str) -> Any:
        need(isinstance(token, str) and re.fullmatch(r"[A-Za-z0-9]+", token), "invalid spreadsheet token")
        return self.call("sheets-export", {"token": token, "output": output})

    def cells_set(self, token: str, writes: Any) -> Any:
        """The only write: every range goes out with allow_overwrite=false, which the server enforces."""
        need(isinstance(token, str) and re.fullmatch(r"[A-Za-z0-9]+", token), "invalid spreadsheet token")
        need(isinstance(writes, list) and len(writes) > 0 and all(isinstance(w, dict) and w.get("allow_overwrite") is False for w in writes), "allow_overwrite:false is missing")
        return self.call("sheets-cells-set", {"token": token, "writes": writes}, write=True)

    def dropdown_add(self, args: dict) -> Any:
        """Adds one option to the existing dropdown of one column span; every rule variant keeps its own options and colors."""
        need(isinstance(args.get("token"), str) and re.fullmatch(r"[A-Za-z0-9]+", args["token"]), "invalid spreadsheet token")
        return self.call("sheets-dropdown-add", args, write=True)

    def create_workbook(self, title: str) -> Any:
        return self.call("sheets-create", {"title": title}, write=True)

    def docx_blocks(self, document: str) -> dict:
        """Every block of a docx, all of one revision (see doc.py)."""
        return self.call("docx-blocks", {"document": document})

    def docx_insert(self, args: dict) -> Any:
        """New blocks in a docx (doc.py plans them, checks the place and reads them back); never replaces anything."""
        return self.call("docx-insert", args, write=True)

    def docx_set_texts(self, args: dict) -> Any:
        """Text into empty text blocks of a docx (doc.py checks they are empty and reads them back)."""
        return self.call("docx-set-texts", args, write=True)


def _revision_of(bridge: Bridge, token: str) -> int:
    value = (bridge.revision(token) or {}).get("revision")
    need(js.is_int(value) and value >= 0, "cannot read the document revision")
    return int(value)


def target(bridge: Bridge) -> str:
    data = bridge.api_read("/open-apis/wiki/v2/spaces/get_node", {"token": NODE, "obj_type": "wiki"})
    node = (data or {}).get("node") or {}
    need(node.get("node_token") == NODE and node.get("obj_type") == "sheet", "the target is not the spreadsheet of the approved node")
    token = node.get("obj_token") or ""
    need(isinstance(token, str) and re.fullmatch(r"[A-Za-z0-9]+", token), "the spreadsheet token is missing")
    return token


def sheets_of(bridge: Bridge, token: str) -> list[dict]:
    data = bridge.api_read(f"/open-apis/sheets/v3/spreadsheets/{token}/sheets/query")
    found = (data or {}).get("sheets")
    need(isinstance(found, list) and len(found) > 0, "cannot list every tab")
    need(all(isinstance(s, dict) and s.get("resource_type") == "sheet" for s in found), "a tab is not a grid and cannot be checked")
    need(len({canonical(s.get("title")) for s in found}) == len(found), "tab titles are not unique")
    return found


def _type_name(value: Any) -> str:
    if isinstance(value, list):
        return "array"
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if js.is_num(value):
        return "number"
    if isinstance(value, str):
        return "string"
    return "object"


def _shape_of(data: Any, sid: str) -> str:
    """Names and types only, never values: enough to see how a reply differs from the documented one without echoing an untrusted body."""
    if not isinstance(data, (dict, list)):
        return f"returned {'null' if data is None else _type_name(data)}"
    if isinstance(data, list):
        keys = [f"{i}:{_type_name(v)}" for i, v in enumerate(data[:12])]
        sheet = "no sheetId"
    else:
        keys = [f"{re.sub(r'[^A-Za-z0-9_]', '?', k)[:24]}:{_type_name(v)}" for k, v in list(data.items())[:12]]
        sheet = "no sheetId" if "sheetId" not in data else "same sheetId" if data["sheetId"] == sid else "another sheetId"
    return f"{sheet}; fields {','.join(keys) or 'none'}"


def _lower_colors(colors: dict) -> dict[str, str]:
    """Feishu reports a color in the case it likes (the ones just written come back upper case, old ones lower case); it is the same color."""
    return {option: js.to_str(color).lower() for option, color in colors.items()}


def read_rules(bridge: Bridge, token: str, metadata: list[dict]) -> list[dict]:
    result: list[dict] = []
    for sheet in metadata:
        sid = sheet["sheet_id"]
        grid = sheet.get("grid_properties") or {}
        rows = grid.get("row_count")
        cols = grid.get("column_count")
        need(js.is_int(rows) and js.is_int(cols) and 0 < rows * cols <= 4_000_000, "the rule read cannot be proven complete")
        rows, cols = int(rows), int(cols)
        for r in range(1, rows + 1, 5000):
            for c in range(1, cols + 1, 100):
                rect = f"{column_letter(c)}{r}:{column_letter(min(c + 99, cols))}{min(r + 4999, rows)}"
                data = bridge.api_read(f"/open-apis/sheets/v2/spreadsheets/{token}/dataValidation", {"range": f"{sid}!{rect}", "dataValidationType": "list"})
                # Feishu omits `dataValidations` when the range holds no dropdown (the docs say an empty array), so absent means none. The reply
                # must still be this sheet's own and carry the workbook's revision, so a reply of some other shape never reads as "no rules".
                none = (isinstance(data, dict) and "dataValidations" not in data and js.is_num(data.get("revision"))
                        and isinstance(data.get("spreadsheetToken"), str))
                need(isinstance(data, dict) and data.get("sheetId") == sid and (none or isinstance(data.get("dataValidations"), list)),
                     f"the dropdown rules came back in an unexpected shape ({_shape_of(data, sid)})")
                for rule in ([] if none else data["dataValidations"]):
                    opts = rule.get("options") if rule.get("options") is not None else {}
                    values = rule.get("conditionValues")
                    need(rule.get("dataValidationType") == "list" and isinstance(values, list) and all(isinstance(v, str) for v in values)
                         and len(set(values)) == len(values), "a dropdown source or its options cannot be kept reliably")
                    colors = opts.get("colorValueMap") if opts.get("colorValueMap") is not None else {}
                    need(isinstance(colors, dict) and all(k in values for k in colors.keys()), "unexpected dropdown color map")
                    multiple = opts.get("multipleValues") if opts.get("multipleValues") is not None else False
                    highlight = opts.get("highlightValidData") if opts.get("highlightValidData") is not None else False
                    need(isinstance(multiple, bool) and isinstance(highlight, bool)
                         and all(isinstance(color, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", color) for color in colors.values()), "a dropdown mode or color cannot be kept reliably")
                    ranges: list[str] = []
                    for raw in rule.get("ranges") or []:
                        cut = raw.find("!")
                        need(cut > 0 and raw[:cut] == sid, "a dropdown range spans tabs or cannot be read")
                        ranges.append(str(CellRange(raw[cut + 1:])))
                    need(len(ranges) > 0, "empty dropdown range")
                    result.append({"sheet_id": sid, "ranges": ranges, "type": "list", "options": values, "multiple": multiple,
                                   "highlight": highlight, "colors": _lower_colors(colors)})
    return result


def _export_snapshot(bridge: Bridge, token: str, output: str) -> Snapshot:
    file = os.path.abspath(output)
    need(file.lower().endswith(".xlsx") and not os.path.exists(file), "the snapshot must be a new .xlsx file")
    bridge.export_sheet(token, file)
    need(os.path.isfile(file), "the export was not saved: stopped; it is not submitted again")
    return Snapshot(file)


def _sorted_titles(metadata: list[dict]) -> list[Any]:
    return [s.get("title") for s in sorted(metadata, key=lambda s: js.number(s.get("index")) if js.is_num(s.get("index")) else 0)]


def api_plan(bridge: Bridge, snapshot: Snapshot, plan: dict) -> dict:
    from .rules import dropdowns

    token = target(bridge)
    before = _revision_of(bridge, token)
    metadata = sheets_of(bridge, token)
    need(canonical(_sorted_titles(metadata)) == canonical(snapshot.sheet_names), "the API's list of tabs does not match the XLSX")
    sheet = next((s for s in metadata if s.get("title") == plan["worksheet"]), None)
    need(sheet, "the API's list of tabs does not match the XLSX")
    records = read_rules(bridge, token, metadata)
    need(before == _revision_of(bridge, token), "the spreadsheet changed while it was read: take a new snapshot and plan again")
    return seal({"schema": 1, "bridge_version": BRIDGE_VERSION, "target": TARGET, "batch_plan_id": plan["plan_id"], "baseline_digest": snapshot.fingerprint,
                 "spreadsheet_token": token, "sheet_id": sheet["sheet_id"], "revision": before, "metadata": metadata, "rules": records,
                 "dropdowns": dropdowns(plan, sheet["sheet_id"], records)}, "api_plan_id")


def execute(bridge: Bridge, directory: str, receipt: str) -> dict:
    """The one production write: only after the isolated probe receipt, once per batch, then read back and verified."""
    from . import batch
    from .rules import acceptance, build_writes, rule_projection, rule_proof

    batch_path, manifest = batch.load_batch(directory)
    need(not manifest["plan"].get("adopted"), batch.ADOPTED)
    need(not any(e["step"] == "append_started" for e in batch.events(batch_path)), "a write of this batch was attempted already: it can only be read back (reconcile), never sent again")
    context = read_json(os.path.join(batch_path, "api-plan.json"))
    check_seal(context, "api_plan_id")
    approved = acceptance(receipt, BRIDGE_VERSION)
    bridge.doctor()
    token = target(bridge)
    need(token == context.get("spreadsheet_token") and approved.get("production_token") == token, "the target does not match the overwrite receipt")
    # A folder of this run's own: two appends of one batch at once (a retry while the first is still exporting) must not share one, or the
    # one that loses the append_started race takes the other's export and proof away with it. A killed run's folder is left as it is.
    attempt = tempfile.mkdtemp(prefix=f"api-attempt-{str(len(batch.events(batch_path)) + 1).zfill(6)}-", dir=batch_path)
    started = False
    try:
        need(_revision_of(bridge, token) == context.get("revision"), "the spreadsheet revision changed: plan again")
        fresh = _export_snapshot(bridge, token, os.path.join(attempt, "preflight.xlsx"))
        batch.preflight(batch_path, fresh)
        current_rules = read_rules(bridge, token, sheets_of(bridge, token))
        need(canonical(rule_projection(current_rules)) == canonical(rule_projection(context["rules"])), "the dropdown rules changed: plan again")
        need(_revision_of(bridge, token) == context.get("revision"), "the spreadsheet changed during the checks")
        writes = build_writes(manifest["plan"], context)
        write_json_new(os.path.join(attempt, "writes.json"), writes)
        batch.record(batch_path, "append_started", None, {"transport": "feishu_api", "api_plan_id": context["api_plan_id"], "request_digest": digest(writes)})
        started = True
        bridge.cells_set(token, writes)
        written_revision = _revision_of(bridge, token)  # the export and the rules below must be of this very revision
        fresh = _export_snapshot(bridge, token, os.path.join(attempt, "after.xlsx"))
        post_revision = _revision_of(bridge, token)
        need(post_revision == written_revision, "the spreadsheet changed during the export")
        after_rules = read_rules(bridge, token, sheets_of(bridge, token))
        need(post_revision == _revision_of(bridge, token), "the spreadsheet changed during the verification")
        rule_proof(os.path.join(attempt, "rule-proof.json"), fresh, after_rules, context, manifest["plan"])
        evidence = {"transport": "feishu_api", "proof": os.path.join(attempt, "rule-proof.json")}
        result = batch.verify(batch_path, fresh, evidence)
        need(result["status"] == "complete", "the append did not verify completely; nothing is written to complete or undo it")
        batch.record(batch_path, "append_verified", None, {"transport": "feishu_api", "validations": evidence, "calls": bridge.calls, "revision": post_revision,
                                                           "snapshot": fresh.path, "snapshot_digest": fresh.fingerprint}, fresh)
        report_path = os.path.join(attempt, "verification-report.json")
        write_json_new(report_path, batch.report(batch_path))
        out = {"status": "append_verified", "batch": batch_path, "worksheet": manifest["plan"]["worksheet"], "sheet_id": context["sheet_id"],
               "sheet_url": batch.sheet_url(context["sheet_id"]), "range": manifest["plan"]["range"], "report": report_path, "calls": bridge.calls}
        if result.get("foreign_changes"):
            out["foreign_changes"] = result["foreign_changes"]
        return out
    except Exception as error:
        state = batch.status(batch_path) if started else None
        # Rows taken over meanwhile (gof batch adopt) belong to that batch now: this one records nothing more.
        if state and not state["blocked"] and not state.get("taken_over"):
            batch.record(batch_path, "blocked", None, {"reason": "the Feishu write or its verification failed or has an unknown result; read back first, never retry", "transport": "feishu_api"})
        # A refusal after the send reads much like one before it ("…changed during…"): say plainly that the rows may be in the sheet already.
        if started:
            raise WriteSent(f"the write was sent and the rows may be in the sheet: only gof feishu reconcile may follow, never prepare or append again ({error})", batch_path) from None
        raise
    finally:
        # Nothing was sent: this run's attempt directory holds only its preflight export.
        if not started:
            shutil.rmtree(attempt, ignore_errors=True)


def probe(bridge: Bridge, receipt: str) -> dict:
    """Only after a separately approved online test plan; creates a new isolated workbook."""
    need(not os.path.exists(receipt), "the receipt file exists already and is not overwritten")
    bridge.doctor()
    bridge.check_scopes(["sheets:spreadsheet:create"])
    production = target(bridge)
    stamp = js.iso().replace("-", "").replace(":", "").replace("T", "_").replace(".", "_").replace("Z", "")
    created = bridge.create_workbook(f"GOFCN_API_SAFETY_{stamp}") or {}
    token = created.get("spreadsheet_token")
    need(token and token != production, "the probe workbook's creation has an unknown result: stopped, nothing is created again")
    sid = sheets_of(bridge, token)[0]["sheet_id"]

    def write(rect: str, cells: list) -> Any:
        return bridge.cells_set(token, [{"sheet_id": sid, "range": rect, "cells": cells, "allow_overwrite": False}])

    def read() -> Any:
        reply = bridge.api_read(f"/open-apis/sheets/v2/spreadsheets/{token}/values_batch_get", {"ranges": f"{sid}!A1:B1", "valueRenderOption": "Formula"})
        need(isinstance(reply.get("valueRanges"), list) and len(reply["valueRanges"]) == 1, "the probe read-back has an unexpected shape")
        return reply["valueRanges"][0].get("values", js.UNDEFINED)

    write("A1:B1", [[{"value": "KEEP_EXISTING"}, {"formula": '=IF(1=1,"","")'}]])
    expected = [["KEEP_EXISTING", '=IF(1=1,"","")']]
    need(canonical(read()) == canonical(expected), "the probe's baseline write or formula differs")
    passed: list[str] = []
    observations: list[dict] = []
    for name, rect in (("occupied_value", "A1:A1"), ("empty_formula", "B1:B1"), ("repeated_occupancy", "A1:B1")):
        area = CellRange(rect)
        width = area.max_col - area.min_col + 1
        rejected = False
        before = read()
        try:
            write(rect, [[{"value": "MUST_NOT_REPLACE"} for _ in range(width)]])
        except OccupiedRefusal:
            rejected = True
        after = read()
        need(rejected and canonical(before) == canonical(after) and canonical(after) == canonical(expected), "the overwrite probe failed: appends stay disabled")
        passed.append(name)
        observations.append({"case": name, "rejection": "non_empty", "before": before, "after": after})
    data = seal({"schema": 2, "bridge_version": BRIDGE_VERSION, "identity": "user", "probe_workbook": token, "production_token": production, "passed": passed,
                 "created_at": js.iso(), "observations": observations, "calls": bridge.calls}, "receipt_id")
    write_json_new(receipt, data)
    return {"status": "safety_verified", "receipt": os.path.abspath(receipt), "probe_url": f"https://centurygames.feishu.cn/sheets/{token}", "calls": bridge.calls}


def reconcile(bridge: Bridge, directory: str) -> dict:
    """Re-read an existing batch without sending a write, even after an unknown result."""
    from . import batch
    from .rules import rule_proof

    batch_path, manifest = batch.load_batch(directory)
    need(not manifest["plan"].get("adopted"), batch.ADOPTED)
    context = read_json(os.path.join(batch_path, "api-plan.json"))
    check_seal(context, "api_plan_id")
    bridge.doctor()
    token = target(bridge)
    need(token == context.get("spreadsheet_token") and context.get("bridge_version") == BRIDGE_VERSION, "the read-back target or bridge version differs from the batch's")
    output = os.path.join(batch_path, f"api-readback-{re.sub(r'[-:.]', '', js.iso())}")
    os.mkdir(output)
    before = _revision_of(bridge, token)
    fresh = _export_snapshot(bridge, token, os.path.join(output, "fresh.xlsx"))
    rules = read_rules(bridge, token, sheets_of(bridge, token))
    need(before == _revision_of(bridge, token), "the spreadsheet changed during the read-back: no evidence can be made")
    evidence: Optional[dict] = None
    try:
        rule_proof(os.path.join(output, "rule-proof.json"), fresh, rules, context, manifest["plan"])
        evidence = {"transport": "feishu_api", "proof": os.path.join(output, "rule-proof.json")}
        result = batch.verify(batch_path, fresh, evidence)
    except Blocked as error:
        if evidence:
            raise
        # The new rows do not carry the planned dropdowns, so nothing proves a complete append; the values still tell whether none, part or
        # all of it landed, which is what the user has to be told. (A complete set of values without the dropdowns stays a refusal.)
        try:
            values = batch.verify(batch_path, fresh)
        except Exception:
            raise error from None
        result = {**values, "rule_issue": error.message}
    write_json_new(os.path.join(output, "readback-report.json"), {"verification": result, "revision": before, "snapshot_digest": fresh.fingerprint,
                                                                 "calls": bridge.calls})
    out = {**result, "snapshot": fresh.path}
    if evidence:
        out["validation_evidence"] = evidence
    out["report"] = os.path.join(output, "readback-report.json")
    return out


#: The progress columns whose dropdown may gain an option, by the name `dropdown-add --field` takes (the plan's field names work too).
OPTION_FIELDS: dict[str, str] = {"primary": "primary_tag", "secondary": "secondary_tag", "form": "creative_form", "supplier": "supplier", "reviewer": "creator"}
TAG_COLORS = ["#bacefd", "#d9f5d6", "#fed4a4", "#ece2fe", "#f8def8", "#b1e8fc", "#fbbfbc", "#f8e6ab"]
ROW_CAP = 5000


def add_option(bridge: Bridge, field_name: str, option: str, sheet_name: str, color: Optional[str] = None) -> dict:
    """
    Adds a missing option to the dropdown of a whole column of one sheet: 一级标签, 二级标签, 创意形式, 设计师供应商 or 创意者脚本审核人.
    Only for a value the request needs and the dropdown lacks: the caller (the agent) checks that and puts it in the approved plan. Existing
    options, colors, modes and cell values are not changed.
    """
    from .brief import cell_shown

    field = OPTION_FIELDS.get(field_name) or (field_name if field_name in OPTION_FIELDS.values() else None)
    need(field, "--field is primary, secondary, form, supplier or reviewer")
    bridge.doctor()
    token = target(bridge)
    sheet = next((s for s in sheets_of(bridge, token) if s.get("title") == sheet_name), None)
    need(sheet, f"no tab named {sheet_name}")
    sid = sheet["sheet_id"]
    grid = sheet.get("grid_properties") or {}
    count = js.number(grid.get("column_count", js.UNDEFINED))
    width = int(min(max(count if js.truthy(count) else 26, 1), 100))
    reply = bridge.api_read(f"/open-apis/sheets/v2/spreadsheets/{token}/values_batch_get", {"ranges": f"{sid}!A1:{column_letter(width)}1"}) or {}
    ranges = reply.get("valueRanges") if isinstance(reply, dict) else None
    header = None
    if isinstance(ranges, list) and ranges and isinstance(ranges[0], dict):
        values = ranges[0].get("values")
        header = values[0] if isinstance(values, list) and values else None
    need(isinstance(header, list), "cannot read the header row")
    hits = [i for i, cell in enumerate(header) if header_matches(field, cell_shown(cell))]
    need(len(hits) == 1, f"the header row has no single {FIELD_HEADERS[field]} column")
    column = column_letter(hits[0] + 1)
    chosen = color if color is not None else TAG_COLORS[sum(ord(c) for c in option) % len(TAG_COLORS)]
    rows = js.number(grid.get("row_count", js.UNDEFINED))
    need(js.is_int(rows) and rows >= 2, "cannot read the tab's row count")
    rows = int(rows)
    updated: list[Any] = []
    for first in range(2, rows + 1, ROW_CAP):
        last = min(first + ROW_CAP - 1, rows)
        try:
            updated.append(bridge.dropdown_add({"token": token, "sheet_id": sid, "column": column, "first_row": first, "last_row": last,
                                                "option": option, "color": chosen}))
        except Blocked as error:
            # A span with no dropdown at all, or one that already has the option, is simply not part of this change. The feishu skill says
            # so in its own words, in Chinese or in English.
            if not re.search(r"没有现有下拉|都已包含|no (existing )?dropdown|already (has|have|contain|include)", error.message, re.IGNORECASE):
                raise
    need(len(updated) > 0, "no range needs this option (every dropdown of the column has it already, or the column has none)")
    total = 0
    for u in updated:
        total += js.number((u or {}).get("updated_ranges", js.UNDEFINED) if isinstance(u, dict) else js.UNDEFINED)
    return {"sheet": sheet_name, "field": field, "column": column, "option": option, "updated_ranges": js.as_int(total)}


def take_snapshot(bridge: Bridge, output: str) -> dict:
    bridge.doctor()
    token = target(bridge)
    before = _revision_of(bridge, token)
    snap = _export_snapshot(bridge, token, output)
    need(before == _revision_of(bridge, token), "the revision changed during the export: the snapshot cannot be used")
    return {"snapshot": snap.path, "snapshot_digest": snap.fingerprint, "revision": before}


def _given_names(plan: dict) -> list[str]:
    from .request import items_of

    out: list[str] = []
    for g in plan["groups"]:
        for item in items_of(g["request"]):
            name = item.get("material_name")
            if isinstance(name, str) and name and name not in out:
                out.append(name)
    return out


def same_name_rows(snapshot: Snapshot, plan: dict) -> list[dict]:
    """
    Rows already in the sheet under a name the request gives (files named for their rows: GOFCNxxxxx_<name>): such a file has most likely
    been registered before, and appending it again would give the same material two rows. Only names the request gives are looked for;
    rows named after the tags alone (…_关键词) are alike by design.
    """
    from .creative import lookup_rows

    given = _given_names(plan)
    if not given:
        return []
    by_body = lookup_rows(snapshot, [], given)[1]
    return [{"name": name, "rows": [{"number": r["number"], "name": r["name"], "where": f"{r['sheet']} row {r['row']}"} for r in by_body[name]]}
            for name in given if name in by_body]


def variant_rows(snapshot: Snapshot, plan: dict) -> list[dict]:
    """
    Rows already in the sheet that a name the request gives may be a variant of (named as its start, see variant_bases): with 合并变体名称
    such files belong to that row, which is not appended again; without it, they are a sign the batch has variants to ask about.
    """
    from .creative import lookup_rows, variant_bases

    given = _given_names(plan)
    bases = {name: variant_bases(name) for name in given}
    by_body = lookup_rows(snapshot, [], [b for v in bases.values() for b in v])[1]
    out = []
    for name in given:
        rows = [r for base in bases[name] for r in by_body.get(base, [])]
        if rows:
            out.append({"name": name, "rows": [{"number": r["number"], "name": r["name"], "where": f"{r['sheet']} row {r['row']}"} for r in rows]})
    return out


def plan_with_api(bridge: Bridge, snapshot: Snapshot, request_input: Any) -> dict:
    from .plan import make_plan

    bridge.doctor()
    offline = make_plan(snapshot, request_input)
    same = same_name_rows(snapshot, offline)
    variants = variant_rows(snapshot, offline)
    result = {"batch_plan": offline, "api_plan": api_plan(bridge, snapshot, offline)}
    if same:
        result["same_name_rows"] = same
    if variants:
        result["variant_rows"] = variants
    return result


def recompute_api_plan(bridge: Bridge, snapshot: Snapshot, approved: dict) -> dict:
    bridge.doctor()
    recomputed = api_plan(bridge, snapshot, approved["batch_plan"])
    need(canonical(recomputed) == canonical(approved.get("api_plan")), "the API target, revision or rules differ from the approved plan")
    return recomputed


def recompute_plan(bridge: Bridge, snapshot: Snapshot, approved: Any) -> dict:
    """
    The approved plan made again on a fresh snapshot, for the append. The rows, their numbers, values and names and the groups must be
    exactly the approved ones, or the user has to approve again; the revision and the rules are taken as they are now, since the approved
    dropdown additions (and anybody's edits elsewhere in the workbook) change them without changing what is written.
    """
    from .plan import business, make_plan

    plan = approved.get("batch_plan") if isinstance(approved, dict) else None
    need(isinstance(plan, dict) and isinstance(approved.get("api_plan"), dict), "the approved plan is incomplete")
    unsealed = {k: v for k, v in plan.items() if k != "plan_id"}
    need(digest(unsealed)[:24] == plan.get("plan_id"), "the approved plan was modified")
    check_seal(approved["api_plan"], "api_plan_id")
    bridge.doctor()
    fresh = make_plan(snapshot, plan.get("request"), plan.get("current_sheet"))
    need(canonical(business(fresh)) == canonical(business(plan)), "the new rows, their numbers or names differ from the approved plan (the sheet changed since): plan again and have it approved again")
    api = api_plan(bridge, snapshot, fresh)
    need(api["spreadsheet_token"] == approved["api_plan"].get("spreadsheet_token") and api["sheet_id"] == approved["api_plan"].get("sheet_id"), "the API target differs from the approved plan")
    return {"batch_plan": fresh, "api_plan": api}


OPTION_NAMES = {field: name for name, field in OPTION_FIELDS.items()}


def plan_summary(result: dict, file: str) -> dict:
    """The few lines of a plan the agent shows the user: sheet, numbers, what each group's rows are called, and the dropdown options to add."""
    from .batch import kind_of
    from .cls import requirement_name
    from .plan import supplier_cell
    from .request import DEFAULT_KEYWORD, items_of

    plan = result["batch_plan"]
    api = result["api_plan"]
    groups = []
    for g in plan["groups"]:
        offset = 0
        items = []
        for item in items_of(g["request"]):
            names = g["names"][offset:offset + item["quantity"]]
            offset += item["quantity"]
            items.append({"quantity": item["quantity"], "first": names[0] if names else js.UNDEFINED, "last": names[-1] if names else js.UNDEFINED})
        # Rows whose 提炼词 is the default: the document named none for them, which the user is asked to confirm.
        defaults = sum(item["quantity"] for item in items_of(g["request"]) if item.get("keyword") == DEFAULT_KEYWORD)
        # `message_kind` is how the group message will name this requirement ("素人剧情*25"), shown in the plan so it can be corrected before.
        # A group without a CLS ticket (rows only, e.g. for finished materials) has neither a requirement name nor a group message.
        ticket = len(g["request"].get("cls") or {}) > 0
        entry: dict[str, Any] = {"group": g["id"], "quantity": g["request"]["quantity"]}
        if ticket:
            entry["requirement_name"] = requirement_name(g["names"])
        entry["supplier"] = g["request"].get("supplier") or None
        entry["reviewer"] = g["request"]["reviewer"]
        # The supplier is the reviewer: the sheet's 设计师供应商 cell stays empty (supplier_cell).
        if g["request"].get("supplier") != "" and supplier_cell(g["request"]) == "":
            entry["supplier_cell_empty"] = True
        if g["request"].get("script_url"):
            entry["script_url"] = g["request"]["script_url"]
        if ticket:
            entry["message_kind"] = kind_of(g["request"])
        entry["items"] = items
        if defaults:
            entry["default_keyword_rows"] = defaults
        groups.append(entry)
    summary: dict[str, Any] = {
        "status": "plan_saved", "plan": file, "worksheet": plan["worksheet"], "sheet_id": api["sheet_id"], "range": plan["range"], "quantity": plan["quantity"],
        "first_number": plan["first_number"], "last_number": plan["last_number"], "naming": plan.get("naming", js.UNDEFINED), "groups": groups,
        # Read from the rules through the API: the options the XLSX export lists for a column are not reliable.
        "new_options": [{"field": OPTION_NAMES.get(d["field"], d["field"]), "option": option} for d in api["dropdowns"] for option in d["missing"]],
        "history_warnings": len(plan["history_warnings"]),
    }
    if result.get("same_name_rows"):
        summary["same_name_rows"] = result["same_name_rows"]
    if result.get("variant_rows"):
        summary["variant_rows"] = result["variant_rows"]
    return summary
