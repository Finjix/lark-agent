"""Writing into a Feishu requirement document the user links to, as the user.

A docx gets new blocks (headings, text, lists, tables) at a place in its top level, or text in empty table cells; a sheet tab gets text in
empty cells. Nothing that is in the document already is changed, moved or deleted: a cell that holds anything is refused, and every
write is read back. A place is given by the index the outline showed together with what the outline showed there, so a document edited in
the meantime cannot take the text somewhere else. The progress sheet has its own append (feishu append) and is never edited here.

Running the same edit again is safe: it reads the document first and reports what it finds already there instead of writing it twice.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Optional

from . import js
from .a1 import column_letter, parse_coord
from .brief import doc_target
from .feishu import Bridge, WriteUnknown, target
from .util import Blocked, NODE, canonical, digest, is_object, need

#: Editing a Feishu document: the agent's Feishu login asks for it, but a login from before it was added lacks it.
DOC_EDIT_SCOPE = "docx:document"

TYPE_NAMES: dict[int, str] = {
    1: "page", 2: "text", 3: "heading1", 4: "heading2", 5: "heading3", 6: "heading4", 7: "heading5", 8: "heading6", 9: "heading7", 10: "heading8",
    11: "heading9", 12: "bullet", 13: "ordered", 14: "code", 15: "quote", 17: "todo", 18: "bitable", 19: "callout", 22: "divider", 23: "file",
    24: "grid", 25: "grid_column", 27: "image", 30: "sheet", 31: "table", 32: "table_cell", 33: "view", 34: "quote_container",
}
#: The blocks an edit can create, by the name the edit file uses: the text kinds carry their text under that name.
TEXT_KINDS: dict[str, int] = {"text": 2, "heading1": 3, "heading2": 4, "heading3": 5, "bullet": 12, "ordered": 13}
SHOWN = 120
CELL_SHOWN = 60
#: Rows of a table the outline shows (header included); `--block` shows one block whole.
OUTLINE_ROWS = 4
MAX_TOP = 50
MAX_CELLS = 100


def _clip(text: str, limit: int) -> str:
    chars = list(text)
    return "".join(chars[:limit]) + "…" if len(chars) > limit else text


def _type_of(block: dict) -> str:
    kind = block.get("block_type")
    if js.is_int(kind) and int(kind) in TYPE_NAMES:
        return TYPE_NAMES[int(kind)]
    return f"type{js.to_str(kind)}"


def _own_text(block: dict) -> str:
    """The text of a block's own elements: text runs, mentioned documents by title, equations. Inline files and pictures count as content."""
    body = block.get(_type_of(block))
    if not is_object(body) or not isinstance(body.get("elements"), list):
        return ""
    out = []
    for e in body["elements"]:
        run = (e.get("text_run") or {}).get("content") if isinstance(e.get("text_run"), dict) else None
        doc = (e.get("mention_doc") or {}).get("title") if isinstance(e.get("mention_doc"), dict) else None
        eq = (e.get("equation") or {}).get("content") if isinstance(e.get("equation"), dict) else None
        if run is not None:
            out.append(js.to_str(run))
        elif doc is not None:
            out.append(js.to_str(doc))
        elif eq is not None:
            out.append(js.to_str(eq))
        else:
            out.append("@" if js.truthy(e.get("mention_user")) else "[attachment]")
    return "".join(out)


class _Doc:
    def __init__(self, revision: Any, title: Any, blocks: list[dict]) -> None:
        self.revision = revision
        self.title = title
        self.by_id = {js.to_str(b.get("block_id")): b for b in blocks}
        pages = [b for b in blocks if b.get("block_type") == 1]
        need(len(pages) == 1, "the document's structure is not recognized (no single page block)")
        self.page = pages[0]

    @property
    def top(self) -> list[dict]:
        return [self.block(i) for i in (self.page.get("children") or [])]

    def block(self, ident: str) -> dict:
        found = self.by_id.get(ident)
        need(found, "the document's structure is incomplete (a child block is missing): read it again a little later")
        return found  # type: ignore[return-value]

    def deep(self, block: dict) -> str:
        """Everything a block shows, children included, as one line of text."""
        kind = _type_of(block)
        if kind == "image":
            own = "[image]"
        elif kind == "file":
            name = (block.get("file") or {}).get("name") if isinstance(block.get("file"), dict) else None
            own = f"[file:{js.to_str(name) if name is not None else ''}]"
        elif kind == "table":
            own = ""
        else:
            own = _own_text(block)
        if kind == "table":
            inner = " / ".join(" | ".join(c if c is not None else "" for c in r) for r in self.rows(block))
        else:
            inner = " ".join(t for t in (self.deep(self.block(i)) for i in (block.get("children") or [])) if t)
        return " ".join(t for t in (own, inner) if t)

    def rows(self, table: dict) -> list[list[Optional[str]]]:
        """A table's cells by row, each as the text it shows; a cell hidden under a merged one is None."""
        rows, cols, cells, covered = self.grid(table)
        return [[None if r * cols + c in covered else self.deep(self.block(cells[r * cols + c])) for c in range(cols)] for r in range(rows)]

    def grid(self, table: dict) -> tuple[int, int, list[str], set[int]]:
        inner = table.get("table") if isinstance(table.get("table"), dict) else {}
        prop = inner.get("property") if isinstance(inner.get("property"), dict) else {}
        rows = js.number(prop.get("row_size", js.UNDEFINED))
        cols = js.number(prop.get("column_size", js.UNDEFINED))
        cells = inner.get("cells") if inner.get("cells") is not None else (table.get("children") if table.get("children") is not None else [])
        need(js.is_int(rows) and js.is_int(cols) and rows > 0 and cols > 0 and isinstance(cells, list) and len(cells) == rows * cols, "the table's structure is not recognized")
        rows, cols = int(rows), int(cols)
        covered: set[int] = set()
        for i, span in enumerate(prop.get("merge_info") or []):
            r, c = i // cols, i % cols
            row_span = span.get("row_span") if span.get("row_span") is not None else 1
            col_span = span.get("col_span") if span.get("col_span") is not None else 1
            for dr in range(int(row_span)):
                for dc in range(int(col_span)):
                    if dr or dc:
                        covered.add((r + dr) * cols + c + dc)
        return rows, cols, cells, covered

    def shown(self, block: dict) -> str:
        """What the outline shows for a top-level block, and what an edit names it by: its text, or a table's first row."""
        if _type_of(block) == "table":
            return _clip(" | ".join(c if c is not None else "" for c in self.rows(block)[0]), SHOWN)
        return _clip(self.deep(block), SHOWN)


def _read_doc(bridge: Bridge, ident: str) -> _Doc:
    read = bridge.docx_blocks(ident) or {}
    return _Doc(read.get("revision"), read.get("title"), read.get("blocks") or [])


def _docx_of(bridge: Bridge, url: str) -> tuple[str, str]:
    """A docx link resolved to its document; a sheet or another kind is refused for what it is."""
    found = doc_target(bridge, url)
    need(found["type"] == "docx", "this is a spreadsheet: a spreadsheet only takes text in empty cells (fill.sheet)" if found["type"] == "sheet" else f"this kind of Feishu document ({js.to_str(found['type'])}) cannot be edited")
    return found["token"], found["title"]


def outline(bridge: Bridge, url: str, full: bool = False, block: Optional[int] = None) -> dict:
    """
    The top level of a docx as the agent picks a place from it: each block's index, kind and text (a table's first row), and a table's
    first rows (cells clipped). `block` gives that one block whole, every cell unclipped; `full`, the whole document so. Read-only.
    """
    ident, title = _docx_of(bridge, url)
    doc = _read_doc(bridge, ident)
    top = doc.top
    need(block is None or (isinstance(block, int) and 0 <= block < len(top)), f"the document has only {len(top)} top-level blocks (counted from 0)")
    whole = full or block is not None
    blocks = []
    for index, b in enumerate(top):
        if block is not None and index != block:
            continue
        entry: dict[str, Any] = {"index": index, "type": _type_of(b), "text": doc.shown(b)}
        if entry["type"] == "table":
            rows = doc.rows(b)
            shown = rows if whole else [[c if c is None else _clip(c, CELL_SHOWN) for c in r] for r in rows[:OUTLINE_ROWS]]
            entry.update({"rows": len(rows), "cols": len(rows[0]), "cells": shown})
            if len(shown) < len(rows):
                entry["more_rows"] = len(rows) - len(shown)
        blocks.append(entry)
    return {"status": "outline", "kind": "docx", "title": title or doc.title, "url": url, "revision": doc.revision, "top_blocks": len(top), "blocks": blocks}


# ---- the edit file ----------------------------------------------------------------------------------------------------------------


def _runs_of(value: Any, where: str) -> list[dict]:
    items = [{"text": value}] if isinstance(value, str) else value
    need(isinstance(items, list) and len(items) > 0, f"{where}: the text must be a string or [{{text, bold?, link?}}]")
    out = []
    for run in items:
        need(is_object(run) and all(k in ("text", "bold", "link") for k in run.keys()) and isinstance(run.get("text"), str), f"{where}: a text run is malformed")
        need("bold" not in run or isinstance(run["bold"], bool), f"{where}: bold is true or false")
        need("link" not in run or (isinstance(run["link"], str) and re.fullmatch(r"https?://\S+", run["link"])), f"{where}: a link must be an http(s) address")
        need(not re.search("[\x00-\x08\x0b-\x1f]", run["text"]) and len(run["text"]) <= 5000, f"{where}: the text holds control characters or is too long")
        entry: dict[str, Any] = {"text": run["text"]}
        if run.get("bold"):
            entry["bold"] = True
        if run.get("link"):
            entry["link"] = run["link"]
        out.append(entry)
    return out


_KIND_LIST = ", ".join([*TEXT_KINDS.keys(), "divider", "table"])


def _planned_block(spec: Any, where: str) -> dict:
    need(is_object(spec), f"{where} must be an object")
    keys = list(spec.keys())
    if "table" in spec:
        need(all(k in ("table", "widths", "header") for k in keys), f"{where}: a table takes only table, widths, header")
        rows = spec["table"]
        need(isinstance(rows, list) and len(rows) > 0 and all(isinstance(r, list) and len(r) > 0 and len(r) == len(rows[0])
                                                              and all(isinstance(c, str) for c in r) for r in rows),
             f"{where}: a table is a list of rows, each with the same number of text cells")
        need(len(rows) * len(rows[0]) <= 400, f"{where}: the table has more than 400 cells")
        widths = spec.get("widths", js.UNDEFINED)
        need(widths is js.UNDEFINED or (isinstance(widths, list) and len(widths) == len(rows[0]) and all(js.is_int(w) and 50 <= w <= 1300 for w in widths)),
             f"{where}: widths needs one whole number from 50 to 1300 per column")
        need("header" not in spec or isinstance(spec["header"], bool), f"{where}: header is true or false")
        for row in rows:
            for cell in row:
                _runs_of(cell, where)
        planned: dict[str, Any] = {"kind": "table", "cells": rows}
        if js.truthy(widths):
            planned["widths"] = widths
        if spec.get("header"):
            planned["header"] = True
        return planned
    need(len(keys) == 1, f"{where} must have exactly one kind: {_KIND_LIST}")
    kind = keys[0]
    if kind == "divider":
        return {"kind": kind}
    need(kind in TEXT_KINDS, f"{where}: the kind \"{kind}\" is not supported: {_KIND_LIST}")
    return {"kind": kind, "runs": _runs_of(spec[kind], where)}


def _planned_shown(p: dict) -> str:
    """What the outline will show for a planned block once it is in: compared with the document to tell whether it is there already."""
    if p["kind"] == "table":
        return _clip(" | ".join(p["cells"][0]), SHOWN)
    return _clip("".join(r["text"] for r in p.get("runs") or []), SHOWN)


def _holds(doc: _Doc, block: dict, p: dict) -> bool:
    """Does a block of the document show exactly a planned one: the kind, the text, and a table cell by cell."""
    if _type_of(block) != p["kind"] or doc.shown(block) != _planned_shown(p):
        return False
    return p["kind"] != "table" or canonical(doc.rows(block)) == canonical(p["cells"])


def _elements(runs: list[dict]) -> list[dict]:
    out = []
    for r in runs:
        run: dict[str, Any] = {"content": r["text"]}
        if r.get("bold") or r.get("link"):
            style: dict[str, Any] = {}
            if r.get("bold"):
                style["bold"] = True
            if r.get("link"):
                style["link"] = {"url": js.encode_uri_component(r["link"])}
            run["text_element_style"] = style
        out.append({"text_run": run})
    return out


def _descendants(planned: list[dict]) -> tuple[list[str], list[dict]]:
    """The descendants request for planned blocks: temporary IDs, tables with a text block in every cell."""
    counter = [0]
    every: list[dict] = []

    def following(kind: str) -> str:
        counter[0] += 1
        return f"{kind}_{counter[0]}"

    def text(kind: str, runs: list[dict]) -> str:
        ident = following(kind)
        every.append({"block_id": ident, "block_type": TEXT_KINDS[kind], kind: {"elements": _elements(runs), "style": {}}, "children": []})
        return ident

    top: list[str] = []
    for p in planned:
        if p["kind"] == "divider":
            ident = following("divider")
            every.append({"block_id": ident, "block_type": 22, "divider": {}, "children": []})
            top.append(ident)
            continue
        if p["kind"] != "table":
            top.append(text(p["kind"], p["runs"]))
            continue
        ident = following("table")
        prop: dict[str, Any] = {"row_size": len(p["cells"]), "column_size": len(p["cells"][0])}
        if p.get("widths"):
            prop["column_width"] = p["widths"]
        if p.get("header"):
            prop["header_row"] = True
        entry: dict[str, Any] = {"block_id": ident, "block_type": 31, "children": [], "table": {"property": prop}}
        every.append(entry)
        for row in p["cells"]:
            for cell in row:
                cell_id = following("cell")
                entry["children"].append(cell_id)
                # As in the original: the cell's text block is listed before the cell itself.
                child_id = text("text", _runs_of(cell, "cell"))
                every.append({"block_id": cell_id, "block_type": 32, "table_cell": {}, "children": [child_id]})
        top.append(ident)
    return top, every


def _client_token(value: Any) -> str:
    """The same request made again gets the same identifier, which Feishu takes as the same write."""
    h = digest(value)
    return f"{h[:8]}-{h[8:12]}-4{h[13:16]}-a{h[17:20]}-{h[20:32]}"


def _locate(doc: _Doc, where: Any, label: str) -> int:
    """A top-level block an edit names: by the index the outline showed, with the text it showed; if the document moved, the one block with that text."""
    need(is_object(where) and js.is_int(where.get("index")) and where["index"] >= 0 and isinstance(where.get("expect"), str),
         f"{label} needs {{index, expect}}: the index in the outline and the text shown there")
    top = doc.top
    index = int(where["index"])
    if index < len(top) and doc.shown(top[index]) == where["expect"]:
        return index
    found = [i for i, b in enumerate(top) if doc.shown(b) == where["expect"]]
    need(len(found) == 1, f"{label}: block {index} of the document is no longer \"{where['expect']}\" and that text is nowhere else; read the outline again and place it anew" if not found
         else f"{label}: block {index} of the document changed and \"{where['expect']}\" is in {len(found)} places; read the outline again and place it anew")
    return found[0]


def _writing(send: Callable[[], Any]) -> Any:
    """Feishu's refusals of an edit, said the way the user can act on them."""
    try:
        return send()
    except Blocked as error:
        text = error.message
        if re.search(r"1770032|HTTP 403|forbidden", text, re.IGNORECASE):
            raise Blocked(f"no edit access to this document: the user asks its owner for edit access ({text})") from None
        if isinstance(error, WriteUnknown):
            raise Blocked("the document write's result is unknown: run the same doc-edit again; it reads first and writes nothing that is in place") from None
        raise


def _can_edit(bridge: Bridge) -> None:
    """Logins made before documents could be edited lack the permission: the user authorizes once more (the same QR as a first login)."""
    checked = bridge.call("auth-check-scopes", {"required": [DOC_EDIT_SCOPE]}) or {}
    need(checked.get("complete") is True and not (checked.get("missing") or []),
         "the Feishu login lacks the permission to edit documents (docx:document): log the user in to Feishu again (feishu_login with extra_scopes [docx:document]), then run this again")


# ---- docx -------------------------------------------------------------------------------------------------------------------------


def _insert_blocks(bridge: Bridge, url: str, spec: Any, check: bool) -> dict:
    need(is_object(spec) and all(k in ("at", "blocks") for k in spec.keys()), "insert takes only at, blocks")
    items = spec.get("blocks")
    need(isinstance(items, list) and 0 < len(items) <= MAX_TOP, f"insert.blocks needs 1-{MAX_TOP} blocks")
    planned = [_planned_block(b, f"block {i + 1}") for i, b in enumerate(items)]
    ident, title = _docx_of(bridge, url)
    if not check:
        _can_edit(bridge)
    doc = _read_doc(bridge, ident)
    top = doc.top
    at = spec.get("at")
    # Where the blocks go, and where they are if they went in already (an edit run a second time): before the anchor, after it, first, last.
    if at == "start":
        index, already = 0, 0
    elif at == "end":
        index, already = len(top), len(top) - len(planned)
    else:
        need(is_object(at) and (("before" in at) != ("after" in at)), 'insert.at is "start", "end", {before: <index>, expect} or {after: <index>, expect}')
        before = "before" in at
        anchor = _locate(doc, {"index": at["before"] if before else at["after"], "expect": at.get("expect")}, "insert.at")
        index, already = (anchor, anchor - len(planned)) if before else (anchor + 1, anchor + 1)
    result: dict[str, Any] = {"kind": "docx", "title": title or doc.title, "url": url, "index": index, "blocks": len(planned),
                              "preview": [f"{p['kind']}: {_planned_shown(p)}" for p in planned]}
    if already >= 0 and already + len(planned) <= len(top) and all(_holds(doc, top[already + i], p) for i, p in enumerate(planned)):
        return {"status": "already_present", **result, "index": already, "revision": doc.revision, "focus": {"block": top[already].get("block_id")}}
    ids, every = _descendants(planned)
    need(len(every) <= 1000, f"this insert has {len(every)} blocks (table cells included), more than 1000: split it")
    if check:
        return {"status": "edit_ready", **result, "revision": doc.revision}
    reply = _writing(lambda: bridge.docx_insert({"document": ident, "parent": doc.page.get("block_id"), "index": index, "children_id": ids,
                                                 "descendants": every, "client_token": _client_token({"id": ident, "revision": doc.revision,
                                                                                                       "index": index, "all": every})}))
    # Read back: the new blocks are next to each other in the top level, in order, showing what was planned (tables cell by cell).
    relations = {r.get("temporary_block_id"): r.get("block_id") for r in ((reply or {}).get("block_id_relations") or [])}
    real = [relations.get(t) for t in ids]
    after = _read_doc(bridge, ident)
    children = after.page.get("children") or []
    start = children.index(real[0]) if all(real) and real[0] in children else -1
    landed = ([after.block(c) for c in children[start:start + len(ids)]]
              if start >= 0 and all(start + i < len(children) and children[start + i] == r for i, r in enumerate(real)) else [])
    need(len(landed) == len(planned) and all(_holds(after, landed[i], p) for i, p in enumerate(planned)), "the document write was sent but the read-back differs from the plan: read the outline to check; do not insert again")
    return {"status": "doc_updated", **result, "index": start, "revision": after.revision, "focus": {"block": real[0]}}


def _empty_text(doc: _Doc, cell: dict) -> Optional[dict]:
    """The empty text block a cell holds (and nothing else), or None when it holds anything."""
    children = [doc.block(c) for c in (cell.get("children") or [])]
    if not children or not all(b.get("block_type") == 2 and all(isinstance(e.get("text_run"), dict) and e["text_run"].get("content") == ""
                                                                  for e in ((b.get("text") or {}).get("elements") or [])) for b in children):
        return None
    return children[0]


def _fill_table(bridge: Bridge, url: str, spec: dict, check: bool) -> dict:
    need(all(k in ("table", "expect", "cells") for k in spec.keys()), "fill takes only table, expect, cells (a docx table) or sheet, cells (a spreadsheet)")
    cells = spec.get("cells")
    need(isinstance(cells, list) and 0 < len(cells) <= MAX_CELLS, f"fill.cells needs 1-{MAX_CELLS} cells")
    ident, title = _docx_of(bridge, url)
    if not check:
        _can_edit(bridge)
    doc = _read_doc(bridge, ident)
    table = doc.top[_locate(doc, {"index": spec.get("table"), "expect": spec.get("expect")}, "fill.table")]
    need(_type_of(table) == "table", "fill.table does not point at a table")
    rows, cols, grid_cells, covered = doc.grid(table)
    seen: set[int] = set()
    todo: list[dict] = []
    for i, c in enumerate(cells):
        need(is_object(c) and all(k in ("row", "col", "text") for k in c.keys()) and isinstance(c.get("text"), str) and c["text"] != "",
             f"cell {i + 1} needs {{row, col, text}}")
        need(js.is_int(c.get("row")) and js.is_int(c.get("col")) and 0 <= c["row"] < rows and 0 <= c["col"] < cols,
             f"cell {i + 1} ({js.to_str(c.get('row'))},{js.to_str(c.get('col'))}) is outside the {rows}x{cols} table (rows and columns from 0)")
        r, col = int(c["row"]), int(c["col"])
        at = r * cols + col
        need(at not in seen, f"cell ({r},{col}) is written twice")
        seen.add(at)
        need(at not in covered, f"cell ({r},{col}) is merged under another cell and cannot be written")
        _runs_of(c["text"], f"cell ({r},{col})")
        cell = doc.block(grid_cells[at])
        shown = doc.deep(cell)
        if shown == c["text"]:
            todo.append({"at": at, "text": c["text"], "state": "same"})
            continue
        block = _empty_text(doc, cell)
        need(block, f"cell ({r},{col}) holds \"{_clip(shown, 40)}\" already and is not overwritten")
        todo.append({"at": at, "text": c["text"], "block": block, "state": "empty"})
    result: dict[str, Any] = {"kind": "docx", "title": title or doc.title, "url": url, "table": doc.top.index(table), "cells": len(todo),
                              "to_write": len([t for t in todo if t["state"] == "empty"]), "already": len([t for t in todo if t["state"] == "same"])}
    writes = [t for t in todo if t["state"] == "empty"]
    if not writes:
        return {"status": "already_present", **result, "revision": doc.revision, "focus": {"block": table.get("block_id")}}
    if check:
        return {"status": "edit_ready", **result, "revision": doc.revision}
    updates = [{"block_id": t["block"].get("block_id"), "elements": _elements(_runs_of(t["text"], "cell"))} for t in writes]
    _writing(lambda: bridge.docx_set_texts({"document": ident, "updates": updates, "client_token": _client_token({"id": ident, "revision": doc.revision, "updates": updates})}))
    after = _read_doc(bridge, ident)
    again = after.by_id.get(js.to_str(table.get("block_id")))
    need(again and all(after.deep(after.block(after.grid(again)[2][t["at"]])) == t["text"] for t in todo), "the document write was sent but the read-back differs from the plan: read the outline to check")
    return {"status": "doc_updated", **result, "revision": after.revision, "focus": {"block": table.get("block_id")}}


# ---- sheet ------------------------------------------------------------------------------------------------------------------------

_A1_CELL = re.compile(r"([A-Z]{1,3})([1-9][0-9]{0,6})")


def _span(cells: list[str]) -> str:
    """The smallest range holding all the cells (A9, C12 → A9:C12)."""
    at = [parse_coord(c) for c in cells]
    cols = [a[0] for a in at]
    rows = [a[1] for a in at]
    return f"{column_letter(min(cols))}{min(rows)}:{column_letter(max(cols))}{max(rows)}"


def _fill_sheet(bridge: Bridge, url: str, spec: dict, check: bool) -> dict:
    need(all(k in ("sheet", "cells") for k in spec.keys()), "fill takes only sheet, cells (a spreadsheet)")
    cells = spec.get("cells")
    need(isinstance(cells, list) and 0 < len(cells) <= MAX_CELLS, f"fill.cells needs 1-{MAX_CELLS} cells")
    found = doc_target(bridge, url)
    need(found["type"] == "sheet", "this is a docx: use fill.table or insert" if found["type"] == "docx" else f"this kind of Feishu document ({js.to_str(found['type'])}) cannot be edited")
    need(found["node"] != NODE and found["token"] != target(bridge), "the progress sheet is written only by gof feishu append, never edited here")
    tabs = (bridge.api_read(f"/open-apis/sheets/v3/spreadsheets/{found['token']}/sheets/query") or {}).get("sheets") or []
    wanted = spec["sheet"] if isinstance(spec.get("sheet"), str) and spec["sheet"] != "" else found["linked"]
    need(wanted or len(tabs) == 1, "fill.sheet must name the tab (title or sheet_id)")
    if wanted:
        tab = next((t for t in tabs if t.get("sheet_id") == wanted), None) or next((t for t in tabs if t.get("title") == wanted), None)
    else:
        tab = tabs[0]
    need(tab, f"the spreadsheet has no tab \"{js.to_str(wanted)}\"")
    seen: set[str] = set()
    todo: list[dict] = []
    for i, c in enumerate(cells):
        need(is_object(c) and all(k in ("cell", "text") for k in c.keys()) and isinstance(c.get("cell"), str) and isinstance(c.get("text"), str) and c["text"] != "",
             f"cell {i + 1} needs {{cell, text}}")
        cell = c["cell"].upper()
        need(_A1_CELL.fullmatch(cell), f"cell {i + 1} \"{c['cell']}\" must be one cell written like A5")
        need(cell not in seen, f"cell {cell} is written twice")
        seen.add(cell)
        need(not c["text"].startswith("=") and not re.search("[\x00-\x08\x0b-\x1f]", c["text"]), f"the text of cell {cell} cannot start with = or hold control characters")
        todo.append({"cell": cell, "text": c["text"]})

    def read() -> list[Any]:
        data = bridge.api_read(f"/open-apis/sheets/v2/spreadsheets/{found['token']}/values_batch_get",
                               {"ranges": ",".join(f"{tab['sheet_id']}!{t['cell']}:{t['cell']}" for t in todo), "valueRenderOption": "ToString"})
        ranges = data.get("valueRanges") if isinstance(data, dict) else None
        need(isinstance(ranges, list) and len(ranges) == len(todo), "the cells came back in an unexpected shape")
        out = []
        for r in ranges:
            values = r.get("values") if isinstance(r, dict) else None
            first = values[0] if isinstance(values, list) and values else None
            out.append(first[0] if isinstance(first, list) and first else None)
        return out

    before = read()
    states = []
    for i, t in enumerate(todo):
        value = before[i]
        if value == t["text"]:
            states.append("same")
            continue
        need(value is None or value == "", f"cell {t['cell']} holds \"{_clip(js.to_str(value), 40)}\" already and is not overwritten")
        states.append("empty")
    writes = [t for i, t in enumerate(todo) if states[i] == "empty"]
    result: dict[str, Any] = {"kind": "sheet", "title": found["title"], "url": url, "sheet": tab.get("title"), "cells": len(todo), "to_write": len(writes),
                              "already": len(todo) - len(writes)}
    # Where a picture of the edit opens the sheet: its tab, on the cells written.
    focus = {"sheet_id": tab.get("sheet_id"), "range": _span([t["cell"] for t in todo])}
    if not writes:
        return {"status": "already_present", **result, "focus": focus}
    if check:
        return {"status": "edit_ready", **result}
    # Each cell its own range, sent with allow_overwrite:false: Feishu itself refuses a cell that has been filled meanwhile.
    _writing(lambda: bridge.cells_set(found["token"], [{"sheet_id": tab.get("sheet_id"), "range": f"{t['cell']}:{t['cell']}", "cells": [[{"value": t["text"]}]],
                                                         "allow_overwrite": False} for t in writes]))
    after = read()
    need(all(after[i] == t["text"] for i, t in enumerate(todo)), "the spreadsheet write was sent but the read-back differs from the plan: read the sheet to check")
    return {"status": "doc_updated", **result, "focus": focus}


def edit_doc(bridge: Bridge, url: str, edit: Any, check: bool = False) -> dict:
    """
    One edit of a requirement document (`gof feishu doc-edit`): {"insert": {at, blocks}} or {"fill": {table, expect, cells}} for a docx,
    {"fill": {sheet, cells}} for a sheet. `check` resolves everything against the document as it is now and writes nothing.
    """
    need(is_object(edit) and len(edit) == 1 and ("insert" in edit or "fill" in edit), 'the edit file holds exactly one of {"insert": …} or {"fill": …}')
    bridge.doctor()
    if "insert" in edit:
        return _insert_blocks(bridge, url, edit["insert"], check)
    need(is_object(edit["fill"]), "fill must be an object")
    return _fill_table(bridge, url, edit["fill"], check) if "table" in edit["fill"] else _fill_sheet(bridge, url, edit["fill"], check)
