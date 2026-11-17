"""The dropdown rules as the Feishu API reports them, compared the same way the XLSX rules are."""

from __future__ import annotations

import os
from typing import Any, Optional

from . import js
from .a1 import CellRange, column_letter, coord_of
from .request import field_values
from .seal import check_seal, seal
from .util import canonical, check_target, is_inside, need, read_json, write_json_new

PALETTE = ["#bacefd", "#a9efe6", "#e5d6ff", "#fed4a4", "#c4e8a0"]


def _rule_body(r: dict) -> dict:
    return {"options": r["options"], "multiple": r["multiple"], "highlight": r["highlight"], "colors": r["colors"]}


def _merge(intervals: list[list[int]]) -> list[list[int]]:
    merged: list[list[int]] = []
    for lo, hi in sorted(intervals, key=lambda p: (p[0], p[1])):
        if merged and lo <= merged[-1][1] + 1:
            merged[-1][1] = max(hi, merged[-1][1])
        else:
            merged.append([lo, hi])
    return merged


def rule_projection(records: list[dict], owned_sheet: Optional[str] = None, owned_range: Optional[str] = None) -> list:
    """Union intervals per sheet and column, excluding only the approved append rectangle."""
    buckets: dict[tuple, dict] = {}
    area = CellRange(owned_range) if owned_range else None
    for entry in records:
        signature = canonical({"type": entry["type"], **_rule_body(entry)})
        for raw in entry["ranges"]:
            rect = CellRange(raw)
            for col in range(rect.min_col, rect.max_col + 1):
                intervals = [[rect.min_row, rect.max_row]]
                if entry["sheet_id"] == owned_sheet and area and area.min_col <= col <= area.max_col:
                    intervals = [[rect.min_row, min(rect.max_row, area.min_row - 1)], [max(rect.min_row, area.max_row + 1), rect.max_row]]
                key = (entry["sheet_id"], col, signature)
                bucket = buckets.get(key)
                if bucket is None:
                    bucket = {"sheetId": entry["sheet_id"], "col": col, "signature": signature, "intervals": []}
                    buckets[key] = bucket
                bucket["intervals"].extend(i for i in intervals if i[0] <= i[1])
    result: list = []
    ordered = sorted(buckets.values(), key=lambda b: (js.utf16_key(b["sheetId"]), b["col"], js.utf16_key(b["signature"])))
    for b in ordered:
        merged = _merge(b["intervals"])
        if merged:
            result.append([b["sheetId"], b["col"], b["signature"], merged])
    return result


def column_options(records: list[dict], sheet_id: str, col: int, last_row: int) -> list[str]:
    """The options a new row of a column would offer: those of the rule nearest above it (several equally near: all of them, in order)."""
    found: list[tuple[int, dict]] = []
    for rule in records:
        if rule["sheet_id"] != sheet_id:
            continue
        for raw in rule["ranges"]:
            rect = CellRange(raw)
            if rect.min_col <= col <= rect.max_col and rect.min_row <= last_row:
                found.append((min(rect.max_row, last_row), rule))
    if not found:
        return []
    nearest = max(end for end, _ in found)
    options: list[str] = []
    for end, rule in found:
        if end == nearest:
            for value in rule["options"]:
                if value not in options:
                    options.append(value)
    return options


def dropdowns(plan: dict, sheet_id: str, records: list[dict]) -> list[dict]:
    """The dropdown to write for each planned field: the nearest existing rule's mode and colors, plus the missing options."""
    result: list[dict] = []
    area = CellRange(plan["range"])
    for spec in plan["dropdowns"]:
        col = CellRange(spec["range"]).min_col
        found: list[tuple[int, dict]] = []
        for rule in records:
            if rule["sheet_id"] != sheet_id:
                continue
            for raw in rule["ranges"]:
                rect = CellRange(raw)
                if rect.min_col <= col <= rect.max_col and rect.min_row < area.min_row:
                    found.append((min(rect.max_row, area.min_row - 1), rule))
        need(len(found) > 0, f"{spec['field']} has no existing rule whose mode and colors can be kept")
        nearest = max(end for end, _ in found)
        choices: dict[str, dict] = {}
        for end, r in found:
            if end == nearest:
                choices[canonical(_rule_body(r))] = r
        need(len(choices) == 1, "the nearest dropdown rules conflict: settle it")
        rule = list(choices.values())[0]
        # Only the values this request asks for are added to the rule being kept. The XLSX export is not a reliable source of a column's
        # options (it can attribute a stray rule on a few old cells to the whole column), so spec.options is not used here.
        wanted = field_values(plan["request"]["groups"], spec["field"])
        options = [*rule["options"], *[v for v in wanted if v not in rule["options"]]]
        colors = [rule["colors"].get(v) if rule["colors"].get(v) is not None else PALETTE[i % len(PALETTE)] for i, v in enumerate(options)]
        # Existing highlighted options must have an actual color, never a guessed replacement.
        need(not rule["highlight"] or all(v in rule["colors"] for v in rule["options"]), "the existing dropdown colors were not read completely")
        result.append({"field": spec["field"], "range": spec["range"], "options": options, "mode": "multiple" if rule["multiple"] else "single",
                       "highlight": rule["highlight"], "colors": colors, "missing": [v for v in options if v not in rule["options"]]})
    return result


def build_writes(plan: dict, context: dict) -> list[dict]:
    """One write per non-empty column, every one with allow_overwrite=false."""
    check_seal(context, "api_plan_id")
    need(context.get("batch_plan_id") == plan["plan_id"] and context.get("baseline_digest") == plan["baseline_digest"], "the API plan does not match the batch")
    check_target(context.get("target"))
    area = CellRange(plan["range"])
    need(area.max_row - area.min_row + 1 <= 5000, "the batch has more rows than one write allows: split the plan")
    validation = {CellRange(d["range"]).min_col: d for d in context["dropdowns"]}
    writes: list[dict] = []
    for col in range(area.min_col, area.max_col + 1):
        letter = column_letter(col)
        values = [row["values"].get(f"{letter}{row['row']}") for row in plan["rows"]]
        if all(v is None for v in values):
            continue  # Never send nulls/clears into unused fields.
        need(all(v is not None for v in values), "a written column has gaps; clearing cells is not allowed")
        cells = []
        for value in values:
            cell: dict[str, Any] = {"formula": value} if isinstance(value, str) and value.startswith("=") else {"value": value}
            d = validation.get(col)
            if d:
                cell["cell_styles"] = {"horizontal_alignment": "center"}  # dropdown columns are centered in the sheet; the rest stay left-aligned
                cell["data_validation"] = {"type": "list", "items": d["options"], "support_multiple_values": d["mode"] == "multiple",
                                           "enable_highlight": d["highlight"], "highlight_colors": d["colors"]}
                if d["mode"] == "multiple":
                    cell.pop("value", None)
                    cell["multiple_values"] = [{"value": value}]
            cells.append([cell])
        writes.append({"sheet_id": context["sheet_id"], "range": f"{letter}{area.min_row}:{letter}{area.max_row}", "cells": cells, "allow_overwrite": False})
    need(0 < len(writes) <= 100, "unsupported number of write ranges")
    need(sum(len(w["cells"]) for w in writes) <= 50000, "the batch has more cells than one write allows: split the plan")
    return writes


def _check_new_rules(records: list[dict], sheet_id: str, dropdown_specs: list[dict], verified_against: str) -> None:
    for spec in dropdown_specs:
        rect = CellRange(spec["range"])
        for row in range(rect.min_row, rect.max_row + 1):
            coord = coord_of(rect.min_col, row)
            matching = [r for r in records if r["sheet_id"] == sheet_id and any(CellRange(a).contains(coord) for a in r["ranges"])]
            if verified_against == "fresh":
                need(len({canonical(_rule_body(r)) for r in matching}) == 1, "a new row's dropdown rule is missing or conflicting")
            else:
                need(len(matching) > 0, "the new rows' dropdown rules did not verify")

            def same(r: dict) -> bool:
                return (canonical(r["options"]) == canonical(spec["options"]) and r["multiple"] == (spec["mode"] == "multiple")
                        and r["highlight"] == spec["highlight"]
                        and (not spec["highlight"] or canonical([r["colors"].get(v, js.UNDEFINED) for v in r["options"]]) == canonical(spec["colors"])))

            if verified_against == "fresh":
                need(same(matching[0]), "a new dropdown's options, colors or mode differ")
            else:
                need(all(same(r) for r in matching), "the new rows' dropdown rules did not verify")


def _append_column_rules(records: list[dict], sheet_id: str, range_: str) -> list:
    """
    The old rules of the columns the append writes into, on its sheet: the only ones its write (which sets the new rows' dropdowns) could
    have changed. A rule changed anywhere else after the write is somebody else's edit of the shared workbook; verify reports such changes.
    """
    area = CellRange(range_)
    return [p for p in rule_projection(records, sheet_id, range_) if p[0] == sheet_id and area.min_col <= p[1] <= area.max_col]


def rule_proof(file: str, fresh: Any, records: list[dict], context: dict, plan: dict) -> dict:
    """After a write: the old rules of the written columns must be unchanged and the new rows must carry the planned dropdowns. Writes the proof file."""
    sid = context["sheet_id"]
    need(canonical(_append_column_rules(context["rules"], sid, plan["range"])) == canonical(_append_column_rules(records, sid, plan["range"])),
         "the API shows that existing dropdown rules, colors or modes changed")
    _check_new_rules(records, sid, context["dropdowns"], "fresh")
    proof = seal({"transport": "feishu_api", "api_plan_id": context["api_plan_id"], "snapshot_digest": fresh.fingerprint, "rules": records}, "proof_id")
    write_json_new(file, proof)
    return proof


def validate_rule_evidence(directory: str, fresh: Any, evidence: dict) -> list[dict]:
    """Called by verify: recomputes the proof from the saved rules instead of trusting a success flag."""
    from .batch import load_batch

    batch_path, manifest = load_batch(directory)
    need("api-plan.json" in manifest["artifacts"], "the sealed API plan is missing")
    context = read_json(os.path.join(batch_path, "api-plan.json"))
    check_seal(context, "api_plan_id")
    need(isinstance(evidence, dict) and evidence.get("transport") == "feishu_api", "an API batch needs the API rule evidence")
    proof_path = os.path.abspath(js.to_str(evidence.get("proof", "")) if evidence.get("proof") is not None else "")
    need(is_inside(batch_path, proof_path) and os.path.isfile(proof_path), "the rule proof is not inside the batch directory")
    proof = read_json(proof_path)
    check_seal(proof, "proof_id")
    need(proof.get("api_plan_id") == context["api_plan_id"] and proof.get("snapshot_digest") == fresh.fingerprint, "the rule evidence does not match the API plan or this XLSX")
    # Recompute all comparisons without writing another proof.
    plan = manifest["plan"]
    sid = context["sheet_id"]
    need(canonical(_append_column_rules(context["rules"], sid, plan["range"])) == canonical(_append_column_rules(proof["rules"], sid, plan["range"])),
         "existing dropdown rules changed")
    _check_new_rules(proof["rules"], sid, context["dropdowns"], "proof")
    return context["dropdowns"]


def acceptance(receipt: str, bridge_version: str) -> dict:
    """The write-protection receipt the isolated probe leaves behind; production appends are refused without it."""
    data = read_json(receipt)
    check_seal(data, "receipt_id")
    need(data.get("bridge_version") == bridge_version and data.get("identity") == "user" and data.get("schema") == 2 and data.get("probe_workbook")
         and canonical(data.get("passed")) == canonical(["occupied_value", "empty_formula", "repeated_occupancy"]),
         "the overwrite receipt is not valid for this bridge: appends are disabled")
    need(data.get("probe_workbook") != data.get("production_token"), "the probe cannot use the progress sheet")
    expected = [["KEEP_EXISTING", '=IF(1=1,"","")']]
    observations = data.get("observations") or []
    need(len(observations) == 3 and canonical([o.get("case") for o in observations]) == canonical(data.get("passed"))
         and all(o.get("rejection") == "non_empty" and canonical(o.get("before")) == canonical(expected) and canonical(o.get("after")) == canonical(expected)
                 for o in observations),
         "the receipt lacks a refusal and read-back for every probe case")
    return data
