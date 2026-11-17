"""Shared test helpers: temporary folders, hand-made zips and workbooks, a clock."""

from __future__ import annotations

import atexit
import os
import shutil
import struct
import sys
import tempfile
import zlib
from typing import Any, Optional, Union

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL = os.path.dirname(HERE)
SCRIPTS = os.path.join(SKILL, "scripts")
FIXTURES = os.path.join(HERE, "fixtures")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

_made: list[str] = []


def _cleanup() -> None:
    for directory in _made:
        shutil.rmtree(directory, ignore_errors=True)


atexit.register(_cleanup)


def temp_dir(prefix: str) -> str:
    """A fresh temporary directory that is removed when the test process ends, whether or not the test cleaned up itself."""
    directory = tempfile.mkdtemp(prefix=prefix)
    _made.append(directory)
    return directory


def fixture(name: str) -> str:
    return os.path.join(FIXTURES, name)


class Clock:
    """A clock the tests move by hand: sleeping just advances it, so nothing waits for real."""

    def __init__(self, start: int = 1_700_000_000_000) -> None:
        self.t = start
        self.slept: list[float] = []

    def now(self) -> int:
        return self.t

    def sleep(self, ms: float) -> None:
        self.slept.append(ms)
        self.t += ms


def make_zip(files: list[dict], zip64: bool = False) -> bytes:
    """
    Writes small zips the way real tools do, for the unpacking tests: deflated or stored entries, names as UTF-8 with the flag or as raw
    bytes without it (Chinese Windows tools write GBK), zip64 records as big-archive writers produce them, and a wrong CRC on request.
    Each file: {"name": str | bytes, "data": bytes | str, "store"?: bool, "bad_crc"?: bool}.
    """
    locals_: list[bytes] = []
    central: list[bytes] = []
    offset = 0
    for file in files:
        data = file["data"].encode("utf-8") if isinstance(file["data"], str) else file["data"]
        name = file["name"] if isinstance(file["name"], bytes) else file["name"].encode("utf-8")
        utf8 = not isinstance(file["name"], bytes) and any(ord(c) > 0x7F for c in file["name"])
        store = file.get("store", False)
        if store:
            body = data
        else:
            compressor = zlib.compressobj(6, zlib.DEFLATED, -15)
            body = compressor.compress(data) + compressor.flush()
        crc = (zlib.crc32(data) ^ (1 if file.get("bad_crc") else 0)) & 0xFFFFFFFF
        extra = struct.pack("<HHQQQ", 0x0001, 24, len(data), len(body), offset) if zip64 else b""
        flag = 0x800 if utf8 else 0
        method = 0 if store else 8
        local = struct.pack("<IHHHHHIIIHH", 0x04034B50, 45 if zip64 else 20, flag, method, 0, 0, crc,
                            0xFFFFFFFF if zip64 else len(body), 0xFFFFFFFF if zip64 else len(data), len(name), len(extra))
        entry = struct.pack("<IHHHHHHIIIHHHHHII", 0x02014B50, 0x031E, 45 if zip64 else 20, flag, method, 0, 0, crc,
                            0xFFFFFFFF if zip64 else len(body), 0xFFFFFFFF if zip64 else len(data), len(name), len(extra), 0, 0, 0, 0,
                            0xFFFFFFFF if zip64 else offset)
        locals_.extend([local, name, extra, body])
        central.extend([entry, name, extra])
        offset += len(local) + len(name) + len(extra) + len(body)
    directory = b"".join(central)
    tail: list[bytes] = []
    if zip64:
        record = struct.pack("<IQHHIIQQQQ", 0x06064B50, 44, 45, 45, 0, 0, len(files), len(files), len(directory), offset)
        locator = struct.pack("<IIQI", 0x07064B50, 0, offset + len(directory), 1)
        tail.extend([record, locator])
    end = struct.pack("<IHHHHIIH", 0x06054B50, 0, 0, 0xFFFF if zip64 else len(files), 0xFFFF if zip64 else len(files),
                      0xFFFFFFFF if zip64 else len(directory), 0xFFFFFFFF if zip64 else offset, 0)
    return b"".join(locals_) + directory + b"".join(tail) + end


XlsxCell = Union[str, int, float, None, dict]


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _letter(index: int) -> str:
    out = ""
    n = index
    while n > 0:
        out = chr(65 + (n - 1) % 26) + out
        n = (n - 1) // 26
    return out


def _cell_xml(value: XlsxCell, coord: str) -> str:
    if value is None:
        return ""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f'<c r="{coord}"><v>{value}</v></c>'
    if isinstance(value, str):
        return f'<c r="{coord}" t="inlineStr"><is><t>{_escape(value)}</t></is></c>'
    return f'<c r="{coord}"><f>{_escape(value["f"])}</f></c>'


def make_xlsx(sheets: list[dict]) -> bytes:
    """
    Writes the smallest XLSX the workbook reader takes, for tests that need a progress sheet of their own: each cell is a number, an inline
    text, or a formula without a computed value ({"f": "CONCATENATE(…)"}, as Feishu's export writes them). Row 1 is the header.
    Each sheet: {"name": str, "rows": [[cell, …], …]}.
    """
    ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    listed = "".join(f'<sheet name="{_escape(s["name"])}" sheetId="{i + 1}" r:id="rId{i + 1}"/>' for i, s in enumerate(sheets))
    workbook = f'<?xml version="1.0" encoding="UTF-8"?><workbook xmlns="{ns}" xmlns:r="{rel}"><sheets>{listed}</sheets></workbook>'
    relations = "".join(f'<Relationship Id="rId{i + 1}" Type="{rel}/worksheet" Target="worksheets/sheet{i + 1}.xml"/>' for i in range(len(sheets)))
    rels = f'<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">{relations}</Relationships>'

    def sheet_xml(sheet: dict) -> str:
        rows = "".join(f'<row r="{r + 1}">' + "".join(_cell_xml(v, f"{_letter(c + 1)}{r + 1}") for c, v in enumerate(cells)) + "</row>"
                       for r, cells in enumerate(sheet["rows"]))
        return f'<?xml version="1.0" encoding="UTF-8"?><worksheet xmlns="{ns}"><sheetData>{rows}</sheetData></worksheet>'

    return make_zip([{"name": "xl/workbook.xml", "data": workbook}, {"name": "xl/_rels/workbook.xml.rels", "data": rels},
                     *[{"name": f"xl/worksheets/sheet{i + 1}.xml", "data": sheet_xml(s)} for i, s in enumerate(sheets)]])


def write_file(path: str, data: Union[bytes, str]) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(data.encode("utf-8") if isinstance(data, str) else data)
    return path


def find_browser() -> Optional[str]:
    from gof.chromium import find_browser as find

    return find()


def py_script(directory: str, name: str, source: str) -> str:
    """A small Python script written to a file, for stand-ins of other programs (the feishu skill's feishu.py)."""
    return write_file(os.path.join(directory, name), source)


def strip_keys(value: dict, *keys: str) -> dict[str, Any]:
    return {k: v for k, v in value.items() if k not in keys}
