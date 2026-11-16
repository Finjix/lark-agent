"""Writes small zips the way real tools do, for the unpacking tests: deflated or stored entries, names as UTF-8 with the flag or as
raw bytes without it (Chinese Windows tools write GBK), zip64 records as big-archive writers produce them, and a wrong CRC on request."""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass


@dataclass
class ZipFile:
    name: str | bytes
    data: bytes | str
    store: bool = False
    bad_crc: bool = False


def _deflate_raw(data: bytes) -> bytes:
    compressor = zlib.compressobj(6, zlib.DEFLATED, -15)
    return compressor.compress(data) + compressor.flush()


def make_zip(files: list[ZipFile], zip64: bool = False) -> bytes:
    locals_: list[bytes] = []
    central: list[bytes] = []
    offset = 0
    for f in files:
        data = f.data.encode("utf-8") if isinstance(f.data, str) else f.data
        name = f.name if isinstance(f.name, bytes) else f.name.encode("utf-8")
        utf8 = isinstance(f.name, str) and any(ord(c) > 0x7F for c in f.name)
        body = data if f.store else _deflate_raw(data)
        crc = (zlib.crc32(data) ^ (1 if f.bad_crc else 0)) & 0xFFFFFFFF
        extra = struct.pack("<HHQQQ", 0x0001, 24, len(data), len(body), offset) if zip64 else b""
        method = 0 if f.store else 8
        flag = 0x800 if utf8 else 0
        version = 45 if zip64 else 20
        csize = 0xFFFFFFFF if zip64 else len(body)
        usize = 0xFFFFFFFF if zip64 else len(data)
        local = struct.pack("<IHHHHHIIIHH", 0x04034B50, version, flag, method, 0, 0, crc, csize, usize, len(name), len(extra))
        entry = struct.pack("<IHHHHHHIIIHHHHHII", 0x02014B50, 0x031E, version, flag, method, 0, 0, crc, csize, usize, len(name),
                            len(extra), 0, 0, 0, 0, 0xFFFFFFFF if zip64 else offset)
        locals_ += [local, name, extra, body]
        central += [entry, name, extra]
        offset += len(local) + len(name) + len(extra) + len(body)
    directory = b"".join(central)
    tail = b""
    if zip64:
        record = struct.pack("<IQHHIIQQQQ", 0x06064B50, 44, 45, 45, 0, 0, len(files), len(files), len(directory), offset)
        locator = struct.pack("<IIQI", 0x07064B50, 0, offset + len(directory), 1)
        tail = record + locator
    count = 0xFFFF if zip64 else len(files)
    end = struct.pack("<IHHHHIIH", 0x06054B50, 0, 0, count, count, 0xFFFFFFFF if zip64 else len(directory), 0xFFFFFFFF if zip64 else offset, 0)
    return b"".join(locals_) + directory + tail + end
