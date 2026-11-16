"""Plans the rows to append: the next material numbers, the naming formula, the dropdown options to extend (plan.ts)."""

from __future__ import annotations

import re
from typing import Any, Optional

from . import js
from .a1 import CellRange, column_letter, coord_of, translate_formula
from .request import ITEM_FIELDS, OPTIONAL_FIELDS, field_values, items_of, normalize_request
from .snapshot import FIELD_HEADERS, REQUIRED_HEADERS, Columns, Snapshot, choose_sheet, collect_ids, concatenate, formula_refs, options_for
from .util import Blocked, TARGET, digest, need, safe_filename

Plan = dict


def naming_formula(cols: Columns, row: int) -> str:
    """
    The sheet's naming: GOFCN<素材编号>_<一级标签>_<二级标签>_<创意形式>_<提炼词>. 创意形式 may be empty, and SUBSTITUTE folds the "__"
    it leaves into one "_".
    """

    def ref(field: str) -> str:
        return coord_of(cols[field], row)

    return (f'=SUBSTITUTE(CONCATENATE("GOFCN",{ref("number")},"_",{ref("primary_tag")},"_",{ref("secondary_tag")},"_",'
            f'{ref("creative_form")},"_",{ref("keyword")}),"__","_")')


def row_formula(snapshot: Snapshot, sheet: str, cols: Columns, row: int, without_form: bool) -> str:
    """
    The naming formula a new row gets: the nearest one above it in the 素材名称 column when it names a row the sheet's way (colleagues' own
    spelling of it is kept, e.g. with the 客户端 cell at the end, which stays empty here), else the sheet's naming itself: colleagues change
    the formula now and then (from row 23282 it left out 创意形式 for a while). Rows without a 创意形式 need it inside
    SUBSTITUTE(…,"__","_"), or the name keeps an empty part.
    """
    own = naming_formula(cols, row)
    above = None
    r = row - 1
    while r > 1 and above is None:
        cell = snapshot.cell(sheet, r, cols["name"])
        if cell is not None and cell.kind == "f" and cell.value is not None:
            above = cell
        r -= 1
    if above is None:
        return own
    formula = translate_formula(above.value, above.coord, coord_of(cols["name"], row))
    # Tried on a row of five distinct values: it must give exactly what the sheet's naming gives.
    probe: dict[str, Any] = {coord: None for coord in formula_refs(formula)}
    probe.update({coord_of(cols["number"], row): 1, coord_of(cols["primary_tag"], row): "A", coord_of(cols["secondary_tag"], row): "B",
                  coord_of(cols["creative_form"], row): "C", coord_of(cols["keyword"], row): "D"})
    try:
        if concatenate(formula, probe) != "GOFCN1_A_B_C_D":
            return own
    except Blocked:
        return own
    if not without_form:
        return formula
    probe[coord_of(cols["creative_form"], row)] = None
    return formula if concatenate(formula, probe) == "GOFCN1_A_B_D" else f'=SUBSTITUTE({formula[1:]},"__","_")'


def naming_pattern(formula: str, row: int, cols: Columns) -> str:
    """How the sheet names a row, in its own words: "GOFCN<素材编号>_<一级标签>_<二级标签>_<创意形式>_<提炼词>"."""
    fields = {coord_of(col, row): FIELD_HEADERS[field] for field, col in cols.items()}
    shown = {coord: f"<{fields.get(coord, coord)}>" for coord in formula_refs(formula)}
    try:
        return concatenate(formula, shown)
    except Blocked:
        return formula


def supplier_cell(g: dict) -> str:
    """
    What the 设计师供应商 cell of a group's rows holds: its supplier, or nothing when the supplier is the 创意者脚本审核人 too (the sheet
    leaves the cell empty then). The request keeps the supplier itself, for CLS, the group message and the uploads' 制作人.
    """
    return "" if g.get("supplier") == g.get("reviewer") else g.get("supplier")


def csv_name(ident: str, g: dict) -> str:
    """The file name of a group's CSV: 创意脚本_<组>_<题材>_<创意形式>_<供应商>.csv, from the group's first rows."""
    first = items_of(g)[0]
    parts = [ident, first.get("topic"), first.get("creative_form"), g.get("supplier")]
    return f"创意脚本_{'_'.join(safe_filename(p) for p in parts if p)}.csv"


def make_plan(snapshot: Snapshot, request_input: Any, current: Optional[str] = None) -> Plan:
    """Plans the rows to append: the next material numbers, the naming formula, the dropdown options to extend."""
    request = normalize_request(request_input)
    sheet, tables, ignored = choose_sheet(snapshot, request, current)
    ws = snapshot.sheet(sheet)
    cols = tables[sheet]
    need(not ws.protected_sheet, "导出工作表受保护，请核对实际编辑权限")
    warnings = [f"较早的 GOFCN 工作表“{name}”表头不可用，未参与编号避让" for name in ignored]
    identifiers = collect_ids(snapshot, tables, warnings)
    start_number = max(identifiers.keys()) + 1
    start_row = snapshot.last[sheet] + 1
    total = sum(g["quantity"] for g in request["groups"])
    end_row = start_row + total - 1
    need(end_row <= 1048576, "追加超出工作表行数")
    optional_used = [k for k in OPTIONAL_FIELDS if any(g[k] != "" for g in request["groups"])]
    need(all(k in cols for k in optional_used), "本批可选字段在工作表中没有对应列")
    used = [*REQUIRED_HEADERS, *optional_used]
    left = min(cols[k] for k in used)
    right = max(cols[k] for k in used)
    area = CellRange(min_col=left, max_col=right, min_row=start_row, max_row=end_row)
    for text in ws.merges:
        merged = CellRange(text)
        need(merged.max_row < area.min_row or merged.min_row > area.max_row or merged.max_col < area.min_col or merged.min_col > area.max_col,
             "追加范围与合并单元格相交")
    # The API writes a column in one go: a column of some rows with a value and some without would be a write with holes.
    forms = [item["creative_form"] != "" for g in request["groups"] for item in items_of(g)]
    need(all(forms) or not any(forms),
         "这一批有的行写了创意形式、有的没写（追加时一列一次写完，不能留空）：都不写创意形式、把它放在提炼词前面（素材名称不变），或分两次追加")
    suppliers = [supplier_cell(g) != "" for g in request["groups"]]
    need(all(suppliers) or not any(suppliers), "这一批有的需求写了设计师供应商、有的没写（追加时一列一次写完，不能留空；供应商就是审核人本人时那格留空）：分两次追加")
    without_form = not any(forms)
    naming = naming_pattern(row_formula(snapshot, sheet, cols, start_row, without_form), start_row, cols)
    rows: list[dict] = []
    groups: list[dict] = []
    cursor = 0
    for offset, g in enumerate(request["groups"]):
        ident = str(offset + 1).zfill(2)
        names: list[str] = []
        group_rows: list[int] = []
        for item in items_of(g):
            for _ in range(item["quantity"]):
                r = start_row + cursor
                number = start_number + cursor
                values: dict[str, Any] = {coord_of(c, r): None for c in range(left, right + 1)}
                for field in used:
                    if field == "name":
                        continue
                    if field in ITEM_FIELDS:
                        value = item.get(field)
                    elif field == "supplier":
                        value = supplier_cell(g)
                    else:
                        value = g.get(field)
                    values[coord_of(cols[field], r)] = number if field == "number" else (value if value != "" else None)
                name_coord = coord_of(cols["name"], r)
                formula = row_formula(snapshot, sheet, cols, r, without_form)
                # The name is what the formula makes of the row; a name the request gives (a file named for its row) must come out exactly.
                # A cell of the row outside the appended columns that a colleague's formula takes as well (row_formula took it empty) is
                # empty in a new row: preflight refuses a row with anything in it.
                outside = {coord: None for coord in formula_refs(formula) if int(re.search(r"[0-9]+$", coord).group(0)) == r}
                name = concatenate(formula, {**outside, **values})
                need(name.startswith(f"GOFCN{number}_"), "命名公式未产生正确 GOFCN 编号")
                material_name = item.get("material_name", js.UNDEFINED)
                need(material_name is js.UNDEFINED or name == f"GOFCN{number}_{material_name}",
                     f"按进度表的命名（{naming}），这一行会叫“{name}”，不是要求的“GOFCN{number}_{js.to_str(material_name)}”：核对一级标签、二级标签、创意形式和提炼词")
                need(not re.search(r"[\\/]", name), f"素材名称“{name}”含 / 或 \\（素材名称也是 CLS 里的创意名称，那里不能有）")
                values[name_coord] = formula
                rows.append({"row": r, "number": number, "name": name, "values": values, "group": ident})
                names.append(name)
                group_rows.append(r)
                cursor += 1
        # Rows appended without a script link (finished materials, or numbers reserved before the requirement is written) have no CSV: there
        # is no CLS ticket to import it into, until one is attached to the batch (batch.py attach_ticket).
        groups.append({"id": ident, "csv": None if g["script_url"] == "" else csv_name(ident, g), "names": names, "rows": group_rows, "request": g})
    dropdowns: list[dict] = []
    for field in ["primary_tag", "secondary_tag", "creative_form", "client", "supplier", "creator", "creative_expression"]:
        if field == "supplier":
            wanted = []
            for g in request["groups"]:
                cell = supplier_cell(g)
                if cell and cell not in wanted:
                    wanted.append(cell)
        else:
            wanted = field_values(request["groups"], field)
        if not wanted or field not in cols:
            continue
        existing = options_for(ws, cols[field], start_row - 1)
        need(all("," not in v and '"' not in v for v in wanted), "下拉标签含逗号/引号，需人工核对规则")
        combined = [*existing, *[v for v in wanted if v not in existing]]
        letter = column_letter(cols[field])
        dropdowns.append({"field": field, "range": f"{letter}{start_row}:{letter}{end_row}", "options": combined,
                          "missing": [v for v in wanted if v not in existing], "mode": "multiple" if field == "creative_expression" else "single"})
    plan: Plan = {
        "schema": 1, "target": TARGET, "worksheet": sheet, "current_sheet": current, "request": request, "baseline_digest": snapshot.fingerprint,
        "columns": cols, "range": str(area), "quantity": total, "first_number": start_number, "last_number": start_number + total - 1,
        "rows": rows, "groups": groups, "dropdowns": dropdowns, "history_warnings": warnings, "naming": naming,
    }
    plan["plan_id"] = digest(plan)[:24]
    return plan


BUSINESS_KEYS = ["schema", "target", "worksheet", "current_sheet", "request", "columns", "range", "quantity", "first_number", "last_number",
                 "rows", "groups"]


def business(plan: Plan) -> dict:
    """
    What the user approved: the sheet, the rows with their numbers, values and names, and the groups. A plan made again on a newer snapshot
    may differ in everything else (the snapshot digest, the dropdown options read from it, warnings about old rows).
    """
    return {k: plan.get(k, js.UNDEFINED) for k in BUSINESS_KEYS}
