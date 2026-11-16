"""Writes the boxes of a small MP4 the way encoders lay them out, for the header tests: ftyp, then either mdat before moov (a plain
export) or moov first (faststart), with a movie header and one video track whose picture may be stored on its side (phone videos)."""

from __future__ import annotations

import struct


def _box(kind: str, *parts: bytes) -> bytes:
    body = b"".join(parts)
    return struct.pack(">I", 8 + len(body)) + kind.encode("latin-1") + body


def make_mp4(seconds: float, width: int, height: int, rotated: bool = False, moov_first: bool = False, version1: bool = False,
             payload: int = 4096) -> bytes:
    scale = 1000
    length = round(seconds * scale)
    mvhd = bytearray(112 if version1 else 100)
    mvhd[0] = 1 if version1 else 0
    if version1:
        struct.pack_into(">I", mvhd, 20, scale)
        struct.pack_into(">Q", mvhd, 24, length)
    else:
        struct.pack_into(">I", mvhd, 12, scale)
        struct.pack_into(">I", mvhd, 16, length)
    tkhd = bytearray(96 if version1 else 84)
    tkhd[0] = 1 if version1 else 0
    base = 36 if version1 else 24
    # The display matrix: identity, or 90° (a = 0, b = 1, c = -1, d = 0).
    matrix = [0, 0x10000, 0, -0x10000, 0, 0, 0, 0, 0x40000000] if rotated else [0x10000, 0, 0, 0, 0x10000, 0, 0, 0, 0x40000000]
    for i, value in enumerate(matrix):
        struct.pack_into(">i", tkhd, base + 16 + i * 4, value)
    struct.pack_into(">I", tkhd, base + 52, width * 65536)
    struct.pack_into(">I", tkhd, base + 56, height * 65536)
    hdlr = bytearray(24)
    hdlr[8:12] = b"vide"
    sound = _box("trak", _box("tkhd", bytes(84)), _box("mdia", _box("hdlr", bytes(8) + b"soun" + bytes(12))))
    moov = _box("moov", _box("mvhd", bytes(mvhd)), sound, _box("trak", _box("tkhd", bytes(tkhd)), _box("mdia", _box("hdlr", bytes(hdlr)))))
    ftyp = _box("ftyp", b"isom\0\0\x02\0isomiso2mp41")
    mdat = _box("mdat", b"\x01" * payload)
    return ftyp + moov + mdat if moov_first else ftyp + mdat + moov
