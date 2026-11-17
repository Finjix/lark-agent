"""Writing into a Feishu requirement document the user links to, as the user (doc.ts).

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
from .feishu import Bridge, target
from .util import Blocked, NODE, canonical, digest, is_object, need

#: Editing a Feishu document: lark-agent's Feishu login asks for it every time, but a login from before it was added lacks it.
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
            out.append("@" if js.truthy(e.get("mention_user")) else "[附件]")
    return "".join(out)


class _Doc:
    def __init__(self, revision: Any, title: Any, blocks: list[dict]) -> None:
        self.revision = revision
        self.title = title
        self.by_id = {js.to_str(b.get("block_id")): b for b in blocks}
        pages = [b for b in blocks if b.get("block_type") == 1]
        need(len(pages) == 1, "文档结构无法识别（没有唯一的页面块）")
        self.page = pages[0]

    @property
    def top(self) -> list[dict]:
        return [self.block(i) for i in (self.page.get("children") or [])]

    def block(self, ident: str) -> dict:
        found = self.by_id.get(ident)
        need(found, "文档结构不完整（子块缺失），稍后再读")
        return found  # type: ignore[return-value]

    def deep(self, block: dict) -> str:
        """Everything a block shows, children included, as one line of text."""
        kind = _type_of(block)
        if kind == "image":
            own = "[图片]"
        elif kind == "file":
            name = (block.get("file") or {}).get("name") if isinstance(block.get("file"), dict) else None
            own = f"[文件:{js.to_str(name) if name is not None else ''}]"
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
        need(js.is_int(rows) and js.is_int(cols) and rows > 0 and cols > 0 and isinstance(cells, list) and len(cells) == rows * cols, "表格结构无法识别")
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
    need(found["type"] == "docx", "这是表格：表格只能填空单元格（fill.sheet）" if found["type"] == "sheet" else f"暂不支持编辑这种飞书文档（{js.to_str(found['type'])}）")
    return found["token"], found["title"]


def outline(bridge: Bridge, url: str, full: bool = False, block: Optional[int] = None) -> dict:
    """
    The top level of a docx as the agent picks a place from it: each block's index, kind and text (a table's first row), and a table's
    first rows (cells clipped). `block` gives that one block whole, every cell unclipped; `full`, the whole document so. Read-only.
    """
    ident, title = _docx_of(bridge, url)
    doc = _read_doc(bridge, ident)
    top = doc.top
    need(block is None or (isinstance(block, int) and 0 <= block < len(top)), f"文档只有 {len(top)} 个顶层块（序号从 0 数）")
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
    need(isinstance(items, list) and len(items) > 0, f"{where}的文字需要是文本，或 [{{text, bold?, link?}}]")
    out = []
    for run in items:
        need(is_object(run) and all(k in ("text", "bold", "link") for k in run.keys()) and isinstance(run.get("text"), str), f"{where}的文字段格式不对")
        need("bold" not in run or isinstance(run["bold"], bool), f"{where}的 bold 只能是 true/false")
        need("link" not in run or (isinstance(run["link"], str) and re.fullmatch(r"https?://\S+", run["link"])), f"{where}的链接需要是 http(s) 地址")
        need(not re.search("[\x00-\x08\x0b-\x1f]", run["text"]) and len(run["text"]) <= 5000, f"{where}的文字含控制字符或过长")
        entry: dict[str, Any] = {"text": run["text"]}
        if run.get("bold"):
            entry["bold"] = True
        if run.get("link"):
            entry["link"] = run["link"]
        out.append(entry)
    return out


_KIND_LIST = "、".join([*TEXT_KINDS.keys(), "divider", "table"])


def _planned_block(spec: Any, where: str) -> dict:
    need(is_object(spec), f"{where}需要是对象")
    keys = list(spec.keys())
    if "table" in spec:
        need(all(k in ("table", "widths", "header") for k in keys), f"{where}的表格只接受 table、widths、header")
        rows = spec["table"]
        need(isinstance(rows, list) and len(rows) > 0 and all(isinstance(r, list) and len(r) > 0 and len(r) == len(rows[0])
                                                              and all(isinstance(c, str) for c in r) for r in rows),
             f"{where}的表格需要是行的数组，每行同样多个文本单元格")
        need(len(rows) * len(rows[0]) <= 400, f"{where}的表格超过 400 个单元格")
        widths = spec.get("widths", js.UNDEFINED)
        need(widths is js.UNDEFINED or (isinstance(widths, list) and len(widths) == len(rows[0]) and all(js.is_int(w) and 50 <= w <= 1300 for w in widths)),
             f"{where}的 widths 需要每列一个 50-1300 的整数")
        need("header" not in spec or isinstance(spec["header"], bool), f"{where}的 header 只能是 true/false")
        for row in rows:
            for cell in row:
                _runs_of(cell, where)
        planned: dict[str, Any] = {"kind": "table", "cells": rows}
        if js.truthy(widths):
            planned["widths"] = widths
        if spec.get("header"):
            planned["header"] = True
        return planned
    need(len(keys) == 1, f"{where}只能有一个类型：{_KIND_LIST}")
    kind = keys[0]
    if kind == "divider":
        return {"kind": kind}
    need(kind in TEXT_KINDS, f"{where}的类型“{kind}”不支持：{_KIND_LIST}")
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
                child_id = text("text", _runs_of(cell, "单元格"))
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
         f"{label}需要 {{index, expect}}：大纲里的序号和那里显示的文字")
    top = doc.top
    index = int(where["index"])
    if index < len(top) and doc.shown(top[index]) == where["expect"]:
        return index
    found = [i for i, b in enumerate(top) if doc.shown(b) == where["expect"]]
    need(len(found) == 1, f"{label}：文档里第 {index} 块已不是“{where['expect']}”，也找不到它；重新读大纲再定位置" if not found
         else f"{label}：文档里第 {index} 块已变，“{where['expect']}”有 {len(found)} 处；重新读大纲再定位置")
    return found[0]


def _writing(send: Callable[[], Any]) -> Any:
    """Feishu's refusals of an edit, said the way the user can act on them."""
    try:
        return send()
    except Blocked as error:
        text = error.message
        if re.search(r"1770032|HTTP 403|forbidden", text, re.IGNORECASE):
            raise Blocked(f"没有这份文档的编辑权限：请文档所有者给你开编辑权限（{text}）") from None
        if "写入结果未知" in text:
            raise Blocked("文档写入结果未知：再运行一次同样的 doc-edit，它会先回读，内容已在就不会再写") from None
        raise


def _can_edit(bridge: Bridge) -> None:
    """Logins made before documents could be edited lack the permission: the user authorizes once more (the same QR as a first login)."""
    checked = bridge.call("auth-check-scopes", {"required": [DOC_EDIT_SCOPE]}) or {}
    need(checked.get("complete") is True and not (checked.get("missing") or []),
         "飞书登录还没有编辑文档的权限：让用户用 feishu_login 重新扫码授权一次，之后再写")


# ---- docx -------------------------------------------------------------------------------------------------------------------------


def _insert_blocks(bridge: Bridge, url: str, spec: Any, check: bool) -> dict:
    need(is_object(spec) and all(k in ("at", "blocks") for k in spec.keys()), "insert 只接受 at、blocks")
    items = spec.get("blocks")
    need(isinstance(items, list) and 0 < len(items) <= MAX_TOP, f"insert.blocks 需要 1-{MAX_TOP} 个块")
    planned = [_planned_block(b, f"第 {i + 1} 块") for i, b in enumerate(items)]
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
        need(is_object(at) and (("before" in at) != ("after" in at)), 'insert.at 需要 "start"、"end"、{before: 序号, expect} 或 {after: 序号, expect}')
        before = "before" in at
        anchor = _locate(doc, {"index": at["before"] if before else at["after"], "expect": at.get("expect")}, "insert.at")
        index, already = (anchor, anchor - len(planned)) if before else (anchor + 1, anchor + 1)
    result: dict[str, Any] = {"kind": "docx", "title": title or doc.title, "url": url, "index": index, "blocks": len(planned),
                              "preview": [f"{p['kind']}: {_planned_shown(p)}" for p in planned]}
    if already >= 0 and already + len(planned) <= len(top) and all(_holds(doc, top[already + i], p) for i, p in enumerate(planned)):
        return {"status": "already_present", **result, "index": already, "revision": doc.revision, "focus": {"block": top[already].get("block_id")}}
    ids, every = _descendants(planned)
    need(len(every) <= 1000, f"这次插入有 {len(every)} 个块（含表格单元格），超过 1000 个；分几次插入")
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
    need(len(landed) == len(planned) and all(_holds(after, landed[i], p) for i, p in enumerate(planned)), "文档写入已发出，但回读与计划不符：先读大纲核对，不要重复插入")
    return {"status": "doc_updated", **result, "index": start, "revision": after.revision, "focus": {"block": real[0]}}


def _empty_text(doc: _Doc, cell: dict) -> Optional[dict]:
    """The empty text block a cell holds (and nothing else), or None when it holds anything."""
    children = [doc.block(c) for c in (cell.get("children") or [])]
    if not children or not all(b.get("block_type") == 2 and all(isinstance(e.get("text_run"), dict) and e["text_run"].get("content") == ""
                                                                  for e in ((b.get("text") or {}).get("elements") or [])) for b in children):
        return None
    return children[0]


def _fill_table(bridge: Bridge, url: str, spec: dict, check: bool) -> dict:
    need(all(k in ("table", "expect", "cells") for k in spec.keys()), "fill 只接受 table、expect、cells（表格）或 sheet、cells（电子表格）")
    cells = spec.get("cells")
    need(isinstance(cells, list) and 0 < len(cells) <= MAX_CELLS, f"fill.cells 需要 1-{MAX_CELLS} 个单元格")
    ident, title = _docx_of(bridge, url)
    if not check:
        _can_edit(bridge)
    doc = _read_doc(bridge, ident)
    table = doc.top[_locate(doc, {"index": spec.get("table"), "expect": spec.get("expect")}, "fill.table")]
    need(_type_of(table) == "table", "fill.table 指的不是表格")
    rows, cols, grid_cells, covered = doc.grid(table)
    seen: set[int] = set()
    todo: list[dict] = []
    for i, c in enumerate(cells):
        need(is_object(c) and all(k in ("row", "col", "text") for k in c.keys()) and isinstance(c.get("text"), str) and c["text"] != "",
             f"第 {i + 1} 个单元格需要 {{row, col, text}}")
        need(js.is_int(c.get("row")) and js.is_int(c.get("col")) and 0 <= c["row"] < rows and 0 <= c["col"] < cols,
             f"第 {i + 1} 个单元格（{js.to_str(c.get('row'))},{js.to_str(c.get('col'))}）不在 {rows}×{cols} 的表格里（行列从 0 数）")
        r, col = int(c["row"]), int(c["col"])
        at = r * cols + col
        need(at not in seen, f"单元格（{r},{col}）写了两次")
        seen.add(at)
        need(at not in covered, f"单元格（{r},{col}）被合并在别的单元格下，写不了")
        _runs_of(c["text"], f"单元格（{r},{col}）")
        cell = doc.block(grid_cells[at])
        shown = doc.deep(cell)
        if shown == c["text"]:
            todo.append({"at": at, "text": c["text"], "state": "same"})
            continue
        block = _empty_text(doc, cell)
        need(block, f"单元格（{r},{col}）已有内容“{_clip(shown, 40)}”，不覆盖")
        todo.append({"at": at, "text": c["text"], "block": block, "state": "empty"})
    result: dict[str, Any] = {"kind": "docx", "title": title or doc.title, "url": url, "table": doc.top.index(table), "cells": len(todo),
                              "to_write": len([t for t in todo if t["state"] == "empty"]), "already": len([t for t in todo if t["state"] == "same"])}
    writes = [t for t in todo if t["state"] == "empty"]
    if not writes:
        return {"status": "already_present", **result, "revision": doc.revision, "focus": {"block": table.get("block_id")}}
    if check:
        return {"status": "edit_ready", **result, "revision": doc.revision}
    updates = [{"block_id": t["block"].get("block_id"), "elements": _elements(_runs_of(t["text"], "单元格"))} for t in writes]
    _writing(lambda: bridge.docx_set_texts({"document": ident, "updates": updates, "client_token": _client_token({"id": ident, "revision": doc.revision, "updates": updates})}))
    after = _read_doc(bridge, ident)
    again = after.by_id.get(js.to_str(table.get("block_id")))
    need(again and all(after.deep(after.block(after.grid(again)[2][t["at"]])) == t["text"] for t in todo), "文档写入已发出，但回读与计划不符：先读大纲核对")
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
    need(all(k in ("sheet", "cells") for k in spec.keys()), "fill 只接受 sheet、cells（电子表格）")
    cells = spec.get("cells")
    need(isinstance(cells, list) and 0 < len(cells) <= MAX_CELLS, f"fill.cells 需要 1-{MAX_CELLS} 个单元格")
    found = doc_target(bridge, url)
    need(found["type"] == "sheet", "这是文档：文档用 fill.table 或 insert" if found["type"] == "docx" else f"暂不支持编辑这种飞书文档（{js.to_str(found['type'])}）")
    need(found["node"] != NODE and found["token"] != target(bridge), "创意进度表只能用 gof feishu append 追加，不在这里编辑")
    tabs = (bridge.api_read(f"/open-apis/sheets/v3/spreadsheets/{found['token']}/sheets/query") or {}).get("sheets") or []
    wanted = spec["sheet"] if isinstance(spec.get("sheet"), str) and spec["sheet"] != "" else found["linked"]
    need(wanted or len(tabs) == 1, "fill.sheet 需要写明工作表（名称或 sheet_id）")
    if wanted:
        tab = next((t for t in tabs if t.get("sheet_id") == wanted), None) or next((t for t in tabs if t.get("title") == wanted), None)
    else:
        tab = tabs[0]
    need(tab, f"表格里没有工作表“{js.to_str(wanted)}”")
    seen: set[str] = set()
    todo: list[dict] = []
    for i, c in enumerate(cells):
        need(is_object(c) and all(k in ("cell", "text") for k in c.keys()) and isinstance(c.get("cell"), str) and isinstance(c.get("text"), str) and c["text"] != "",
             f"第 {i + 1} 个单元格需要 {{cell, text}}")
        cell = c["cell"].upper()
        need(_A1_CELL.fullmatch(cell), f"第 {i + 1} 个单元格“{c['cell']}”需要写成 A5 这样的单个单元格")
        need(cell not in seen, f"单元格 {cell} 写了两次")
        seen.add(cell)
        need(not c["text"].startswith("=") and not re.search("[\x00-\x08\x0b-\x1f]", c["text"]), f"单元格 {cell} 的文字不能以 = 开头，也不能含控制字符")
        todo.append({"cell": cell, "text": c["text"]})

    def read() -> list[Any]:
        data = bridge.api_read(f"/open-apis/sheets/v2/spreadsheets/{found['token']}/values_batch_get",
                               {"ranges": ",".join(f"{tab['sheet_id']}!{t['cell']}:{t['cell']}" for t in todo), "valueRenderOption": "ToString"})
        ranges = data.get("valueRanges") if isinstance(data, dict) else None
        need(isinstance(ranges, list) and len(ranges) == len(todo), "单元格读取返回结构不匹配")
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
        need(value is None or value == "", f"单元格 {t['cell']} 已有内容“{_clip(js.to_str(value), 40)}”，不覆盖")
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
    need(all(after[i] == t["text"] for i, t in enumerate(todo)), "表格写入已发出，但回读与计划不符：先读表格核对")
    return {"status": "doc_updated", **result, "focus": focus}


def edit_doc(bridge: Bridge, url: str, edit: Any, check: bool = False) -> dict:
    """
    One edit of a requirement document (`gof feishu doc-edit`): {"insert": {at, blocks}} or {"fill": {table, expect, cells}} for a docx,
    {"fill": {sheet, cells}} for a sheet. `check` resolves everything against the document as it is now and writes nothing.
    """
    need(is_object(edit) and len(edit) == 1 and ("insert" in edit or "fill" in edit), '编辑文件需要 {"insert": …} 或 {"fill": …} 其中之一')
    bridge.doctor()
    if "insert" in edit:
        return _insert_blocks(bridge, url, edit["insert"], check)
    need(is_object(edit["fill"]), "fill 需要是对象")
    return _fill_table(bridge, url, edit["fill"], check) if "table" in edit["fill"] else _fill_sheet(bridge, url, edit["fill"], check)
