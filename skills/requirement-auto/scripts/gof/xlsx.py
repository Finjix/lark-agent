"""A read-only model of an .xlsx.

It holds exactly what the append checks compare: cell values and kinds, the content of cell styles, comments and links, merged ranges,
row/column settings, the auto filter, sheet protection and the dropdown rules. Nothing is ever written back, so there is no round trip
that could drop or change a part of the workbook (which is why the raw XML is read here instead of going through openpyxl).
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from . import js
from .a1 import CellRange, column_letter, coord_of, parse_coord, translate_formula
from .util import canonical, digest, need
from .xmlparse import XmlNode, child, children_of, parse_xml, to_data
from .unzip import ZipReader


@dataclass
class CellData:
    row: int
    col: int
    coord: str
    #: The text as stored: strings, "=formula", or the number/boolean/error exactly as written in the file.
    value: Optional[str] = None
    kind: Optional[str] = None
    #: Digest of the style's content (font, fill, border, alignment, protection, number format); None for the default style.
    style: Optional[str] = None
    comment: Optional[list] = None
    link: Optional[list] = None


@dataclass
class RuleData:
    type: Optional[str]
    #: Everything about the rule except where it applies.
    signature: str
    ranges: list[CellRange]
    formula1: Optional[str]
    #: Declared in the x14 extension block instead of the main dataValidations element.
    ext: bool


@dataclass
class SheetData:
    name: str
    state: str
    cells: dict[str, CellData]
    max_row: int
    max_col: int
    merges: list[str]
    rows: dict[str, list]
    columns: dict[str, list]
    filter: Optional[dict]
    protection: Optional[str]
    protected_sheet: bool
    rules: list[RuleData] = field(default_factory=list)


@dataclass
class WorkbookData:
    sheets: list[SheetData]


MAX_CELLS = 4_000_000


def _truthy(value: Optional[str]) -> bool:
    return value in ("1", "true")


_ESCAPED = re.compile(r"_x([0-9A-Fa-f]{4})_")


def _unescape_text(text: str) -> str:
    return _ESCAPED.sub(lambda m: chr(int(m.group(1), 16)), text)


def _rich_text(node: Optional[XmlNode]) -> str:
    if node is None:
        return ""
    out = ""
    for c in node.children:
        if c.name == "t":
            out += c.text
        elif c.name == "r":
            out += "".join(t.text for t in c.children if t.name == "t")
    return _unescape_text(out)


def _relationships(zip_: ZipReader, file: str) -> dict[str, dict]:
    result: dict[str, dict] = {}
    if not zip_.has(file):
        return result
    for rel in children_of(parse_xml(zip_.text(file)), "Relationship"):
        if rel.attrs.get("Id") and rel.attrs.get("Target"):
            result[rel.attrs["Id"]] = {"target": rel.attrs["Target"], "external": rel.attrs.get("TargetMode") == "External",
                                       "type": rel.attrs.get("Type", "")}
    return result


def _resolve(base_dir: str, target: str) -> str:
    if target.startswith("/"):
        return target[1:]
    return posixpath.normpath(posixpath.join(base_dir, target))


class _Styles:
    def __init__(self, xml: Optional[str]) -> None:
        self.fonts: list[XmlNode] = []
        self.fills: list[XmlNode] = []
        self.borders: list[XmlNode] = []
        self.xfs: list[XmlNode] = []
        self.formats: dict[str, str] = {}
        self.cache: dict[int, str] = {}
        if xml is not None:
            root = parse_xml(xml)
            for f in children_of(child(root, "numFmts"), "numFmt"):
                self.formats[f.attrs.get("numFmtId", "")] = f.attrs.get("formatCode", "")
            self.fonts = (child(root, "fonts") or XmlNode("")).children
            self.fills = (child(root, "fills") or XmlNode("")).children
            self.borders = (child(root, "borders") or XmlNode("")).children
            self.xfs = (child(root, "cellXfs") or XmlNode("")).children
        self.base = self._compute(0)

    def _compute(self, index: int) -> str:
        xf = self.xfs[index] if 0 <= index < len(self.xfs) else None
        if xf is None:
            return digest(None)
        num_fmt_id = xf.attrs.get("numFmtId", "0")
        return digest({
            "font": to_data(js.index_of(self.fonts, js.number(xf.attrs.get("fontId", 0)))),
            "fill": to_data(js.index_of(self.fills, js.number(xf.attrs.get("fillId", 0)))),
            "border": to_data(js.index_of(self.borders, js.number(xf.attrs.get("borderId", 0)))),
            "alignment": to_data(child(xf, "alignment")),
            "protection": to_data(child(xf, "protection")),
            "numFmt": self.formats[num_fmt_id] if num_fmt_id in self.formats else f"builtin:{num_fmt_id}",
        })

    def signature(self, index: Any) -> Optional[str]:
        """The content signature of cell format `index`, or None when it is the same as the default format."""
        if index is None or not js.is_int(index) or index < 0:
            return None
        index = int(index)
        sig = self.cache.get(index)
        if sig is None:
            sig = self._compute(index)
            self.cache[index] = sig
        return None if sig == self.base else sig


def _dimension_style(styles: _Styles, value: Optional[str]) -> Optional[str]:
    return None if value is None else styles.signature(js.number(value))


def _read_rule(node: XmlNode, ext: bool) -> RuleData:
    attrs = {k: v for k, v in node.attrs.items() if k != "sqref"}
    attr_ref = node.attrs.get("sqref")
    f1_node = child(child(node, "formula1"), "f") if ext else child(node, "formula1")
    f2_node = child(child(node, "formula2"), "f") if ext else child(node, "formula2")
    f1 = f1_node.text if f1_node is not None else None
    f2 = f2_node.text if f2_node is not None else None
    if ext:
        sq = child(node, "sqref")
        sqref = sq.text if sq is not None else ""
    else:
        sqref = attr_ref if attr_ref is not None else ""
    ranges = [CellRange(part) for part in re.split(r"\s+", sqref) if part]
    return RuleData(node.attrs.get("type"), canonical({"ext": ext, "attrs": attrs, "formula1": f1, "formula2": f2}), ranges, f1, ext)


_ROW_KEYS = ("ht", "hidden", "outlineLevel", "collapsed", "customHeight", "customFormat", "s", "thickBot", "thickTop")


def _read_sheet(zip_: ZipReader, file: str, name: str, state: str, styles: _Styles, shared: list[str]) -> SheetData:
    root = parse_xml(zip_.text(file))
    need(root.name == "worksheet", f"{name} is not a valid worksheet")
    rels = _relationships(zip_, posixpath.join(posixpath.dirname(file), "_rels", f"{posixpath.basename(file)}.rels"))
    directory = posixpath.dirname(file)
    cells: dict[str, CellData] = {}
    shared_formulas: dict[str, tuple[str, str]] = {}
    rows: dict[str, list] = {}
    bounds = {"row": 0, "col": 0}

    def ensure(coord: str) -> CellData:
        cell = cells.get(coord)
        if cell is None:
            col, row = parse_coord(coord)
            cell = CellData(row, col, coord)
            cells[coord] = cell
            bounds["row"] = max(bounds["row"], row)
            bounds["col"] = max(bounds["col"], col)
        return cell

    row_cursor = 0
    for row_node in children_of(child(root, "sheetData"), "row"):
        a = row_node.attrs
        row_index = js.number(a["r"]) if "r" in a else row_cursor + 1
        need(js.is_int(row_index) and 1 <= row_index <= 1048576, f"{name} has an invalid row number")
        row_index = int(row_index)
        row_cursor = row_index
        if any(k in a for k in _ROW_KEYS):
            rows[str(row_index)] = [a.get("ht"), _truthy(a.get("hidden")), js.num(a.get("outlineLevel", 0)), _truthy(a.get("collapsed")),
                                    _dimension_style(styles, a.get("s")) if _truthy(a.get("customFormat")) else None]
        col_cursor = 0
        for c in children_of(row_node, "c"):
            coord = c.attrs["r"] if "r" in c.attrs else coord_of(col_cursor + 1, row_index)
            cell = ensure(coord)
            col_cursor = cell.col
            cell.style = styles.signature(None if "s" not in c.attrs else js.number(c.attrs["s"]))
            f = child(c, "f")
            v_node = child(c, "v")
            v = v_node.text if v_node is not None else None
            t = c.attrs.get("t", "n")
            if f is not None and (f.text != "" or f.attrs.get("t") == "shared"):
                text = f.text
                if f.attrs.get("t") == "shared":
                    key = f.attrs.get("si", "")
                    if text != "":
                        shared_formulas[key] = (coord, text)
                    else:
                        master = shared_formulas.get(key)
                        need(master, f"the shared formula of {name}!{coord} has no master cell")
                        text = translate_formula(master[1], master[0], coord)
                cell.value = f"={text}"
                cell.kind = "f"
            elif t == "s":
                if v is not None and v != "":
                    text = js.index_of(shared, js.number(v))
                    need(text is not None, f"{name}!{coord} refers to a shared string that does not exist")
                    cell.value = text
                    cell.kind = "s"
            elif t == "inlineStr":
                cell.value = _rich_text(child(c, "is"))
                cell.kind = "s"
            elif t == "str":
                if v is not None:
                    cell.value = _unescape_text(v)
                    cell.kind = "s"
            elif t in ("b", "e", "d"):
                if v is not None and v != "":
                    cell.value = v
                    cell.kind = t
            elif v is not None and v != "":
                cell.value = js.trim(v)
                cell.kind = "n"

    columns: dict[str, list] = {}
    for col in children_of(child(root, "cols"), "col"):
        lo = js.number(col.attrs.get("min", js.UNDEFINED))
        hi = js.number(col.attrs.get("max", js.UNDEFINED))
        need(js.is_int(lo) and js.is_int(hi) and lo >= 1 and hi >= lo and hi <= 16384, f"{name} has invalid column settings")
        columns[column_letter(int(lo))] = [int(lo), int(hi), col.attrs.get("width"), _truthy(col.attrs.get("hidden")),
                                           js.num(col.attrs.get("outlineLevel", 0)), _truthy(col.attrs.get("collapsed")),
                                           _dimension_style(styles, col.attrs.get("style"))]

    merges = sorted((str(CellRange(m.attrs.get("ref", ""))) for m in children_of(child(root, "mergeCells"), "mergeCell")), key=js.utf16_key)

    filter_node = child(root, "autoFilter")
    filter_: Optional[dict] = None
    if filter_node is not None:
        attrs = {k: v for k, v in filter_node.attrs.items() if k != "ref"}
        stripped = XmlNode(filter_node.name, attrs, filter_node.children, filter_node.text)
        filter_ = {"ref": filter_node.attrs.get("ref"), "rest": canonical(to_data(stripped))}
    protection_node = child(root, "sheetProtection")

    # Comments and hyperlinks make a cell "occupied" even when it has no value.
    for rel in rels.values():
        if rel["external"] or not rel["type"].endswith("/comments"):
            continue
        comments_file = _resolve(directory, rel["target"])
        if not zip_.has(comments_file):
            continue
        comments = parse_xml(zip_.text(comments_file))
        authors = [a.text for a in children_of(child(comments, "authors"), "author")]
        for comment in children_of(child(comments, "commentList"), "comment"):
            ref = comment.attrs.get("ref")
            if not ref:
                continue
            author = js.index_of(authors, js.number(comment.attrs.get("authorId", 0)))
            ensure(ref).comment = [author if author is not None else "", _rich_text(child(comment, "text"))]
    for link in children_of(child(root, "hyperlinks"), "hyperlink"):
        rel = rels.get(link.attrs["id"]) if link.attrs.get("id") else None
        value = [rel["target"] if rel else None, link.attrs.get("location")]
        area = CellRange(link.attrs.get("ref", ""))
        need((area.max_col - area.min_col + 1) * (area.max_row - area.min_row + 1) <= 10_000, f"{name} has a hyperlink range that is too large")
        for r in range(area.min_row, area.max_row + 1):
            for c in range(area.min_col, area.max_col + 1):
                ensure(coord_of(c, r)).link = value

    rules = [_read_rule(n, False) for n in children_of(child(root, "dataValidations"), "dataValidation")]
    for ext in children_of(child(root, "extLst"), "ext"):
        for n in children_of(child(ext, "dataValidations"), "dataValidation"):
            rules.append(_read_rule(n, True))

    return SheetData(name, state, cells, bounds["row"], bounds["col"], merges, rows, columns, filter_,
                     canonical(protection_node.attrs) if protection_node is not None else None,
                     _truthy(protection_node.attrs.get("sheet") if protection_node is not None else None), rules)


def read_workbook(file: str) -> WorkbookData:
    with open(file, "rb") as handle:
        zip_ = ZipReader(handle.read())
    need(zip_.has("xl/workbook.xml"), "not a valid XLSX: xl/workbook.xml is missing")
    book = parse_xml(zip_.text("xl/workbook.xml"))
    rels = _relationships(zip_, "xl/_rels/workbook.xml.rels")
    shared = [_rich_text(si) for si in children_of(parse_xml(zip_.text("xl/sharedStrings.xml")), "si")] if zip_.has("xl/sharedStrings.xml") else []
    styles = _Styles(zip_.text("xl/styles.xml") if zip_.has("xl/styles.xml") else None)
    sheets: list[SheetData] = []
    cells = 0
    for s in children_of(child(book, "sheets"), "sheet"):
        name = s.attrs.get("name")
        rel = rels.get(s.attrs["id"]) if s.attrs.get("id") else None
        need(name and rel and not rel["external"], "the workbook's list of sheets is invalid")
        need(not any(x.name == name for x in sheets), f"duplicate sheet name: {name}")
        sheet = _read_sheet(zip_, _resolve("xl", rel["target"]), name, s.attrs.get("state", "visible"), styles, shared)
        need(sheet.max_row * sheet.max_col <= MAX_CELLS, f"{name} is too large to check completely")
        cells += len(sheet.cells)
        sheets.append(sheet)
    need(len(sheets) > 0, "the XLSX has no worksheet")
    need(cells <= MAX_CELLS, "the XLSX has too many cells to check completely")
    return WorkbookData(sheets)
