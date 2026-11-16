"""A Feishu document as Markdown (markdown.ts).

A docx from its blocks (headings, paragraphs, lists, to-dos, code, quotes, callouts, tables, dividers; pictures, files and embedded objects
as a placeholder in brackets), a sheet tab as a table. What it is for is reading: the text and its structure come through, the looks do not
(colors, alignment, column widths).
"""

from __future__ import annotations

import math
import re
from typing import Any
from urllib.parse import unquote

from .jsutil import js_number, js_string, js_trim, truthy

Obj = dict[str, Any]

HEADINGS = {3: 1, 4: 2, 5: 3, 6: 4, 7: 5, 8: 6, 9: 6, 10: 6, 11: 6}
BODY = {
    2: "text", 3: "heading1", 4: "heading2", 5: "heading3", 6: "heading4", 7: "heading5", 8: "heading6", 9: "heading7", 10: "heading8",
    11: "heading9", 12: "bullet", 13: "ordered", 14: "code", 15: "quote", 17: "todo",
}
PLACEHOLDER = {
    18: "[多维表格]", 20: "[群名片]", 21: "[流程图]", 26: "[内嵌网页]", 28: "[应用]", 29: "[思维笔记]", 30: "[内嵌电子表格]", 33: "[视图]",
    35: "[任务]", 36: "[OKR]", 40: "[小组件]", 43: "[画板]",
}
_BAD_ESCAPE = re.compile(r"%(?![0-9A-Fa-f]{2})")
_EDGES = re.compile(r"(\s*)(.*?)(\s*)\Z", re.DOTALL)


def _decode(url: str) -> str:
    """`decodeURIComponent`, or the text as it is when it is not a valid escape sequence."""
    if _BAD_ESCAPE.search(url):
        return url
    try:
        return unquote(url, errors="strict")
    except UnicodeDecodeError:
        return url


def _o(value: Any) -> Obj:
    return value if isinstance(value, dict) else {}


def _s(value: Any, fallback: str = "") -> str:
    """`String(value ?? fallback)`."""
    return fallback if value is None else js_string(value)


def inline(elements: Any) -> str:
    """Text runs with their inline marks; mentions by name or title, equations as they are written."""
    if not isinstance(elements, list):
        return ""
    out: list[str] = []
    for element in elements:
        e = _o(element)
        if truthy(e.get("text_run")):
            run = _o(e["text_run"])
            text = _s(run.get("content"))
            style = _o(run.get("text_element_style"))
            if not js_trim(text):
                out.append(text)
                continue
            if style.get("inline_code"):
                text = f"`{text}`"
            else:
                # The marks go round the words, not the spaces beside them: Markdown reads "** 3 亿 **" as plain asterisks.
                match = _EDGES.match(text)
                assert match is not None
                lead, marked, trail = match.group(1), match.group(2), match.group(3)
                if style.get("bold"):
                    marked = f"**{marked}**"
                if style.get("italic"):
                    marked = f"*{marked}*"
                if style.get("strikethrough"):
                    marked = f"~~{marked}~~"
                text = f"{lead}{marked}{trail}"
            link = _o(style.get("link"))
            if link.get("url"):
                text = f"[{text}]({_decode(js_string(link['url']))})"
            out.append(text)
        elif truthy(e.get("mention_doc")):
            doc = _o(e["mention_doc"])
            title = doc.get("title")
            if doc.get("url"):
                out.append(f"[{_s(title, '文档')}]({_decode(js_string(doc['url']))})")
            else:
                out.append(_s(title))
        elif truthy(e.get("mention_user")):
            out.append("@用户")
        elif truthy(e.get("equation")):
            out.append(f"${js_trim(_s(_o(e['equation']).get('content')))}$")
        elif truthy(e.get("file")):
            out.append("[附件]")
        elif truthy(e.get("reminder")):
            out.append("[提醒]")
    return "".join(out)


def _cell_text(text: str) -> str:
    """A cell's text for a Markdown table: no line breaks, no column separators."""
    return js_trim(re.sub(r"\r?\n", "<br>", text).replace("|", "\\|"))


def table(rows: list[list[str]]) -> str:
    if not rows:
        return ""
    width = max([1, *(len(row) for row in rows)])

    def line(cells: list[str]) -> str:
        return "| " + " | ".join(_cell_text(cells[i] if i < len(cells) and cells[i] is not None else "") for i in range(width)) + " |"

    return "\n".join([line(rows[0]), "| " + " | ".join("---" for _ in range(width)) + " |", *(line(row) for row in rows[1:])])


def _type(block: Any) -> float:
    value = js_number(_o(block).get("block_type")) if _o(block).get("block_type") is not None else math.nan
    return value


def _key(value: float) -> int | None:
    return int(value) if math.isfinite(value) and value.is_integer() else None


def docx_to_markdown(blocks: list[Obj]) -> str:
    by_id = {js_string(b.get("block_id")) if b.get("block_id") is not None else "undefined": b for b in blocks if isinstance(b, dict)}
    page = next((b for b in blocks if isinstance(b, dict) and b.get("block_type") == 1 and not isinstance(b.get("block_type"), bool)), None)
    if page is None:
        return ""

    def children(block: Obj) -> list[Obj]:
        ids = block.get("children")
        if not isinstance(ids, list):
            return []
        found = (by_id.get(js_string(i) if i is not None else "null") for i in ids)
        return [b for b in found if b is not None]

    def is_list(block: Obj | None) -> bool:
        return block is not None and _key(_type(block)) in (12, 13, 17)

    def sequence(items: list[Obj], indent: str) -> list[str]:
        # List items are written line after line; a blank line closes a run of them before what follows (in a callout or a quote too), or
        # that would read as more of the last item.
        out: list[str] = []
        for i, block in enumerate(items):
            out.extend(render(block, indent))
            following = items[i + 1] if i + 1 < len(items) else None
            if is_list(block) and following and not is_list(following):
                out.append("")
        return out

    def render(block: Obj, indent: str) -> list[str]:
        """A block and what is below it, as Markdown lines; `indent` is the list depth."""
        kind = _key(_type(block))
        body_key = BODY.get(kind, "") if kind is not None else ""
        body = _o(block.get(body_key)) if block.get(body_key) is not None else {}
        text = inline(body.get("elements"))

        def nested(prefix: str) -> list[str]:
            return sequence(children(block), prefix)

        if kind in HEADINGS:
            return [f"{'#' * HEADINGS[kind]} {text}".rstrip(), "", *nested(indent)]
        if kind == 2:
            return [*([f"{indent}{text}", *([] if indent else [""])] if js_trim(text) else []), *nested(indent)]
        if kind == 12:
            return [f"{indent}- {text}", *nested(f"{indent}  ")]
        if kind == 13:
            return [f"{indent}1. {text}", *nested(f"{indent}   ")]
        if kind == 17:
            done = _o(body.get("style")).get("done")
            return [f"{indent}- [{'x' if done else ' '}] {text}", *nested(f"{indent}  ")]
        if kind == 14:
            elements = body.get("elements") if isinstance(body.get("elements"), list) else []
            code = "".join(_s(_o(_o(e).get("text_run")).get("content")) for e in elements)
            return ["```", *code.split("\n"), "```", ""]
        if kind == 15:
            return [f"> {text}", ""]
        if kind in (19, 34):
            lines = nested("")
            inner = [line for i, line in enumerate(lines) if line != "" or (i > 0 and lines[i - 1] != "")]
            return [*(f"> {line}" if line else ">" for line in inner), ""]
        if kind == 22:
            return ["---", ""]
        if kind == 27:
            return [f"{indent}[图片]", ""]
        if kind == 23:
            return [f"{indent}[文件：{_s(_o(block.get('file')).get('name'))}]", ""]
        if kind in (24, 25):
            return nested(indent)
        if kind == 31:
            spec = _o(block.get("table"))
            prop = _o(spec.get("property"))
            rows, cols = js_number(prop.get("row_size", _Undefined)), js_number(prop.get("column_size", _Undefined))
            cells = spec.get("cells") if spec.get("cells") is not None else block.get("children") if block.get("children") is not None else []
            if not (math.isfinite(rows) and rows.is_integer() and math.isfinite(cols) and cols.is_integer()) or not isinstance(cells, list) or len(cells) != rows * cols:
                return ["[表格]", ""]
            r_count, c_count = int(rows), int(cols)
            covered: set[int] = set()
            merges = prop.get("merge_info") if isinstance(prop.get("merge_info"), list) else []
            for i, span in enumerate(merges):
                r, c = (i // c_count, i % c_count) if c_count else (0, 0)
                span = _o(span)
                row_span = _span(span.get("row_span"))
                col_span = _span(span.get("col_span"))
                for dr in range(row_span):
                    for dc in range(col_span):
                        if dr or dc:
                            covered.add((r + dr) * c_count + c + dc)

            def shown(cell_id: Any, at: int) -> str:
                cell = by_id.get(js_string(cell_id) if cell_id is not None else "null")
                if at in covered or cell is None:
                    return ""
                return "\n".join(line for child in children(cell) for line in render(child, "") if line != "")

            grid = [[shown(cells[r * c_count + c], r * c_count + c) for c in range(c_count)] for r in range(r_count)]
            return [table(grid), ""]
        if kind in PLACEHOLDER:
            return [f"{indent}{PLACEHOLDER[kind]}", ""]
        return [*([f"{indent}{text}", ""] if js_trim(text) else []), *nested(indent)]

    out = sequence(children(page), "")
    # Blank lines in a row become one, except in a code block, where they are the code's own.
    lines: list[str] = []
    code = False
    for line in out:
        if line == "```":
            code = not code
        elif not code and line == "" and lines and lines[-1] == "":
            continue
        lines.append(line)
    title = js_trim(inline(_o(page.get("page")).get("elements")))
    return f"{f'# {title}' + chr(10) + chr(10) if title else ''}{js_trim(chr(10).join(lines))}\n"


def _span(value: Any) -> int:
    number = js_number(value) if value is not None else 1.0
    return int(number) if math.isfinite(number) and number > 0 else 0


class _UndefinedType:
    pass


_Undefined = _UndefinedType()
