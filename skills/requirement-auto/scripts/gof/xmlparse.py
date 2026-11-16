"""A small non-validating XML reader for the parts of an .xlsx (xml.ts).

No DOCTYPE handling, so no entity expansion beyond the five predefined ones and character references. Kept as a hand-written reader
rather than xml.etree so that it reads exactly what the TypeScript one read: the same text, the same attributes, the same refusals.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .util import need


@dataclass
class XmlNode:
    #: Local name: the namespace prefix is dropped (x14:dataValidation → dataValidation).
    name: str
    attrs: dict[str, str] = field(default_factory=dict)
    children: list["XmlNode"] = field(default_factory=list)
    #: All character data directly inside the element, in order.
    text: str = ""


_ENTITY = re.compile(r"&(#x[0-9a-fA-F]+|#[0-9]+|amp|lt|gt|quot|apos);")
_NAMED = {"amp": "&", "lt": "<", "gt": ">", "quot": '"', "apos": "'"}
MAX_DEPTH = 100


def _decode(value: str) -> str:
    if "&" not in value:
        return value

    def replace(match: re.Match[str]) -> str:
        body = match.group(1)
        if body in _NAMED:
            return _NAMED[body]
        code = int(body[2:], 16) if body[1] == "x" else int(body[1:], 10)
        if 0 <= code <= 0x10FFFF and not (0xD800 <= code <= 0xDFFF):
            return chr(code)
        return match.group(0)

    return _ENTITY.sub(replace, value)


def _local(name: str) -> str:
    i = name.find(":")
    return name if i < 0 else name[i + 1:]


_ATTRIBUTE = re.compile(r"""([^\s=/>]+)\s*=\s*(?:"([^"]*)"|'([^']*)')""")
_SPACE = re.compile(r"\s")
_TAG_END = re.compile(r"""[^"'>]*(?:(?:"[^"]*"|'[^']*')[^"'>]*)*>""")


def parse_xml(source: str) -> XmlNode:
    root = XmlNode("#document")
    stack: list[XmlNode] = [root]
    pos = 0
    end = len(source)
    texts: dict[int, list[str]] = {}

    def add_text(node: XmlNode, text: str) -> None:
        if text:
            texts.setdefault(id(node), []).append(text)

    while pos < end:
        lt = source.find("<", pos)
        top = stack[-1]
        if lt < 0:
            add_text(top, _decode(source[pos:]))
            break
        if lt > pos:
            add_text(top, _decode(source[pos:lt]))
        if source.startswith("<!--", lt):
            close = source.find("-->", lt + 4)
            need(close >= 0, "XML 注释未结束")
            pos = close + 3
        elif source.startswith("<![CDATA[", lt):
            close = source.find("]]>", lt + 9)
            need(close >= 0, "XML CDATA 未结束")
            add_text(top, source[lt + 9:close])
            pos = close + 3
        elif source.startswith("<?", lt):
            close = source.find("?>", lt + 2)
            need(close >= 0, "XML 声明未结束")
            pos = close + 2
        elif source.startswith("<!", lt):
            close = source.find(">", lt + 2)
            need(close >= 0, "XML 声明未结束")
            pos = close + 1
        elif lt + 1 < end and source[lt + 1] == "/":
            close = source.find(">", lt + 2)
            need(close >= 0 and len(stack) > 1, "XML 标签不匹配")
            need(_local(source[lt + 2:close].strip()) == top.name, "XML 标签不匹配")
            node = stack.pop()
            node.text = "".join(texts.pop(id(node), []))
            pos = close + 1
        else:
            # The first > outside quoted attribute values.
            found = _TAG_END.match(source, lt + 1)
            close = found.end() - 1 if found else end
            need(close < end, "XML 标签未结束")
            self_closing = source[close - 1] == "/"
            inner = source[lt + 1:close - 1 if self_closing else close]
            space = _SPACE.search(inner)
            raw_name = inner if space is None else inner[:space.start()]
            need(len(raw_name) > 0, "XML 标签缺少名称")
            attrs: dict[str, str] = {}
            if space is not None:
                attribute_text = inner[space.start():]
                need(len(attribute_text) <= 100_000, "XML 标签过长")
                for m in _ATTRIBUTE.finditer(attribute_text):
                    key = m.group(1)
                    if key == "xmlns" or key.startswith("xmlns:"):
                        continue
                    value = m.group(2) if m.group(2) is not None else (m.group(3) or "")
                    attrs[_local(key)] = _decode(value)
            node = XmlNode(_local(raw_name), attrs)
            top.children.append(node)
            if not self_closing:
                need(len(stack) <= MAX_DEPTH, "XML 嵌套过深")
                stack.append(node)
            pos = close + 1
    need(len(stack) == 1, "XML 标签未闭合")
    root.text = "".join(texts.pop(id(root), []))
    doc = root.children[0] if root.children else None
    need(doc is not None and len(root.children) == 1, "XML 缺少根元素")
    return doc  # type: ignore[return-value]


def child(node: XmlNode | None, name: str) -> XmlNode | None:
    if node is None:
        return None
    for c in node.children:
        if c.name == name:
            return c
    return None


def children_of(node: XmlNode | None, name: str) -> list[XmlNode]:
    return [c for c in node.children if c.name == name] if node is not None else []


def to_data(node: XmlNode | None) -> Any:
    """The node as plain data, for comparing two nodes by content."""
    if node is None:
        return None
    return {"name": node.name, "attrs": node.attrs, "text": "" if node.children else node.text,
            "children": [to_data(c) for c in node.children]}
