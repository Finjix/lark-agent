"""Batches: the approved plan on disk, its immutable event chain, verification of an append, the CSV, the group message."""

from __future__ import annotations

import json
import os
import re
import shutil
from typing import Any, Optional

from . import js
from .a1 import CellRange, column_letter, coord_of
from .plan import csv_name, make_plan
from .request import NO_SCRIPT, items_of, normalize_request
from .seal import check_seal, reject_secrets
from .snapshot import Snapshot, collect_ids, has_content, holds, inline_options, plain_rules, scan_sheets, validation_projection
from .util import (Blocked, CSV_HEADERS, FIXED_TAG, TARGET, canonical, check_cls_url, check_target, digest, is_inside, is_object, need, read_json,
                   sha256, write_json_new, write_text_new)

BOM = "\ufeff"


def default_batch_root() -> str:
    """Where batches go unless told otherwise: the served user's own directory (see paths.py)."""
    from .paths import resolve_data_paths

    return resolve_data_paths().batches


def _sha_file(file: str) -> str:
    with open(file, "rb") as handle:
        return sha256(handle.read())


def load_batch(directory: str) -> tuple[str, dict]:
    batch_path = os.path.abspath(directory)
    manifest = read_json(os.path.join(batch_path, "manifest.json"))
    plan = manifest["plan"]
    token = plan.get("plan_id")
    unsealed = {k: v for k, v in plan.items() if k != "plan_id"}
    # The directory is named after the plan; the same plan prepared again (its first batch spent or out of date) gets "-2", "-3" ...
    named = os.path.basename(batch_path)
    need(digest(unsealed)[:24] == token and (named == token or re.fullmatch(re.escape(js.to_str(token)) + r"-[1-9][0-9]*", named)),
         "the batch manifest is damaged or its directory does not match its plan")
    need(manifest.get("baseline_sha256") == _sha_file(os.path.join(batch_path, "baseline.xlsx")), "the baseline file has changed")
    for name, expected in manifest["artifacts"].items():
        need(os.path.basename(name) == name and _sha_file(os.path.join(batch_path, name)) == expected, f"a batch file has changed: {name}")
    return batch_path, manifest


def _csv_field(value: str) -> str:
    return '"' + value.replace('"', '""') + '"' if re.search(r'[",\r\n]', value) else value


def csv_text(names: list[str], script_url: str) -> str:
    """The CSV CLS imports a group's scripts from: one line per row, the script link (or 无) on the first and 同上 below it, the fixed tag."""
    lines = [",".join(_csv_field(h) for h in CSV_HEADERS)]
    for index, name in enumerate(names):
        lines.append(",".join(_csv_field(v) for v in [name, script_url if index == 0 else "同上", FIXED_TAG]))
    return BOM + "\r\n".join(lines) + "\r\n"


def clipboard_tsv(plan: dict) -> str:
    area = CellRange(plan["range"])
    lines: list[str] = []
    for row in plan["rows"]:
        cells: list[str] = []
        for col in range(area.min_col, area.max_col + 1):
            raw = row["values"].get(coord_of(col, row["row"]))
            value = "" if raw is None else js.to_str(raw)
            need(not re.search(r"[\t\r\n]", value), "a clipboard cell contains a separator: no raw TSV can be made")
            cells.append(value)
        lines.append("\t".join(cells))
    return "\r\n".join(lines) + "\r\n"


def _requirement_keys(plan: dict) -> list[str]:
    """
    What tells a requirement apart across batches: each group's script link, or for rows without one (finished materials, reserved
    numbers, a requirement without a document), the names of its rows without their numbers.
    """
    keys: list[str] = []
    for g in plan["groups"]:
        url = g["request"].get("script_url")
        if url and url != NO_SCRIPT:
            keys.append(f"link:{url}")
        else:
            keys.extend(f"name:{re.sub(r'^GOFCN[0-9]+_', '', name, count=1)}" for name in g["names"])
    return keys


def _landed_unverified(base: str, plan: dict, snapshot: Snapshot) -> list[str]:
    """
    Earlier batches under `base` for the same requirement (see _requirement_keys) whose write went out without being verified, and whose
    rows are in the sheet now (a planned number at the row it was written to, or carrying the planned name). Planning that requirement
    again would append it twice: what happened to the earlier batch is to be read back there. One whose rows are nowhere is no obstacle.
    """
    keys = set(_requirement_keys(plan))
    present: Optional[tuple[dict, dict]] = None
    found: list[str] = []
    for name in os.listdir(base):
        directory = os.path.join(base, name)
        try:
            earlier = read_json(os.path.join(directory, "manifest.json"))["plan"]
            steps = [e["step"] for e in events(directory)]
            earlier_keys = _requirement_keys(earlier)
        except Exception:  # not a batch
            continue
        if not any(key in keys for key in earlier_keys) or "append_started" not in steps or "append_verified" in steps:
            continue
        if present is None:
            tables = scan_sheets(snapshot)[0]
            present = (tables, collect_ids(snapshot, tables))
        tables, ids = present

        def landed(r: dict) -> bool:
            at = ids.get(r.get("number"))
            if not at:
                return False
            cell = snapshot.cell(at[0], at[1], tables[at[0]]["name"])
            shown = cell.value if cell is not None else None
            return (at[0] == earlier.get("worksheet") and at[1] == r.get("row")) or shown == r.get("name")

        if any(landed(r) for r in earlier.get("rows", [])):
            found.append(directory)
    return found


def _new_batch_dir(base: str, plan_id: str) -> str:
    directory = os.path.join(base, plan_id)
    n = 2
    while os.path.exists(directory):
        directory = os.path.join(base, f"{plan_id}-{n}")
        n += 1
    return directory


def _copy_new(source: str, target: str) -> None:
    """``fs.copyFileSync(source, target, COPYFILE_EXCL)``."""
    with open(source, "rb") as src, open(target, "xb") as dst:
        shutil.copyfileobj(src, dst)


def prepare(plan: dict, snapshot: Snapshot, api_plan: Optional[dict] = None, output_root: Optional[str] = None) -> dict:
    need(canonical(plan) == canonical(make_plan(snapshot, plan.get("request"), plan.get("current_sheet"))), "the snapshot differs from the approved plan: plan again and have it approved again")
    if api_plan:
        check_seal(api_plan, "api_plan_id")
        check_target(api_plan.get("target") if api_plan.get("target") is not None else "")
        need(api_plan.get("batch_plan_id") == plan["plan_id"] and api_plan.get("baseline_digest") == snapshot.fingerprint, "the API plan does not match this batch's snapshot")
    tsv = clipboard_tsv(plan)
    base = os.path.abspath(output_root) if output_root is not None else default_batch_root()
    os.makedirs(base, exist_ok=True)
    earlier = _landed_unverified(base, plan, snapshot)
    need(len(earlier) == 0, f"an earlier batch of this request was sent and never verified, and the sheet has its rows ({', '.join(earlier)}): "
         "run gof feishu reconcile on that batch and act on its result; it cannot be appended again")
    # A batch is never reused or rewritten: the same plan made again (the workbook's revision moved with nothing visible in the export, or a
    # write that landed nowhere) starts a batch of its own beside the earlier one, which keeps its record.
    directory = _new_batch_dir(base, plan["plan_id"])
    os.mkdir(directory)
    try:
        # No in-place rewrite or rollback of a partially generated batch.
        _copy_new(snapshot.path, os.path.join(directory, "baseline.xlsx"))
        # Browser clipboard TSV is not CSV: quoting formulas makes them literal text.
        write_text_new(os.path.join(directory, "append.tsv"), tsv)
        # Groups of rows appended without a script link have no CSV (see make_plan).
        scripted = [g for g in plan["groups"] if g.get("csv")]
        for group in scripted:
            write_text_new(os.path.join(directory, group["csv"]), csv_text(group["names"], group["request"]["script_url"]))
        files = ["append.tsv", *[g["csv"] for g in scripted]]
        if api_plan:
            write_json_new(os.path.join(directory, "api-plan.json"), api_plan)
            files.append("api-plan.json")
        artifacts = {name: _sha_file(os.path.join(directory, name)) for name in files}
        write_json_new(os.path.join(directory, "manifest.json"),
                       {"plan": plan, "baseline_sha256": _sha_file(os.path.join(directory, "baseline.xlsx")), "artifacts": artifacts})
        os.mkdir(os.path.join(directory, "events"))
        return {"status": "prepared", "batch": directory, "range": plan["range"], "csv": [g["csv"] for g in scripted]}
    except BaseException:
        # This call made the directory a moment ago and nothing has been recorded in it: a half-made batch would block the plan for good.
        shutil.rmtree(directory, ignore_errors=True)
        raise


#: Why a batch made by `adopt` has nothing to append, verify or read back.
ADOPTED = "this batch took over rows already in the progress sheet (gof batch adopt): it appended nothing and has no write to check"


def taken_over(by: Any) -> str:
    """Why a batch whose rows `adopt` took over goes no further."""
    return f"this batch's rows were taken over by batch {js.to_str(by)} (gof batch adopt): continue with that one"


def adopt(snapshot: Snapshot, numbers: list[int], supplier: Optional[str] = None, reviewer: Optional[str] = None,
          output_root: Optional[str] = None) -> dict:
    """
    A batch for rows that are in the progress sheet already but in no batch of this user: registered by hand or by a colleague, or their
    batch is gone (an interrupted conversation, removed after 7 days). Nothing is written to the sheet. The rows are taken as the sheet has
    them (names, tags, supplier, reviewer), recorded as `rows_adopted`, and the batch is then like one whose rows were appended without a
    ticket: `attach_ticket` gives it one. Refused only where the ticket would be unclear: a number the sheet lacks, numbers that are not one
    run (the ticket is named GOFCN<first>-GOFCN<last>), one number on rows of different names, rows of different suppliers or reviewers
    (`supplier` / `reviewer` decide then), rows without a 一级/二级标签 in their cells or their name, numbers another batch already holds.

    A batch whose write went out but never verified holds its numbers too, so that nothing is appended or ticketed twice on a guess. Its
    rows can still be stuck there for good: someone filling a cell of the new rows that the skill left empty makes every verification a
    conflict. Such rows are taken over once every cell that batch wrote into them is in the sheet exactly as written, and that batch is
    closed (`taken_over`). Its verification stays as strict as it is: the takeover is the user's explicit step, with the rows as the sheet
    has them.
    """
    from .creative import lookup_rows, name_body

    wanted = sorted(set(numbers))
    need(len(wanted) > 0, "no numbers given")
    base = os.path.abspath(output_root) if output_root is not None else default_batch_root()
    os.makedirs(base, exist_ok=True)
    # A batch holds its numbers once its rows went out or were taken over; one only prepared holds nothing yet, nor does one a takeover closed.
    closing: list[dict] = []
    for name in os.listdir(base):
        earlier = os.path.join(base, name)
        try:
            plan = load_batch(earlier)[1]["plan"]
            steps = [e["step"] for e in events(earlier)]
        except Exception:
            continue
        if not any(s in ("append_started", "append_verified", "rows_adopted") for s in steps) or "taken_over" in steps:
            continue
        held = [r for r in plan["rows"] if r.get("number") in wanted]
        if not held:
            continue
        first = held[0]["number"]
        need("append_verified" not in steps and "rows_adopted" not in steps, f"GOFCN{first} is in batch {earlier} already: continue with that one (gof batch list --number {first})")
        ws = snapshot.sheet(plan["worksheet"]) if plan.get("worksheet") in snapshot.sheet_names else None
        differ = [coord for r in held for coord, value in (r.get("values") or {}).items()
                  if value is not None and not holds(ws.cells.get(coord) if ws else None, value)]
        need(len(differ) == 0, f"GOFCN{first} is in batch {earlier}, whose write was sent and never verified, and {', '.join(differ[:5])} in the sheet differ from what it wrote: run gof feishu reconcile on that batch and act on its result")
        closing.append({"dir": earlier, "numbers": [r["number"] for r in held]})
    by_number = lookup_rows(snapshot, wanted)[0]
    missing = [n for n in wanted if n not in by_number]
    need(len(missing) == 0, f"the progress sheet has no rows with these numbers: {', '.join(f'GOFCN{n}' for n in missing)}")
    cut = next((i for i, n in enumerate(wanted) if i > 0 and n != wanted[i - 1] + 1), -1)
    if cut >= 0:
        raise Blocked(f"the numbers must be one run: GOFCN{wanted[cut - 1]} is followed by GOFCN{wanted[cut]}. A ticket is named GOFCN<first>-GOFCN<last>, so take over each run of numbers on its own")
    rows = []
    for n in wanted:
        found = by_number[n]
        where = ", ".join(f"{r['sheet']} row {r['row']}" for r in found)
        need(len({canonical(r["name"]) for r in found}) == 1, f"GOFCN{n} is on {len(found)} rows of the progress sheet with different 素材名称 ({where})")
        need(found[0]["name"], f"GOFCN{n} ({where}) has no 素材名称")
        rows.append(found[0])

    def one(field: str, label: str, given: Optional[str]) -> str:
        if given is not None and js.trim(given) != "":
            return js.trim(given)
        values: list[str] = []
        for r in rows:
            if r[field] not in values:
                values.append(r[field])
        need(len(values) == 1, f"the rows' {label} differ ({', '.join(v or 'empty' for v in values)}): give the ticket's {label} with --{field}")
        need(values[0] != "", f"the rows have no {label}: give it with --{field}")
        return values[0]

    supplier_name = one("supplier", "设计师供应商", supplier)
    reviewer_name = one("reviewer", "创意者脚本审核人", reviewer)
    # The tags and form only name the requirement in the group message and its CSV; a row typed by hand without them has them in its name.
    items = []
    for r in rows:
        body = name_body(r["name"])
        parts = body.split("_") if body is not None else []
        item = {"quantity": 1, "primary_tag": r["primary_tag"] or (parts[0] if len(parts) > 0 else "") or "",
                "secondary_tag": r["secondary_tag"] or (parts[1] if len(parts) > 1 else "") or "", "creative_form": r["creative_form"]}
        if r["keyword"]:
            item["keyword"] = r["keyword"]
        items.append(item)
    # The rows have their names already: the rules for making a new one (no _ in a tag, no / in a 提炼词 …) are not theirs to meet.
    request = normalize_request({"groups": [{"supplier": supplier_name, "reviewer": reviewer_name, "items": items}]}, adopted=True)
    names = [r["name"] for r in rows]
    plan = {
        "schema": 1, "target": TARGET, "adopted": True, "worksheet": rows[0]["sheet"], "current_sheet": None, "request": request,
        "baseline_digest": snapshot.fingerprint, "range": None, "quantity": len(rows), "first_number": wanted[0], "last_number": wanted[-1],
        "rows": [{"sheet": r["sheet"], "row": r["row"], "number": r["number"], "name": r["name"], "group": "01"} for r in rows],
        "groups": [{"id": "01", "csv": None, "names": names, "rows": [r["row"] for r in rows], "request": request["groups"][0]}],
    }
    plan["plan_id"] = digest(plan)[:24]
    directory = _new_batch_dir(base, plan["plan_id"])
    os.mkdir(directory)
    try:
        # The batches the rows are taken from close first: should anything below fail, the rows are free to be taken over again, never held twice.
        for earlier in closing:
            record(earlier["dir"], "taken_over", None, {"by": directory, "numbers": earlier["numbers"]})
        # The export the rows were taken from, kept as the batch's baseline.
        _copy_new(snapshot.path, os.path.join(directory, "baseline.xlsx"))
        write_json_new(os.path.join(directory, "manifest.json"),
                       {"plan": plan, "baseline_sha256": _sha_file(os.path.join(directory, "baseline.xlsx")), "artifacts": {}})
        os.mkdir(os.path.join(directory, "events"))
        record(directory, "rows_adopted", None, {"snapshot_digest": snapshot.fingerprint})
    except BaseException:
        shutil.rmtree(directory, ignore_errors=True)
        raise
    result = {"status": "rows_adopted", "batch": directory, "worksheet": plan["worksheet"], "quantity": plan["quantity"],
              "first_number": plan["first_number"], "last_number": plan["last_number"], "first": names[0], "last": names[-1],
              "supplier": supplier_name, "reviewer": reviewer_name}
    if closing:
        result["taken_over"] = [c["dir"] for c in closing]
    return result


def events(directory: str) -> list[dict]:
    folder = os.path.join(directory, "events")
    files = sorted((f for f in os.listdir(folder) if f.endswith(".json")), key=js.utf16_key)
    records: list[dict] = []
    previous = ""
    for offset, file in enumerate(files):
        need(file == f"{str(offset + 1).zfill(6)}.json", "a batch event is missing or out of order")
        event = read_json(os.path.join(folder, file))
        token = event.get("event_id")
        rest = {k: v for k, v in event.items() if k != "event_id"}
        need(rest.get("previous") == previous, "the batch's event chain is broken")
        need(digest(rest) == token, "a batch event was modified")
        records.append({**rest, "event_id": token})
        previous = token
    return records


def status(directory: str) -> dict:
    batch_path, manifest = load_batch(directory)
    records = events(batch_path)
    blocked = False
    for event in records:
        if event["step"] in ("blocked", "resumed"):
            blocked = event["step"] == "blocked"
    groups = {g["id"]: [e["step"] for e in records if e.get("group") == g["id"]] for g in manifest["plan"]["groups"]}
    # Rows taken over from the sheet (adopt) are there already: for the ticket and CLS they count as appended and verified.
    over = next((e for e in records if e["step"] == "taken_over"), None)
    result = {"status": "recorded", "plan_id": manifest["plan"]["plan_id"], "events": records,
              "append_verified": any(e["step"] in ("append_verified", "rows_adopted") for e in records), "blocked": blocked, "groups": groups}
    if manifest["plan"].get("adopted"):
        result["adopted"] = True
    if over:
        result["taken_over"] = over["evidence"].get("by")
    return result


def _filter_extends_only_into_append(before: Optional[dict], after: Optional[dict], area: CellRange) -> bool:
    if not (before and before.get("ref")) or not (after and after.get("ref")):
        return False
    try:
        old_range = CellRange(before["ref"])
        new_range = CellRange(after["ref"])
    except Blocked:
        return False
    return (old_range.min_col == new_range.min_col and old_range.max_col == new_range.max_col and old_range.min_row == new_range.min_row
            and old_range.max_row < new_range.max_row and old_range.max_row + 1 >= area.min_row and new_range.max_row == area.max_row
            and before.get("rest") == after.get("rest"))


def _ui_validates_omitted_dropdown(spec: dict, evidence: Any, fresh: Snapshot, sheet: str, value: Optional[str]) -> bool:
    if not isinstance(evidence, list):
        return False
    matches = [item for item in evidence if isinstance(item, dict) and item.get("worksheet") == sheet and item.get("range") == spec["range"]]
    if len(matches) != 1:
        return False
    item = matches[0]
    options = item.get("options")
    if not (item.get("confirmed") is True and item.get("saved") is True and item.get("source") == "custom" and item.get("reason") == "xlsx_omits_inline_rule"
            and item.get("snapshot_digest") == fresh.fingerprint and item.get("mode") in ("single", "multiple")
            and isinstance(options, list) and all(isinstance(o, str) and o != "" for o in options)
            and value is not None and value in options and all(o in options for o in spec["options"])):
        return False
    try:
        check_target(item.get("url") if item.get("url") is not None else "")
        screenshot = js.to_str(item["screenshot"]) if item.get("screenshot") is not None else ""
        return os.path.isabs(screenshot) and os.path.isfile(screenshot)
    except Blocked:
        return False


def _row_col(coord: str) -> tuple[int, int, str]:
    m = re.fullmatch(r"([A-Z]+)([0-9]+)", coord)
    if not m:
        return (0, 0, coord)
    return int(m.group(2)), len(m.group(1)), m.group(1)


def _by_position(coord: str) -> tuple:
    row, length, letters = _row_col(coord)
    return (row, length, js.utf16_key(letters))


def verify(directory: str, fresh: Snapshot, validation_evidence: Any = None) -> dict:
    from .rules import validate_rule_evidence

    batch_path, manifest = load_batch(directory)
    plan = manifest["plan"]
    need(not plan.get("adopted"), ADOPTED)
    api_dropdowns: Optional[list[dict]] = None
    evidence_is_api = isinstance(validation_evidence, dict) and validation_evidence.get("transport") == "feishu_api"
    # API rules are checked even when the XLSX happens to include validations.
    # A pre-write or zero-applied snapshot can still be inspected without post-write proof.
    if "api-plan.json" in manifest["artifacts"] and evidence_is_api:
        api_dropdowns = validate_rule_evidence(batch_path, fresh, validation_evidence)
    baseline = Snapshot(os.path.join(batch_path, "baseline.xlsx"))
    need(baseline.fingerprint == plan["baseline_digest"], "the baseline's digest does not match the plan")
    sheet = plan["worksheet"]
    area = CellRange(plan["range"])
    issues: list[str] = []
    # An API batch sends nothing but its own ranges (allow_overwrite:false), so a difference outside the appended rows is somebody else's edit
    # of the shared workbook made meanwhile: it is reported (foreign_changes) and does not fail the append, or every busy day would leave
    # appended rows behind a batch that can never be verified. A batch written any other way (a paste in the browser) keeps the strict check.
    strict = "api-plan.json" not in manifest["artifacts"]
    foreign: list[str] = []

    def elsewhere(text: str) -> None:
        if strict:
            issues.append(text)
        elif len(foreign) < 20:
            foreign.append(text)

    def in_rows(row: int) -> bool:
        return area.min_row <= row <= area.max_row

    if canonical(fresh.sheet_names) != canonical(baseline.sheet_names):
        elsewhere("the list or order of tabs changed")
    expected: dict[str, Any] = {}
    for row in plan["rows"]:
        expected.update(row["values"])

    def owned(name: str, coord: str) -> bool:
        return name == sheet and area.contains(coord)

    sheet_names = list(dict.fromkeys([*baseline.cells.keys(), *fresh.cells.keys()]))
    done = False
    for name in sheet_names:
        before = baseline.cells.get(name, {})
        after = fresh.cells.get(name, {})
        coords = sorted(set(before) | set(after), key=_by_position)
        for coord in coords:
            if not owned(name, coord) and canonical(before.get(coord)) != canonical(after.get(coord)):
                # The appended rows themselves, beyond the written columns as well: someone writing into them is a collision, not a neighbour's edit.
                if name == sheet and in_rows(_row_col(coord)[0]):
                    issues.append(f"unplanned change: {name}!{coord}")
                else:
                    elsewhere(f"unplanned change: {name}!{coord}")
                if len(issues) >= 20:
                    done = True
                    break
        if done:
            break
    before_meta = json.loads(js.stringify(baseline.meta))
    after_meta = json.loads(js.stringify(fresh.meta))
    for data in (before_meta, after_meta):
        if data.get(sheet):
            data[sheet]["rows"] = {r: v for r, v in data[sheet]["rows"].items() if not (area.min_row <= js.number(r) <= area.max_row)}
    if before_meta.get(sheet) and after_meta.get(sheet) and _filter_extends_only_into_append(before_meta[sheet]["filter"], after_meta[sheet]["filter"], area):
        after_meta[sheet]["filter"] = before_meta[sheet]["filter"]
    if strict:
        if canonical(before_meta) != canonical(after_meta):
            issues.append("the existing layout, merges, hidden rows, protection or filter changed")
    else:
        for name in dict.fromkeys([*before_meta.keys(), *after_meta.keys()]):
            b = before_meta.get(name)
            a = after_meta.get(name)
            if canonical(b) == canonical(a):
                continue

            # On the appended sheet a protection change, or a merge that reaches the appended rows, concerns them; filters, sizes and hidden
            # rows elsewhere do not.
            def reaching(meta: dict) -> list[str]:
                out = []
                for m in meta["merges"]:
                    r = CellRange(m)
                    if r.max_row >= area.min_row and r.min_row <= area.max_row:
                        out.append(m)
                return out

            if name == sheet and b and a and (canonical(b.get("protection")) != canonical(a.get("protection")) or canonical(reaching(b)) != canonical(reaching(a))):
                issues.append("the existing layout, merges, hidden rows, protection or filter changed")
            else:
                elsewhere(f"the existing layout, merges, hidden rows or filter changed: {name}")
    if canonical(validation_projection(baseline, (sheet, area))) != canonical(validation_projection(fresh, (sheet, area))):
        elsewhere("dropdown rules outside the appended range changed")
    need(sheet in fresh.sheet_names, "the target tab no longer exists")
    ws = fresh.sheet(sheet)
    matches = 0
    blanks = 0
    meaningful = 0
    for coord, value in expected.items():
        actual = ws.cells.get(coord)
        present = actual is not None and actual.value is not None
        if value is not None:
            meaningful += 1
            if holds(actual, value):
                matches += 1
            elif not present:
                blanks += 1
            else:
                issues.append(f"appended value differs: {coord}")
        elif present:
            issues.append(f"a new cell that must stay empty is filled: {coord}")
    state = "conflict" if issues else "not_applied" if matches == 0 else "partial" if blanks else "complete"
    ui_validated: list[str] = []
    if state == "complete":
        need("api-plan.json" not in manifest["artifacts"] or api_dropdowns is not None, "an API append needs the full API read-back of the rules of this snapshot")
        rules = plain_rules(ws)
        for dropdown in plan["dropdowns"]:
            r = CellRange(dropdown["range"])
            # A batch written through the API wrote the options of the API's rule (rules.py dropdowns), and validate_rule_evidence has read the
            # new rows' rules back through the API and found them to be exactly those: the cell must hold one of them. The lists the XLSX
            # export shows are not what was written (it can show a stray rule of a few old cells for the whole column), so they only judge
            # other batches.
            spec = next((d for d in api_dropdowns if d["range"] == dropdown["range"]), None) if api_dropdowns is not None else None
            for row in range(r.min_row, r.max_row + 1):
                coord = f"{column_letter(r.min_col)}{row}"
                cell = ws.cells.get(coord)
                value = cell.value if cell is not None else None
                if api_dropdowns is not None:
                    if spec and value is not None and value in spec["options"]:
                        continue
                    issues.append(f"a new row's dropdown lacks its value or the existing options: {coord}")
                    break
                matching = [v for v in rules if any(rng.contains(coord) for rng in v.ranges)]
                if not matching and _ui_validates_omitted_dropdown(dropdown, validation_evidence, fresh, sheet, value):
                    if dropdown["range"] not in ui_validated:
                        ui_validated.append(dropdown["range"])
                    continue

                def accepts(v: Any) -> bool:
                    options = inline_options(v)
                    return value is not None and value in options and all(o in options for o in dropdown["options"])

                if not matching or not any(accepts(v) for v in matching):
                    issues.append(f"a new row's dropdown lacks its value or the existing options: {coord}")
                    break
        if issues:
            state = "conflict"
    result: dict[str, Any] = {"status": state, "matched_cells": matches, "expected_cells": meaningful, "issues": issues[:20]}
    if foreign:
        result["foreign_changes"] = foreign
    if ui_validated:
        result["ui_validated_ranges"] = ui_validated
    if api_dropdowns is not None:
        result["api_validated_ranges"] = [d["range"] for d in api_dropdowns]
    return result


def can_start_append(records: list[dict]) -> bool:
    if all(e["step"] in ("blocked", "resumed") for e in records):
        return True
    # A user-authorized retry is consumed by the next append_started event.
    last = records[-1] if records else None
    return (len(records) > 0 and last["step"] == "resumed" and last["evidence"].get("retry_append") is True
            and last["evidence"].get("sync_confirmed") is True and all(e["step"] in ("append_started", "blocked", "resumed") for e in records))


def preflight(directory: str, fresh: Snapshot) -> dict:
    batch_path, manifest = load_batch(directory)
    records = events(batch_path)
    need(not status(batch_path)["blocked"] and can_start_append(records), "this batch has an attempt or a block already: check what is there first; it is never pasted again automatically")
    need(fresh.fingerprint == manifest["plan"]["baseline_digest"], "the snapshot changed or the target is occupied: plan again and have it approved")
    area = CellRange(manifest["plan"]["range"])
    ws = fresh.sheet(manifest["plan"]["worksheet"])
    for cell in ws.cells.values():
        if area.min_row <= cell.row <= area.max_row:
            need(not has_content(cell), "a target row holds a value, a formula or attachment data")
    return {"status": "ready_to_append", "range": str(area)}


def group_for(plan: dict, group_id: Optional[str]) -> dict:
    found = [g for g in plan["groups"] if g["id"] == group_id]
    need(len(found) == 1, "no such group")
    return found[0]


def cls_check(directory: str, group_id: str, action: str, ui: dict) -> dict:
    need(action in ("fill", "save_draft", "import_scripts"), "publishing, deleting, updating scripts and unknown actions are not allowed")
    state = status(directory)
    need(state["append_verified"] and not state["blocked"], "the progress-sheet rows are not verified yet, or the batch is blocked")
    need(group_id in state["groups"], "no such group")
    stages = state["groups"][group_id]
    attempted = [e for e in state["events"] if e.get("group") == group_id and e["step"] == "cls_api_attempt" and e["evidence"].get("action") == action]
    need(len(attempted) == 0, "this group's action was attempted already: read back; it is never sent again automatically")
    need("cls_started" in stages and "handoff" not in stages, "record cls_started first; a group handed off does not continue automatically")
    ui = ui if isinstance(ui, dict) else {}
    need(ui.get("logged_in") is True, "CLS is not logged in")
    need(ui.get("can_create") is True, "no proof that the CLS account may create requirements")
    need(ui.get("unpublished") is True, "no proof that this is a new, unpublished requirement or draft")
    need(ui.get("save_result") not in ("failure", "unknown"), "the draft save failed or its result is unknown: check what is there first")
    if action == "save_draft":
        need("draft_saved" not in stages, "a draft exists already: it is not saved again as a new ticket")
        need(ui.get("required_complete") is True, "CLS required fields are incomplete")
    if action == "import_scripts":
        need("draft_saved" in stages and "scripts_imported" not in stages, "no draft saved, or this group's scripts were imported already: not repeated")
        need(ui.get("draft_saved") is True and ui.get("unpublished") is True, "needs proof of the unpublished draft")
        need(ui.get("import_enabled") is True and ui.get("import_would_publish") is False, "the script import is disabled, would publish, or has an unknown effect: keep the records and report what blocks it")
    return {"status": "allowed", "action": action, "group": group_id}


def _cls_evidence(evidence: dict, api_batch: bool) -> None:
    from .cls import validate_receipt

    need(evidence.get("confirmed") is True and evidence.get("unpublished") is True, "no confirmation of success and of the unpublished state")
    check_cls_url(evidence.get("url") if evidence.get("url") is not None else "")
    # A batch made through the bridge (it has an api-plan) only accepts receipts of the CLS API: a screenshot path proves nothing about CLS.
    need(not api_batch or evidence.get("transport") == "cls_api", "the CLS evidence of an API batch must be a receipt with transport=cls_api")
    if evidence.get("transport") == "cls_api":
        validate_receipt(evidence)
    else:
        # Historical UI receipts retain their original evidence requirements.
        screenshot = js.to_str(evidence["screenshot"]) if evidence.get("screenshot") is not None else ""
        need(os.path.isabs(screenshot) and os.path.exists(screenshot) and os.path.isfile(screenshot), "needs the absolute path of an existing screenshot")


def _has_reason(evidence: dict) -> bool:
    return isinstance(evidence.get("reason"), str) and js.trim(evidence["reason"]) != ""


#: A group whose rows were appended without a CLS ticket, and none attached since.
NO_TICKET = "this group's rows were appended without a ticket (no 脚本内容 and no CSV): attach one with gof batch ticket first"

#: What a ticket attached to a batch group may give: the 脚本内容 (a Feishu document or 无), the CLS fields and the group message's label.
TICKET_KEYS = ["script_url", "cls", "label"]


def ticket_request(group: dict, ticket_input: Any, adopted: bool = False) -> dict:
    """
    A group's request with a CLS ticket put on it: the script link (or 无) and the CLS fields, checked the way a new requirement's are.
    Everything that was appended (rows, names, tags, supplier, reviewer) stays as it was approved; rows taken over (`adopted`) stay as the
    sheet has them.
    """
    need(is_object(ticket_input) and all(k in TICKET_KEYS for k in ticket_input.keys()), f"a ticket takes only {', '.join(TICKET_KEYS)}")
    need(isinstance(ticket_input.get("script_url"), str) and js.trim(ticket_input["script_url"]) != "", f"script_url is missing: the requirement document link, or \"{NO_SCRIPT}\" when there is none")
    need(is_object(ticket_input.get("cls")) and len(ticket_input["cls"]) > 0, "the CLS fields (direction, task_type) are missing")
    request = normalize_request({"groups": [{**group["request"], **ticket_input}]}, adopted=adopted)["groups"][0]

    def rest(g: dict) -> dict:
        return {k: v for k, v in g.items() if k not in TICKET_KEYS}

    need(canonical(rest(request)) == canonical(rest(group["request"])), "the ticket changes the appended rows: it may only add script_url, the CLS fields and label")
    return request


def ticket_group(plan: dict, records: list[dict], group_id: Optional[str]) -> dict:
    """
    A group the way its CLS ticket sees it: the approved group, or for rows appended without a ticket, the one attached to it since (the
    last `ticket_attached`), with its own CSV.
    """
    group = group_for(plan, group_id)
    attached = next((e for e in reversed(records) if e.get("group") == group_id and e["step"] == "ticket_attached"), None)
    if not attached:
        return group
    ticket = {k: v for k, v in attached["evidence"].items() if k not in ("csv", "csv_sha256")}
    return {**group, "csv": attached["evidence"].get("csv", js.UNDEFINED), "request": ticket_request(group, ticket, plan.get("adopted") is True)}


def _ticket_groups(plan: dict, records: list[dict]) -> list[dict]:
    return [ticket_group(plan, records, g["id"]) for g in plan["groups"]]


def attach_ticket(directory: str, group_id: str, ticket_input: Any, catalog: dict, preview: bool = False) -> dict:
    """
    Puts a CLS ticket on a group whose rows were appended without one (numbers reserved first, the requirement written later): its CSV for
    the rows as they are in the sheet, recorded as `ticket_attached`; the CLS stage then runs for the group as for any other. `preview`
    writes nothing and returns what the user is asked to approve. Allowed until the group's CLS stage has started.
    """
    from .cls import PENDING_SUPPLIER, build_payload, requirement_name

    batch_path, manifest = load_batch(directory)
    reject_secrets(ticket_input)
    reject_secrets(catalog)
    need(isinstance(catalog, dict) and catalog.get("can_create") is True, "the CLS account may not create requirements")
    group = group_for(manifest["plan"], group_id)
    request = ticket_request(group, ticket_input, manifest["plan"].get("adopted") is True)
    _, _, defaults, matched = build_payload(request, catalog, group["names"])
    result = {
        "group": group_id, "requirement_name": requirement_name(group["names"]), "quantity": request["quantity"], "first": group["names"][0],
        "last": group["names"][-1], "script_url": request["script_url"], "supplier": request["supplier"] or PENDING_SUPPLIER,
        "reviewer": request["reviewer"], "cls_names": matched, "native_defaults": defaults, "message_kind": kind_of(request),
    }
    _check_ticket_allowed(batch_path, manifest["plan"], group_id)
    if preview:
        return {"status": "ticket_preview", **result}
    csv = csv_name(group["id"], request)
    n = 2
    while os.path.exists(os.path.join(batch_path, csv)):
        csv = re.sub(r"\.csv$", f"-{n}.csv", csv_name(group["id"], request))
        n += 1
    text = csv_text(group["names"], request["script_url"])
    write_text_new(os.path.join(batch_path, csv), text)
    record(batch_path, "ticket_attached", group_id, {**ticket_input, "csv": csv, "csv_sha256": sha256(text)})
    return {"status": "ticket_attached", **result, "csv": os.path.join(batch_path, csv)}


def _check_ticket_allowed(batch_path: str, plan: dict, group_id: str) -> None:
    """A ticket can be attached to a group appended without one, once the rows are verified, until its CLS stage begins."""
    state = status(batch_path)
    need(not state.get("taken_over"), taken_over(state.get("taken_over")))
    need(not state["blocked"], "the batch is blocked: deal with the cause first")
    need(state["append_verified"], "this batch's rows are not appended and verified yet: no ticket can be attached")
    need(len(group_for(plan, group_id)["request"].get("cls") or {}) == 0, "this group was approved with a ticket: create it as planned")
    need(not any(s.startswith("cls_") or s in TICKET_STEPS for s in state["groups"][group_id]), "this group's CLS stage has started: its ticket can no longer change")


def list_batches(root: Optional[str] = None, number: Optional[int] = None) -> dict:
    """
    The batches of this user (newest first), each with its sheet, numbers and groups, and whether a group has its CLS ticket; `number`
    keeps only the batch holding that material number. `adopted` marks one made of rows taken over from the sheet (see adopt). A folder
    that is not a readable batch is left out.
    """
    root = default_batch_root() if root is None else root
    found: list[dict] = []
    for name in os.listdir(root) if os.path.exists(root) else []:
        try:
            batch_path, manifest = load_batch(os.path.join(root, name))
            plan = manifest["plan"]
            # The rows it holds, not the span from its first to its last number.
            if number is not None and not any(r.get("number") == number for r in plan["rows"]):
                continue
            records = events(batch_path)
            state = status(batch_path)
            entry = {"batch": batch_path, "prepared_at": js.iso((os.stat(os.path.join(batch_path, "manifest.json")).st_mtime_ns + 500_000) // 1_000_000),
                     "worksheet": plan["worksheet"], "first_number": plan["first_number"], "last_number": plan["last_number"],
                     "append_verified": state["append_verified"], "blocked": state["blocked"]}
            if state.get("adopted"):
                entry["adopted"] = True
            if state.get("taken_over"):
                entry["taken_over"] = state["taken_over"]
            entry["groups"] = [{"group": g["id"], "quantity": g["request"]["quantity"], "first": g["names"][0], "last": g["names"][-1],
                                "supplier": g["request"]["supplier"] or None, "reviewer": g["request"]["reviewer"],
                                "ticket": len(g["request"].get("cls") or {}) > 0, "steps": state["groups"][g["id"]]} for g in _ticket_groups(plan, records)]
        except Exception:
            continue
        found.append(entry)
    found.sort(key=lambda e: e["prepared_at"], reverse=True)
    return {"status": "batches", "batches": found}


def record(directory: str, step: str, group_id: Optional[str] = None, evidence_input: Optional[dict] = None, fresh: Optional[Snapshot] = None) -> dict:
    from .cls import validate_batch_receipt

    batch_path, manifest = load_batch(directory)
    records = events(batch_path)
    evidence: dict = evidence_input if evidence_input is not None else {}
    reject_secrets(evidence)
    state = status(batch_path)
    group = group_id
    # Rows another batch took over are that batch's: nothing goes on here, or the same rows could get a second ticket.
    need(not state.get("taken_over"), taken_over(state.get("taken_over")))
    need(not state["blocked"] or step in ("resumed", "taken_over"), "a blocked batch does not continue automatically; leave it as it is for the user to decide")
    adopted = manifest["plan"].get("adopted") is True
    need(not adopted or step not in ("append_started", "append_verified"), ADOPTED)
    ev = evidence if isinstance(evidence, dict) else {}
    if step == "rows_adopted":
        need(adopted and len(records) == 0 and group is None, "recorded only by gof batch adopt when it takes over existing rows")
    elif step == "taken_over":
        need(not adopted and group is None and any(e["step"] == "append_started" for e in records) and not state["append_verified"]
             and isinstance(ev.get("by"), str) and os.path.isabs(ev["by"]), "recorded only by gof batch adopt when it takes over rows of an unverified write")
    elif step == "resumed" and adopted:
        # Nothing was appended, so there is no write to read back: only what CLS already has must have been looked at.
        need(state["blocked"] and group is None, "resuming needs a blocked record")
        need(ev.get("confirmed") is True and _has_reason(ev), "resuming needs the user's explicit decision and a reason")
        if any(e["step"] == "cls_started" for e in records):
            need(ev.get("cls_reconciled") is True, "check the existing draft and import first (cls_reconciled), so no second ticket is made")
    elif step == "resumed":
        need(state["blocked"] and fresh is not None and group is None, "resuming needs a blocked record and a fresh snapshot")
        need(ev.get("confirmed") is True and _has_reason(ev), "resuming needs the user's explicit decision and a reason")
        result = verify(batch_path, fresh, ev.get("validations"))
        never_attempted = not any(e["step"] == "append_started" for e in records)
        if ev.get("retry_append") is True:
            need("api-plan.json" not in manifest["artifacts"], "an API batch whose write was attempted can only be read back; sending again is not allowed")
            need(ev.get("sync_confirmed") is True and not state["append_verified"]
                 and all(e["step"] in ("append_started", "blocked", "resumed") for e in records), "a retry needs the sync confirmed, and no verified append or CLS stage")
            need(result["status"] == "not_applied" and fresh.fingerprint == manifest["plan"]["baseline_digest"],
                 "a retry is allowed only when the whole batch is empty and the full snapshot matches the baseline; a partial write or a conflict cannot be retried")
        else:
            need(result["status"] == "complete" or (never_attempted and result["status"] == "not_applied" and fresh.fingerprint == manifest["plan"]["baseline_digest"]),
                 "a partial, conflicting or unconfirmed write cannot be resumed")
        if any(e["step"] == "cls_started" for e in records):
            need(ev.get("cls_reconciled") is True, "check the existing draft and import first (cls_reconciled), so no second ticket is made")
        evidence = ev = {**ev, "verified_snapshot_digest": fresh.fingerprint}
    elif step == "append_started":
        need(can_start_append(records) and group is None, "a write was attempted already: it is not recorded or pasted again")
    elif step == "append_verified":
        need(group is None, "append_verified belongs to no group")
        need(any(e["step"] == "append_started" for e in records) and not state["append_verified"], "append_verified is out of order")
        need(fresh is not None and verify(batch_path, fresh, ev.get("validations"))["status"] == "complete", "the append did not verify completely")
    elif step == "blocked":
        need(_has_reason(ev), "a reason is needed")
    else:
        need(state["append_verified"], "the progress-sheet rows must be verified first")
        plan_group = group_for(manifest["plan"], group)
        # A ticket attached to the group is what its CLS stage works from, not a step of it.
        stages = [s for s in state["groups"][group] if s != "ticket_attached"]
        need("handoff" not in stages or step == "requirement_number", "this group was handed off and does not continue automatically")
        if step == "ticket_attached":
            _check_ticket_allowed(batch_path, manifest["plan"], group)
            csv = ev.get("csv")
            sha = ev.get("csv_sha256")
            ticket = {k: v for k, v in ev.items() if k not in ("csv", "csv_sha256")}
            request = ticket_request(plan_group, ticket, adopted)
            need(isinstance(csv, str) and os.path.basename(csv) == csv and re.search(r"\.csv\Z", csv) and csv not in manifest["artifacts"], "invalid CSV file name for the ticket")
            with open(os.path.join(batch_path, csv), "r", encoding="utf-8", newline="") as handle:
                text = handle.read()
            need(sha256(text) == sha and text == csv_text(plan_group["names"], request["script_url"]), "the ticket's CSV does not match this group's rows")
        elif step == "cls_started":
            need(ticket_group(manifest["plan"], records, group).get("csv"), NO_TICKET)
            need(len(stages) == 0, "a CLS attempt exists already: look for the draft first; none is created again")
        elif step == "cls_api_attempt":
            action = ev.get("action")
            need(action in ("save_draft", "import_scripts") and isinstance(ev.get("operation_id"), str)
                 and re.fullmatch(r"[0-9a-f]{64}", ev["operation_id"]), "invalid action or digest for the API attempt")
            cls_check(batch_path, group, action, ev.get("observations") if ev.get("observations") is not None else {})
        elif step == "draft_saved":
            need([s for s in stages if s != "cls_api_attempt"] == ["cls_started"], "a draft exists already, or the records are out of order")
            _cls_evidence(ev, "api-plan.json" in manifest["artifacts"])
            if ev.get("transport") == "cls_api":
                validate_batch_receipt(batch_path, group, ev, "draft_saved")
        elif step == "scripts_imported":
            need("draft_saved" in stages and "scripts_imported" not in stages, "the import is out of order or repeated")
            _cls_evidence(ev, "api-plan.json" in manifest["artifacts"])
            if ev.get("transport") == "cls_api":
                validate_batch_receipt(batch_path, group, ev, "scripts_imported")
            need(js.is_int(ev.get("quantity")) and ev["quantity"] == plan_group["request"]["quantity"]
                 and canonical(ev.get("names")) == canonical(plan_group["names"]), "the imported quantity or names differ")
        elif step == "handoff":
            need(_has_reason(ev), "say what the person taking over has to do")
        elif step == "requirement_number":
            need("draft_saved" in stages or "handoff" in stages, "there is no ticket to give a number to yet")
            need(ev.get("confirmed") is True, "the official number must come from the user, the real page or an API read-back")
        else:
            raise Blocked("unsupported step")
    number = ev.get("requirement_number")
    if number is not None:
        need(isinstance(number, str) and re.fullmatch(r"GOFCN[0-9]{11}", number), "not an official requirement number; a material number or a draft ID cannot be used")
    if step == "requirement_number":
        need(number is not None, "the official requirement number is missing")
    event: dict[str, Any] = {"step": step, "group": group, "evidence": evidence, "time": js.iso(),
                             "previous": records[-1]["event_id"] if records else ""}
    event["event_id"] = digest(event)
    write_json_new(os.path.join(batch_path, "events", f"{str(len(records) + 1).zfill(6)}.json"), event)
    return {"status": "recorded", "step": step, "group": group}


NUMBER_PLACEHOLDER = "【发布后补填需求编号】"
#: Steps after which a group has a CLS ticket (or one the user is making by hand): only such groups belong in the group message.
TICKET_STEPS = ["draft_saved", "scripts_imported", "handoff", "requirement_number"]


def kind_of(g: dict) -> str:
    """What a group asks for: its label and quantity ("素人剧情*25"); without a label, its rows counted by 一级标签 in the order they first appear."""
    if isinstance(g.get("label"), str) and g["label"] != "":
        return f"{g['label']}*{js.to_str(g['quantity'])}"
    kinds: dict[str, int] = {}
    for item in items_of(g):
        kinds[item["primary_tag"]] = kinds.get(item["primary_tag"], 0) + item["quantity"]
    return "，".join(f"{kind}*{js.to_str(quantity)}" for kind, quantity in kinds.items())


def message(directory: str) -> dict:
    """
    The copyable group message of the whole batch: every requirement (one per group, in the order of the plan) with its number and supplier,
    then the creators of all of them once, each person once. A group without a ticket is left out and named in `skipped_groups`;
    `missing_numbers` names the groups that still carry the placeholder instead of the official number.
    """
    batch_path, manifest = load_batch(directory)
    records = events(batch_path)
    return compose_message(_ticket_groups(manifest["plan"], records), records)


def compose_message(groups: list[dict], records: list[dict]) -> dict:
    """The group message from the plan's groups and the batch's events; the part of `message` that needs no batch on disk."""
    from .cls import PENDING_SUPPLIER

    lines = ["老师好 刚刚发了新的工单"]
    creators: list[str] = []
    skipped: list[str] = []
    missing: list[str] = []
    for group in groups:
        if not any(e.get("group") == group["id"] and e["step"] in TICKET_STEPS for e in records):
            skipped.append(group["id"])
            continue
        found = next((e for e in reversed(records) if e.get("group") == group["id"] and (e.get("evidence") or {}).get("requirement_number")), None)
        if not found:
            missing.append(group["id"])
        g = group["request"]
        lines.append(f"需求编号：{found['evidence']['requirement_number'] if found else NUMBER_PLACEHOLDER}（{kind_of(g)}）")
        lines.append(f"供应商：{g.get('supplier') or PENDING_SUPPLIER}")
        if g.get("creator") not in creators:
            creators.append(g.get("creator"))
    if not creators:
        return {"status": "message_not_ready", "message": None, "skipped_groups": skipped, "missing_numbers": missing}
    lines.append(f"创意人：{'/'.join(js.to_str(c) for c in creators)}")
    lines.append("辛苦安排一下~")
    return {"status": "message_ready", "message": "\n".join(lines), "skipped_groups": skipped, "missing_numbers": missing}


def sheet_url(sheet_id: Optional[str] = None) -> str:
    """The progress sheet's tab of `sheet_id` (the sheet itself without one)."""
    return f"{TARGET}?sheet={js.encode_uri_component(sheet_id)}" if sheet_id else TARGET


def _links(sheet_id: Optional[str], groups: list[dict]) -> dict:
    """What the user is handed to look at the result: the progress sheet's tab and each group's CLS draft, once saved."""
    def url_of(step: Any) -> Any:
        value = step.get("url") if isinstance(step, dict) else None
        return None if value is js.UNDEFINED else value

    out = []
    for g in groups:
        draft = url_of(g.get("scripts_imported"))
        if draft is None:
            draft = url_of(g.get("draft_saved"))
        out.append({"group": g["group"], "draft": draft})
    return {"progress_sheet": sheet_url(sheet_id), "cls": out}


def report(directory: str) -> dict:
    """Local audit report, derived from immutable events and validated receipts; no network."""
    from .cls import validate_batch_receipt

    batch_path, manifest = load_batch(directory)
    records = events(batch_path)
    plan = manifest["plan"]
    state = status(batch_path)
    groups: list[dict] = []
    for group in _ticket_groups(plan, records):
        result: dict[str, Any] = {"group": group["id"], "quantity": group["request"]["quantity"],
                                  "csv": os.path.join(batch_path, group["csv"]) if group.get("csv") else None, "steps": state["groups"][group["id"]]}
        for event in records:
            if event.get("group") == group["id"] and event["step"] in ("draft_saved", "scripts_imported"):
                evidence = event["evidence"]
                if evidence.get("transport") == "cls_api":
                    validate_batch_receipt(batch_path, group["id"], evidence, event["step"])
                    result[event["step"]] = {"receipt": evidence.get("receipt", js.UNDEFINED), "verified_at": event["time"],
                                             "draft_id": evidence.get("draft_id", js.UNDEFINED), "url": evidence.get("url", js.UNDEFINED)}
        groups.append(result)
    context = read_json(os.path.join(batch_path, "api-plan.json")) if "api-plan.json" in manifest["artifacts"] else {}
    verified = next((e for e in reversed(records) if e["step"] == "append_verified"), None)
    result = {
        "schema": 1, "status": "report_ready", "plan_id": plan["plan_id"], "target": TARGET, "worksheet": plan["worksheet"],
        "sheet_id": context.get("sheet_id"), "range": plan["range"], "state": state, "groups": groups, "message": message(batch_path)["message"],
        "evidence_time": "the read-back times saved at each stage; this report does not query the live state again",
        "links": _links(context.get("sheet_id"), groups),
    }
    if verified:
        evidence = verified["evidence"]
        if evidence.get("transport") == "feishu_api" and evidence.get("snapshot"):
            snapshot_path = os.path.abspath(evidence["snapshot"])
            need(is_inside(batch_path, snapshot_path), "the report's snapshot must be inside the batch directory")
            snapshot = Snapshot(snapshot_path)
            need(snapshot.fingerprint == evidence.get("snapshot_digest") and verify(batch_path, snapshot, evidence.get("validations"))["status"] == "complete",
                 "the local snapshot or rule proof behind the report has changed")
        result["append_verified"] = {"verified_at": verified["time"], "evidence": verified["evidence"]}
    return result
