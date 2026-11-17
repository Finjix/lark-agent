"""A full XLSX export of the progress workbook as the append checks see it: its fingerprint, the GOFCN tabs, numbers and options."""

from __future__ import annotations

import os
import re
import time
from typing import Any, Optional

from . import js, timing
from .a1 import CellRange, column_letter
from .util import Blocked, digest, need
from .xlsx import CellData, RuleData, SheetData, WorkbookData, read_workbook

FIELD_HEADERS: dict[str, str] = {
    "number": "素材编号", "keyword": "提炼词", "name": "素材名称", "primary_tag": "一级标签", "secondary_tag": "二级标签",
    "creative_form": "创意形式", "client": "客户端", "supplier": "供应商", "creator": "创意者", "creative_expression": "创意表达",
    "creative_batch": "创意批次", "head_reference": "片头引用", "creative_reference": "创意引用",
}
REQUIRED_HEADERS = ["number", "keyword", "name", "primary_tag", "secondary_tag", "creative_form", "client", "supplier", "creator"]

Columns = dict[str, int]


def has_content(cell: Optional[CellData]) -> bool:
    """A cell that holds something: a value, a comment or a link. Formatting alone does not count."""
    return cell is not None and (cell.value is not None or cell.comment is not None or cell.link is not None)


def is_text(cell: Optional[CellData]) -> bool:
    return cell is not None and cell.value is not None and cell.kind in ("s", "f", "e")


def holds(cell: Optional[CellData], expected: Any) -> bool:
    """Does the cell hold exactly this expected value (a number for the material number, text otherwise)?"""
    if cell is None or cell.value is None:
        return False
    if js.is_num(expected):
        return cell.kind == "n" and cell.value == js.num_to_str(expected)
    return is_text(cell) and cell.value == expected


class Snapshot:
    def __init__(self, file: str) -> None:
        from . import snapshot_cache

        began = time.perf_counter()
        self.path = os.path.abspath(file)
        need(self.path.lower().endswith(".xlsx"), "the checks need the XLSX export (with formulas), not a CSV")
        directory = snapshot_cache.cache_dir(self.path) if os.path.isfile(self.path) else None
        key = snapshot_cache.key_of(self.path) if directory else None
        state = snapshot_cache.load(directory, key) if directory and key else None
        if state is not None:
            self.__dict__.update(state)
            self.path = os.path.abspath(file)
            timing.add("xlsx:cached", time.perf_counter() - began)
            return
        self._parse()
        if directory and key:
            snapshot_cache.save(directory, key, {k: v for k, v in self.__dict__.items() if k != "path"})
        timing.add("xlsx:parse", time.perf_counter() - began)

    def _parse(self) -> None:
        self.book: WorkbookData = read_workbook(self.path)
        self.sheet_names: list[str] = [s.name for s in self.book.sheets]
        self.last: dict[str, int] = {}
        self.cells: dict[str, dict[str, Any]] = {}
        self.meta: dict[str, Any] = {}
        self._by_name = {s.name: s for s in self.book.sheets}
        for ws in self.book.sheets:
            data: dict[str, Any] = {}
            last = 1
            for cell in ws.cells.values():
                if has_content(cell):
                    last = max(last, cell.row)
                if has_content(cell) or cell.style is not None:
                    data[cell.coord] = {"value": cell.value, "type": cell.kind if cell.value is not None else None, "style": cell.style,
                                        "comment": cell.comment, "link": cell.link}
            self.cells[ws.name] = data
            self.last[ws.name] = last
            self.meta[ws.name] = {"state": ws.state, "merges": ws.merges, "rows": ws.rows, "columns": ws.columns, "filter": ws.filter,
                                  "protection": ws.protection}
        self.rules = validation_projection(self)
        self.fingerprint = digest({"cells": self.cells, "meta": self.meta, "rules": self.rules, "order": self.sheet_names})

    def sheet(self, name: str) -> SheetData:
        found = self._by_name.get(name)
        need(found, f"no such tab: {name}")
        return found  # type: ignore[return-value]

    def cell(self, sheet: str, row: int, col: int) -> Optional[CellData]:
        return self.sheet(sheet).cells.get(f"{column_letter(col)}{row}")

    def close(self) -> None:
        """Nothing is held open."""


def _merge_intervals(intervals: list[list[int]]) -> list[list[int]]:
    merged: list[list[int]] = []
    for lo, hi in sorted(intervals, key=lambda p: (p[0], p[1])):
        if merged and lo <= merged[-1][1] + 1:
            merged[-1][1] = max(hi, merged[-1][1])
        else:
            merged.append([lo, hi])
    return merged


def validation_projection(snapshot: Snapshot, allowed: Optional[tuple[str, CellRange]] = None) -> list:
    """Compares per-column union intervals of the dropdown rules, independent of how a rule was split or ordered."""
    result: list = []
    for ws in snapshot.book.sheets:
        buckets: dict[tuple[int, str], dict] = {}
        for rule in ws.rules:
            for rect in rule.ranges:
                for col in range(rect.min_col, rect.max_col + 1):
                    ranges = [[rect.min_row, rect.max_row]]
                    if allowed and allowed[0] == ws.name and allowed[1].min_col <= col <= allowed[1].max_col:
                        a = allowed[1]
                        ranges = []
                        if rect.min_row < a.min_row:
                            ranges.append([rect.min_row, min(rect.max_row, a.min_row - 1)])
                        if rect.max_row > a.max_row:
                            ranges.append([max(rect.min_row, a.max_row + 1), rect.max_row])
                    key = (col, rule.signature)
                    bucket = buckets.get(key)
                    if bucket is None:
                        bucket = {"col": col, "signature": rule.signature, "ranges": []}
                        buckets[key] = bucket
                    bucket["ranges"].extend(r for r in ranges if r[0] <= r[1])
        ordered = sorted(buckets.values(), key=lambda b: (b["col"], js.utf16_key(b["signature"])))
        for bucket in ordered:
            if not bucket["ranges"]:
                continue
            result.append([ws.name, bucket["col"], bucket["signature"], _merge_intervals(bucket["ranges"])])
    return result


def header_matches(field: str, header: str) -> bool:
    """Is this header cell the column of the field? 设计师供应商 counts as 供应商, 审核人 and 脚本审核人 as 创意者."""
    text = re.sub(r"\s+", "", header)
    stem = FIELD_HEADERS[field]
    if field == "creator":
        return any(text.startswith(h) for h in (stem, "审核人", "脚本审核人"))
    return stem in text if field == "supplier" else text.startswith(stem)


def columns(ws: SheetData) -> Columns:
    result: Columns = {}
    for col in range(1, ws.max_col + 1):
        header = ws.cells.get(f"{column_letter(col)}1")
        text = re.sub(r"\s+", "", (header.value if header and header.value is not None else ""))
        for field, stem in FIELD_HEADERS.items():
            if header_matches(field, text):
                need(field not in result, f"{ws.name}: the {stem} header is not unique")
                result[field] = col
    need(all(f in result for f in REQUIRED_HEADERS), f"{ws.name} is not a complete progress sheet")
    return result


def scan_sheets(snapshot: Snapshot) -> tuple[dict[str, Columns], list[dict[str, str]]]:
    """The GOFCN progress sheets with a usable header, and the GOFCN-named sheets that were turned down (with the reason)."""
    tables: dict[str, Columns] = {}
    rejected: list[dict[str, str]] = []
    for ws in snapshot.book.sheets:
        if "GOFCN" not in ws.name.upper():
            continue
        try:
            tables[ws.name] = columns(ws)
        except Blocked as error:
            rejected.append({"name": ws.name, "reason": error.message})
    need(len(tables) > 0, "no complete GOFCN progress tab found")
    return tables, rejected


def candidates(snapshot: Snapshot) -> dict[str, Columns]:
    return scan_sheets(snapshot)[0]


_PERIOD = re.compile(r"([0-9]{2,4})年([0-9]{1,2})月(?:\s*[-至]\s*([0-9]{1,2})月)?")


def sheet_period(name: str) -> Optional[tuple[int, int]]:
    m = _PERIOD.search(name)
    if not m:
        return None
    year = int(m.group(1))
    month = int(m.group(3) if m.group(3) is not None else m.group(2))
    if year < 100:
        year += 2000
    if m.group(3) is not None and month < int(m.group(2)):
        year += 1  # "2025年12月-1月" ends in January 2026
    return (year, month) if 1 <= month <= 12 else None


def choose_sheet(snapshot: Snapshot, request: dict, current: Optional[str]) -> tuple[str, dict[str, Columns], list[str]]:
    """
    Picks the sheet to append to. A GOFCN-named sheet whose header is not usable is never skipped silently: if it is as new as the chosen
    sheet (or has no readable month), it may be the real target or hold higher numbers, so planning stops. Older ones are returned as
    `ignored` so that the plan can warn about them.
    """
    tables, rejected = scan_sheets(snapshot)

    def key(p: tuple[int, int]) -> int:
        return p[0] * 100 + p[1]

    explicit = request.get("worksheet") or current
    if explicit:
        need(explicit in tables, "the named tab does not exist or its columns do not fit: settle it")
        chosen = explicit
    else:
        dated = [(sheet_period(name), name) for name in tables if sheet_period(name)]
        need(len(dated) > 0, "no tab with a recognizable year and month: name one (worksheet)")
        latest = max(key(p) for p, _ in dated)
        names = [name for p, name in dated if key(p) == latest]
        need(len(names) == 1, "several tabs are the newest: name one (worksheet)")
        chosen = names[0]
    chosen_period = sheet_period(chosen)
    blocking = [r for r in rejected if not sheet_period(r["name"]) or not chosen_period or key(sheet_period(r["name"])) >= key(chosen_period)]
    first = blocking[0] if blocking else {"name": "undefined", "reason": "undefined"}
    need(len(blocking) == 0,
         f"the GOFCN tab \"{first['name']}\" is not a usable progress sheet ({first['reason']}); it may be the target or hold higher numbers, so it is not skipped silently: fix that tab first, then plan")
    return chosen, tables, [r["name"] for r in rejected]


def collect_ids(snapshot: Snapshot, tables: dict[str, Columns], warnings: Optional[list[str]] = None) -> dict[int, tuple[str, int]]:
    warnings = [] if warnings is None else warnings
    identifiers: dict[int, tuple[str, int]] = {}
    for name, cols in tables.items():
        for r in range(2, snapshot.last[name] + 1):
            raw = snapshot.cell(name, r, cols["number"])
            existing = snapshot.cell(name, r, cols["name"])
            raw_text = raw.value if raw is not None else None
            number: Optional[int] = None
            if raw_text is not None and raw_text != "":
                need(raw.kind != "b" and re.fullmatch(r"[0-9]+", raw_text), f"{name}!{r} holds an invalid material number")
                number = int(raw_text)
                need(number > 0, "existing material numbers must be positive")
            existing_name = existing.value if is_text(existing) else None
            literal = re.match(r"GOFCN([0-9]+)(?:_|\Z)", existing_name) if existing_name is not None else None
            if literal:
                named = int(literal.group(1))
                if number is not None and named != number:
                    warnings.append(f"an existing number does not match its name: {name}!{r}; both are kept and avoided")
                    if named not in identifiers:
                        identifiers[named] = (name, r)
                else:
                    number = named
            if existing_name is not None and existing_name.startswith("=") and (raw_text is None or raw_text == ""):
                # An existing naming formula without a number cannot be assigned safely.
                need('"GOFCN"' not in existing_name.upper(), f"{name}!{r} has a naming formula without the number")
            if number is not None:
                seen = identifiers.get(number)
                if seen:
                    warnings.append(f"an existing number is repeated: {number}, {seen[0]}!{seen[1]} / {name}!{r}; left as it is")
                else:
                    identifiers[number] = (name, r)
    need(len(identifiers) > 0, "no GOFCN number to continue from: settle the first number")
    return identifiers


_STRING_LITERAL = '"(?:[^"]|"")*"'
_SUBSTITUTE = re.compile(r'=\s*SUBSTITUTE\s*\((.*),\s*(' + _STRING_LITERAL + r')\s*,\s*(' + _STRING_LITERAL + r')\s*\)', re.IGNORECASE)


def _unwrap_substitute(formula: str) -> tuple[str, list[tuple[str, str]]]:
    """
    The naming formula inside its SUBSTITUTE wrappers (the sheet has SUBSTITUTE(CONCATENATE(…),"__","_")), and the replacements they
    make, innermost first.
    """
    replacements: list[tuple[str, str]] = []

    def literal(token: str) -> str:
        return token[1:-1].replace('""', '"')

    body = formula
    while True:
        m = _SUBSTITUTE.fullmatch(body)
        if not m:
            break
        need(literal(m.group(2)) != "", "the naming formula's SUBSTITUTE has an empty search text, which is not supported")
        replacements.insert(0, (literal(m.group(2)), literal(m.group(3))))
        body = f"={js.trim(m.group(1))}"
    return body, replacements


def formula_refs(formula: str) -> list[str]:
    """The cells a formula refers to (outside its texts), as plain coordinates."""
    stripped = re.sub(_STRING_LITERAL, "", formula)
    return [ref.replace("$", "").upper() for ref in re.findall(r"\$?[A-Za-z]+\$?[0-9]+", stripped)]


_TOKENS = re.compile(_STRING_LITERAL + r"|\$?[A-Za-z]+\$?[0-9]+|,")


def concatenate(formula: str, row_values: dict[str, Any]) -> str:
    inner, replacements = _unwrap_substitute(formula)
    m = re.fullmatch(r"=(?:CONCATENATE|CONCAT)\((.*)\)", inner, re.IGNORECASE)
    need(m, "the naming formula is not a supported CONCATENATE/CONCAT; nothing is guessed")
    body = m.group(1)
    tokens = _TOKENS.findall(body)
    cleaned = re.sub(_STRING_LITERAL + r"|\s+", lambda x: "" if re.match(r"\s", x.group(0)) else x.group(0), body)
    need("".join(tokens) == cleaned, "the naming formula holds an unsupported expression")
    need(len(tokens) > 0 and len(tokens) % 2 == 1 and all(i % 2 == 0 or t == "," for i, t in enumerate(tokens)), "the naming formula's arguments are not supported")
    output: list[str] = []
    for i in range(0, len(tokens), 2):
        token = tokens[i]
        if token.startswith('"'):
            output.append(token[1:-1].replace('""', '"'))
        else:
            coord = token.replace("$", "").upper()
            need(coord in row_values, "the naming formula refers to a field this batch does not write, or to another row")
            value = row_values[coord]
            output.append("" if value is None or value is js.UNDEFINED else js.to_str(value))
    text = "".join(output)
    for old, replacement in replacements:
        text = text.replace(old, replacement)
    return text


def inline_options(rule: RuleData) -> list[str]:
    f = rule.formula1 or ""
    need(rule.type == "list" and f.startswith('"') and f.endswith('"') and len(f) >= 2, "a dropdown's source is not a custom list that can be kept reliably")
    return f[1:-1].replace('""', '"').split(",")


def plain_rules(ws: SheetData) -> list[RuleData]:
    """Rules a spreadsheet application would show; the x14 extension block is compared in the fingerprint but never read for options."""
    return [rule for rule in ws.rules if not rule.ext]


def options_for(ws: SheetData, col: int, last: int) -> list[str]:
    found: list[tuple[int, RuleData]] = []
    for rule in plain_rules(ws):
        for area in rule.ranges:
            if area.min_col <= col <= area.max_col and area.min_row <= last:
                found.append((min(last, area.max_row), rule))
    if not found:
        return []
    nearest = max(end for end, _ in found)
    options: list[str] = []
    for end, rule in found:
        if end == nearest:
            for value in inline_options(rule):
                if value not in options:
                    options.append(value)
    return options
