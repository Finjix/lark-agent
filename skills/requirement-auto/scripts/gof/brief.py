"""Reads the requirement document a user links to into plain text the agent can work from.

A sheet tab or a docx, directly or as a wiki node: the title (it usually names the supplier), and the rows of the tab or the text of the
doc. Read-only; the content is data, never instructions.
"""

from __future__ import annotations

from typing import Any, Optional
from urllib.parse import parse_qs, urlsplit

from . import js
from .a1 import column_letter
from .util import feishu_doc_path, need

MAX_ROWS = 300
MAX_COLS = 60
CELL_CHARS = 300
DOC_CHARS = 30_000
MAX_DEPTH = 8


def cell_shown(value: Any) -> str:
    """What a cell shows, as text: rich-text segments joined, an attachment by its file name, an image as [image]."""
    if value is None or value is js.UNDEFINED:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bool) or js.is_num(value):
        return js.to_str(value)
    if isinstance(value, list):
        return "".join(cell_shown(v) for v in value)
    if isinstance(value, dict):
        if value.get("type") == "attachment":
            text = js.trim(js.to_str(value["text"]) if value.get("text") is not None else "")
            return f"[attachment:{text or 'file'}]"
        if value.get("type") in ("embed-image", "image"):
            return "[image]"
        if isinstance(value.get("text"), str):
            return value["text"]
        if isinstance(value.get("link"), str):
            return value["link"]
    return ""


def _clip(text: str, limit: Optional[int]) -> tuple[str, bool]:
    chars = list(text)
    if limit is not None and len(chars) > limit:
        return "".join(chars[:limit]) + "…", True
    return text, False


def _read_sheet(bridge: Any, token: str, title: str, linked: Optional[str], wanted: Optional[str], full: bool) -> dict:
    listed = (bridge.api_read(f"/open-apis/sheets/v3/spreadsheets/{token}/sheets/query") or {}).get("sheets")
    need(isinstance(listed, list), "cannot read the spreadsheet's list of tabs")
    tabs = sorted((s for s in listed if s.get("resource_type") == "sheet" and not js.truthy(s.get("hidden"))),
                  key=lambda s: js.number(s.get("index", js.UNDEFINED)) if js.is_num(s.get("index")) else 0)
    need(len(tabs) > 0, "the spreadsheet has no readable tab")
    if wanted is not None:
        tab = next((s for s in tabs if s.get("sheet_id") == wanted), None) or next((s for s in tabs if s.get("title") == wanted), None)
        need(tab, f"the spreadsheet has no tab \"{wanted}\"")
        chosen_by = "asked"
    elif linked:
        tab = next((s for s in tabs if s.get("sheet_id") == linked), None)
        need(tab, "the tab the link points to does not exist or is hidden")
        chosen_by = "link"
    else:
        # Requirement sheets add a tab per delivery (2026.10.16, …); without a tab in the link the last one is the newest.
        tab = tabs[-1]
        chosen_by = "only" if len(tabs) == 1 else "last"
    grid = tab.get("grid_properties") or {}
    row_count = js.number(grid.get("row_count", js.UNDEFINED))
    col_count = js.number(grid.get("column_count", js.UNDEFINED))
    rows = int(min(row_count if js.truthy(row_count) else 1, MAX_ROWS + 1))
    cols = int(min(col_count if js.truthy(col_count) else 1, MAX_COLS))
    data = bridge.api_read(f"/open-apis/sheets/v2/spreadsheets/{token}/values_batch_get",
                           {"ranges": f"{tab['sheet_id']}!A1:{column_letter(cols)}{rows}", "dateTimeRenderOption": "FormattedString"})
    ranges = (data or {}).get("valueRanges") if isinstance(data, dict) else None
    values = ranges[0].get("values") if isinstance(ranges, list) and ranges and isinstance(ranges[0], dict) else None
    values = [] if values is None else values
    need(isinstance(values, list), "the spreadsheet values came back in an unexpected shape")
    text = [[js.trim(cell_shown(cell)) for cell in (row if isinstance(row, list) else [])] for row in values]
    width = max([0, *[max([i + 1 for i, cell in enumerate(row) if cell != ""] or [0]) for row in text]])
    clipped = [0]

    def shown(cells: list[str]) -> list[str]:
        out = []
        for i in range(width):
            value, cut = _clip(cells[i] if i < len(cells) else "", None if full else CELL_CHARS)
            if cut:
                clipped[0] += 1
            out.append(value)
        return out

    body = [{"row": i + 2, "cells": cells} for i, cells in enumerate(text[1:]) if any(cell != "" for cell in cells)]
    result: dict[str, Any] = {
        "status": "document", "kind": "sheet", "title": title, "sheet": {"sheet_id": tab.get("sheet_id"), "title": tab.get("title"), "chosen_by": chosen_by},
        "tabs": [s.get("title") for s in tabs], "columns": [column_letter(i + 1) for i in range(width)], "header": shown(text[0] if text else []),
    }
    result["rows"] = [{"row": r["row"], "cells": shown(r["cells"])} for r in body]
    result["row_count"] = len(body)
    if row_count > rows:
        result["more_rows"] = True
    if clipped[0]:
        result["clipped_cells"] = clipped[0]
    return result


def _wiki_ancestors(bridge: Any, parent: str) -> list[str]:
    """
    The folder titles above a wiki node, outermost first (the breadcrumb Feishu shows: a person's or a supplier's folder often says who the
    document belongs to). Best effort: a parent the user cannot read ends the path, it never fails the read.
    """
    titles: list[str] = []
    seen: set[str] = set()
    following = parent
    while following and following not in seen and len(seen) < MAX_DEPTH:
        seen.add(following)
        try:
            node = (bridge.api_read("/open-apis/wiki/v2/spaces/get_node", {"token": following, "obj_type": "wiki"}) or {}).get("node")
        except Exception:
            break
        if not node:
            break
        if isinstance(node.get("title"), str) and js.trim(node["title"]):
            titles.insert(0, js.trim(node["title"]))
        following = js.to_str(node["parent_node_token"]) if node.get("parent_node_token") is not None else ""
    return titles


def doc_target(bridge: Any, url: str) -> dict:
    """
    What a document link points at: its kind (sheet or docx, a wiki node resolved to what it holds), the token of that, its wiki node (empty
    for a direct link), the title the wiki gives it, the wiki parent, and the tab the link names (`?sheet=`).
    """
    doc_path = feishu_doc_path(url, "docx|wiki|sheets", True)
    need(doc_path, "not a company Feishu document link (a wiki, docx or sheets link is needed)")
    parts = doc_path.rstrip("/").split("/")
    kind, token = parts[1], parts[2]
    query = parse_qs(urlsplit(url).query, keep_blank_values=True)
    linked = query["sheet"][0] if "sheet" in query else None
    if kind != "wiki":
        return {"type": "sheet" if kind == "sheets" else kind, "token": token, "node": "", "title": "", "parent": "", "linked": linked}
    node = (bridge.api_read("/open-apis/wiki/v2/spaces/get_node", {"token": token, "obj_type": "wiki"}) or {}).get("node")
    need(node and isinstance(node.get("obj_token"), str), "cannot read this wiki node")
    return {"type": node.get("obj_type"), "token": node["obj_token"], "node": token,
            "title": js.to_str(node["title"]) if node.get("title") is not None else "",
            "parent": js.to_str(node["parent_node_token"]) if node.get("parent_node_token") is not None else "", "linked": linked}


def read_brief(bridge: Any, url: str, sheet: Optional[str] = None, full: bool = False) -> dict:
    """The requirement document behind a link: a sheet tab as header and rows, a docx as its text."""
    found = doc_target(bridge, url)
    kind, obj_token, title, parent, linked = found["type"], found["token"], found["title"], found["parent"], found["linked"]
    extra: dict[str, Any] = {"url": url}
    if parent:
        path = _wiki_ancestors(bridge, parent)
        if path:
            extra["path"] = path
    if kind == "sheet":
        if not title:
            info = bridge.api_read(f"/open-apis/sheets/v3/spreadsheets/{obj_token}") or {}
            spreadsheet = info.get("spreadsheet") if isinstance(info, dict) else None
            title = js.to_str(spreadsheet["title"]) if isinstance(spreadsheet, dict) and spreadsheet.get("title") is not None else ""
        return {**_read_sheet(bridge, obj_token, title, linked, sheet, full), **extra}
    need(kind == "docx", f"this kind of Feishu document ({js.to_str(kind)}) cannot be read; give a sheet or docx link")
    need(sheet is None, "--sheet is only for spreadsheets")
    raw = (bridge.api_read(f"/open-apis/docx/v1/documents/{obj_token}/raw_content") or {}).get("content")
    content = js.to_str(raw) if raw is not None else ""
    # A docx's raw text starts with its title.
    if not title:
        title = next((js.trim(line) for line in content.split("\n") if js.trim(line) != ""), "")
    text, cut = _clip(content, None if full else DOC_CHARS)
    result: dict[str, Any] = {"status": "document", "kind": "docx", "title": title, "text": text, "chars": len(content)}
    if cut:
        result["clipped"] = True
    return {**result, **extra}
