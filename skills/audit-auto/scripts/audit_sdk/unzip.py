"""The zip a user sends instead of a folder (a chat app does not let a bot download a folder).

Material zips are large, so entries are streamed from the file to disk, never held in memory; zip64, stored and deflated entries and
the name encodings zips are made with in practice (UTF-8, or GBK from Chinese Windows tools, without the UTF-8 flag) are understood,
and every entry is checked against its size and CRC-32. Folders inside the zip are kept as part of the name, since materials often
come as 横版/ and 竖版/ (landscape, portrait). Named unzip.py so as not to shadow the built-in zip().

Python's zipfile is not used: it reads GBK names as cp437, knows nothing of the Info-ZIP Unicode path, and its errors are not the
reasons the user is told here.
"""

from __future__ import annotations

import os
import posixpath
import re
import struct
import zlib
from dataclasses import dataclass, field
from typing import BinaryIO, Callable

from .errors import Blocked, need

MAX_DIRECTORY = 64 * 1024 * 1024
_CHUNK = 1024 * 1024
_SAFE_MAX = 2 ** 53 - 1


@dataclass
class ZipEntry:
    name: str
    flags: int
    method: int
    crc: int
    csize: int
    usize: int
    offset: int
    directory: bool


def _read_at(handle: BinaryIO, position: int, length: int) -> bytes:
    handle.seek(position)
    return handle.read(length)


def _big(buffer: bytes, at: int) -> int:
    value = struct.unpack_from("<Q", buffer, at)[0]
    need(value <= _SAFE_MAX, "The zip is too large")
    return value


def _decode_name(raw: bytes, flags: int, unicode: str | None) -> str:
    """A name as the zip means it: flagged UTF-8, the Info-ZIP Unicode path, plain UTF-8, else GBK (zips made by Chinese Windows tools)."""
    if flags & 0x800:
        return raw.decode("utf-8", errors="replace")
    if unicode is not None:
        return unicode
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        # The WHATWG "gbk" decoder TextDecoder uses is GB18030's.
        return raw.decode("gb18030", errors="replace")


def list_zip(file: str) -> list[ZipEntry]:
    """The entries of a zip, from its central directory."""
    with open(file, "rb") as handle:
        size = os.fstat(handle.fileno()).st_size
        tail_length = min(size, 65_557)
        tail = _read_at(handle, size - tail_length, tail_length)
        eocd = -1
        for i in range(len(tail) - 22, -1, -1):
            if tail[i:i + 4] == b"PK\x05\x06":
                eocd = i
                break
        need(eocd >= 0, "Not a valid zip file")
        count = struct.unpack_from("<H", tail, eocd + 10)[0]
        dir_size = struct.unpack_from("<I", tail, eocd + 12)[0]
        dir_start = struct.unpack_from("<I", tail, eocd + 16)[0]
        if count == 0xFFFF or dir_size == 0xFFFFFFFF or dir_start == 0xFFFFFFFF:
            # zip64: the locator just before the end record points at the zip64 end record.
            need(eocd >= 20 and tail[eocd - 20:eocd - 16] == b"PK\x06\x07", "The zip64 structure is damaged")
            record = _read_at(handle, _big(tail, eocd - 20 + 8), 56)
            need(len(record) == 56 and record[:4] == b"PK\x06\x06", "The zip64 structure is damaged")
            count, dir_size, dir_start = _big(record, 32), _big(record, 40), _big(record, 48)
        need(dir_size <= MAX_DIRECTORY and dir_start + dir_size <= size, "The zip's directory is damaged or too large")
        directory = _read_at(handle, dir_start, dir_size)
    entries: list[ZipEntry] = []
    pos = 0
    for _ in range(count):
        need(pos + 46 <= len(directory) and directory[pos:pos + 4] == b"PK\x01\x02", "The zip's directory is damaged")
        flags, method = struct.unpack_from("<HH", directory, pos + 8)
        crc, csize, usize = struct.unpack_from("<III", directory, pos + 16)
        name_length, extra_length, comment_length = struct.unpack_from("<HHH", directory, pos + 28)
        offset = struct.unpack_from("<I", directory, pos + 42)[0]
        raw = directory[pos + 46:pos + 46 + name_length]
        unicode: str | None = None
        e = pos + 46 + name_length
        end = pos + 46 + name_length + extra_length
        while e + 4 <= end:
            ident, length = struct.unpack_from("<HH", directory, e)
            body = directory[e + 4:e + 4 + length]
            if ident == 0x0001:
                # zip64 sizes and offset, each present only where the 32-bit field is full.
                at = 0
                if usize == 0xFFFFFFFF:
                    usize = _big(body, at)
                    at += 8
                if csize == 0xFFFFFFFF:
                    csize = _big(body, at)
                    at += 8
                if offset == 0xFFFFFFFF:
                    offset = _big(body, at)
            elif ident == 0x7075 and len(body) > 5 and body[0] == 1:
                unicode = body[5:].decode("utf-8", errors="replace")
            e += 4 + length
        name = _decode_name(raw, flags, unicode).replace("\\", "/")
        entries.append(ZipEntry(name, flags, method, crc, csize, usize, offset, name.endswith("/")))
        pos += 46 + name_length + extra_length + comment_length
    return entries


def extract_entry(file: str, entry: ZipEntry, target: str) -> None:
    """Streams one entry into a new file and checks its size and CRC-32."""
    need((entry.flags & 1) == 0, f"A file in the zip is password-protected: {entry.name}")
    need(entry.method in (0, 8), f"A file in the zip uses an unsupported compression method: {entry.name}")
    size = 0
    crc = 0
    created = False
    try:
        with open(file, "rb") as source:
            local = _read_at(source, entry.offset, 30)
            need(len(local) == 30 and local[:4] == b"PK\x03\x04", f"A zip entry is damaged: {entry.name}")
            start = entry.offset + 30 + struct.unpack_from("<H", local, 26)[0] + struct.unpack_from("<H", local, 28)[0]
            source.seek(start)
            inflate = zlib.decompressobj(-15) if entry.method == 8 else None
            with open(target, "xb") as out:
                created = True
                left = entry.csize
                while left > 0:
                    chunk = source.read(min(_CHUNK, left))
                    if not chunk:
                        break
                    left -= len(chunk)
                    data = inflate.decompress(chunk) if inflate else chunk
                    size += len(data)
                    if size > entry.usize:
                        raise Blocked(f"A zip entry is larger than recorded: {entry.name}")
                    crc = zlib.crc32(data, crc)
                    out.write(data)
                if inflate:
                    data = inflate.flush()
                    size += len(data)
                    if size > entry.usize:
                        raise Blocked(f"A zip entry is larger than recorded: {entry.name}")
                    crc = zlib.crc32(data, crc)
                    out.write(data)
    except Blocked:
        if created:
            _remove(target)
        raise
    except Exception:  # noqa: BLE001 - a broken stream is a broken entry
        if created:
            _remove(target)
        raise Blocked(f"A file in the zip could not be unpacked: {entry.name}") from None
    if size != entry.usize or (crc & 0xFFFFFFFF) != (entry.crc & 0xFFFFFFFF):
        _remove(target)
        raise Blocked(f"A file in the zip is damaged (its size or checksum does not match): {entry.name}")


def _remove(target: str) -> None:
    try:
        os.remove(target)
    except OSError:
        pass


@dataclass
class Unpacked:
    files: list[dict[str, str]] = field(default_factory=list)
    skipped: list[dict[str, str]] = field(default_factory=list)


JUNK = re.compile(r"^(\.DS_Store|Thumbs\.db|desktop\.ini)$", re.IGNORECASE)
_UNSAFE_CHARS = re.compile(r'[<>:"\\|?*\x00-\x1f]')


def unpack_zip(file: str, directory: str, keep: Callable[[str], str | None], max_files: int = 50, max_bytes: int = 10 * 1024 ** 3) -> Unpacked:
    """Unpacks the files of a material zip that `keep` accepts into `directory`, each under a numbered name of its own; `name` is its
    path inside the zip, which is what the user sees. What is left out is listed with the reason."""
    zip_name = os.path.basename(file)

    def label(name: str) -> str:
        return f"{zip_name} › {name}"

    entries = [e for e in list_zip(file)
               if not e.directory and not e.name.startswith("__MACOSX/") and not JUNK.match(posixpath.basename(e.name))]
    result = Unpacked()
    chosen: list[ZipEntry] = []
    total = 0
    for entry in entries:
        parts = [p for p in entry.name.split("/") if p]
        if entry.name.startswith("/") or re.match(r"^[a-z]:", entry.name, re.IGNORECASE) or ".." in parts:
            result.skipped.append({"file": label(entry.name), "reason": "Unsafe path: not unpacked"})
            continue
        refused = keep(parts[-1])
        if refused:
            result.skipped.append({"file": label(entry.name), "reason": refused})
            continue
        total += entry.usize
        chosen.append(entry)
    need(len(chosen) <= max_files, f"The zip holds more than {max_files} materials: send them in batches")
    need(total <= max_bytes, f"The zip unpacks to more than {max_bytes // 1024 ** 3} GB: send it in batches")
    os.makedirs(directory, exist_ok=True)
    for i, entry in enumerate(chosen):
        base = _UNSAFE_CHARS.sub("_", posixpath.basename(entry.name))
        target = os.path.join(directory, f"{i + 1:02d}-{base}")
        extract_entry(file, entry, target)
        result.files.append({"file": target, "name": label(entry.name)})
    return result
