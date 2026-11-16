"""A read-only view of a zip file: just enough of the format for an .xlsx (zip.ts).

Stored or deflated entries, no encryption, no zip64. Hand-written like the original (rather than zipfile) so that the same limits and the
same refusals apply: an entry's declared size is checked against what it inflates to, and the total is capped against zip bombs.
"""

from __future__ import annotations

import struct
import zlib

from .util import Blocked, need

MAX_ENTRY = 512 * 1024 * 1024
MAX_TOTAL = 1024 * 1024 * 1024


def _u16(data: bytes, at: int) -> int:
    return struct.unpack_from("<H", data, at)[0]


def _u32(data: bytes, at: int) -> int:
    return struct.unpack_from("<I", data, at)[0]


class ZipReader:
    def __init__(self, data: bytes) -> None:
        self._data = data
        self._entries: dict[str, tuple[int, int, int, int]] = {}
        self._inflated = 0
        tail = max(0, len(data) - 65557)
        eocd = -1
        i = len(data) - 22
        while i >= tail:
            if _u32(data, i) == 0x06054B50:
                eocd = i
                break
            i -= 1
        need(eocd >= 0, "不是有效的 XLSX（zip）文件")
        count = _u16(data, eocd + 10)
        size = _u32(data, eocd + 12)
        start = _u32(data, eocd + 16)
        need(count != 0xFFFF and size != 0xFFFFFFFF and start != 0xFFFFFFFF, "不支持 zip64 压缩包")
        need(start + size <= len(data), "XLSX（zip）目录已损坏")
        pos = start
        for _ in range(count):
            need(pos + 46 <= len(data) and _u32(data, pos) == 0x02014B50, "XLSX（zip）目录已损坏")
            flags = _u16(data, pos + 8)
            need((flags & 1) == 0, "不支持加密的压缩包")
            method = _u16(data, pos + 10)
            csize = _u32(data, pos + 20)
            usize = _u32(data, pos + 24)
            name_length = _u16(data, pos + 28)
            extra_length = _u16(data, pos + 30)
            comment_length = _u16(data, pos + 32)
            offset = _u32(data, pos + 42)
            name = data[pos + 46:pos + 46 + name_length].decode("utf-8", "replace")
            need(name not in self._entries, f"XLSX 含重复条目：{name}")
            self._entries[name] = (method, csize, usize, offset)
            pos += 46 + name_length + extra_length + comment_length

    def has(self, name: str) -> bool:
        return name in self._entries

    def names(self) -> list[str]:
        return list(self._entries)

    def read(self, name: str) -> bytes:
        entry = self._entries.get(name)
        need(entry, f"XLSX 缺少条目：{name}")
        method, csize, usize, offset = entry  # type: ignore[misc]
        need(usize <= MAX_ENTRY, f"XLSX 条目过大：{name}")
        data = self._data
        need(offset + 30 <= len(data) and _u32(data, offset) == 0x04034B50, "XLSX（zip）条目已损坏")
        begin = offset + 30 + _u16(data, offset + 26) + _u16(data, offset + 28)
        need(begin + csize <= len(data), "XLSX（zip）条目已损坏")
        raw = data[begin:begin + csize]
        if method == 0:
            out = bytes(raw)
        elif method == 8:
            try:
                inflater = zlib.decompressobj(-15)
                out = inflater.decompress(raw, MAX_ENTRY + 1)
                if len(out) > MAX_ENTRY:
                    raise zlib.error("too large")
            except zlib.error:
                raise Blocked(f"XLSX 条目无法解压：{name}") from None
        else:
            raise Blocked(f"不支持的压缩方式：{name}")
        need(len(out) == usize, f"XLSX 条目大小不符：{name}")
        self._inflated += len(out)
        need(self._inflated <= MAX_TOTAL, "XLSX 解压后过大")
        return out

    def text(self, name: str) -> str:
        text = self.read(name).decode("utf-8", "replace")
        return text[1:] if text.startswith("\ufeff") else text
