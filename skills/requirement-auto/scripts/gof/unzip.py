"""Zip files: the .xlsx exports, read in memory (ZipReader), and the zip a user sends instead of a folder, unpacked to disk.

Feishu does not let a bot download a folder. Material zips are large, so the entries are streamed from the file to disk, never held in
memory; zip64 (what big archives use), stored and deflated entries, and the name encodings zips are made with in practice (UTF-8, or the
Chinese Windows code page GBK without the UTF-8 flag) are understood. Every entry is checked against its size and CRC-32.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import struct
import time
import zlib
from dataclasses import dataclass
from typing import Callable, Optional

from .util import Blocked, need

MAX_DIRECTORY = 64 * 1024 * 1024
MAX_SAFE = 2 ** 53 - 1


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


def _read_at(handle, position: int, length: int) -> bytes:
    handle.seek(position)
    return handle.read(length)


def _big(data: bytes, at: int) -> int:
    value = struct.unpack_from("<Q", data, at)[0]
    need(value <= MAX_SAFE, "the zip is too large")
    return value


def _decode_name(raw: bytes, flags: int, unicode: Optional[str]) -> str:
    """A name as the zip means it: flagged UTF-8, the Info-ZIP Unicode path, plain UTF-8, else GBK (zips made by Chinese Windows tools)."""
    if flags & 0x800:
        return raw.decode("utf-8", "replace")
    if unicode is not None:
        return unicode
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("gb18030", "replace")


def _central_directory(read_at: Callable[[int, int], bytes], size: int) -> list[ZipEntry]:
    """The entries of a zip from its central directory; `read_at(position, length)` reads the archive (a file, or bytes in memory)."""
    tail_length = min(size, 65_557)
    tail = read_at(size - tail_length, tail_length)
    eocd = -1
    i = len(tail) - 22
    while i >= 0:
        if struct.unpack_from("<I", tail, i)[0] == 0x06054B50:
            eocd = i
            break
        i -= 1
    need(eocd >= 0, "not a valid zip file")
    count = struct.unpack_from("<H", tail, eocd + 10)[0]
    dir_size = struct.unpack_from("<I", tail, eocd + 12)[0]
    dir_start = struct.unpack_from("<I", tail, eocd + 16)[0]
    if count == 0xFFFF or dir_size == 0xFFFFFFFF or dir_start == 0xFFFFFFFF:
        # zip64: the locator just before the end record points at the zip64 end record.
        need(eocd >= 20 and struct.unpack_from("<I", tail, eocd - 20)[0] == 0x07064B50, "damaged zip64 structure")
        record = read_at(_big(tail, eocd - 20 + 8), 56)
        need(len(record) == 56 and struct.unpack_from("<I", record, 0)[0] == 0x06064B50, "damaged zip64 structure")
        count, dir_size, dir_start = _big(record, 32), _big(record, 40), _big(record, 48)
    need(dir_size <= MAX_DIRECTORY and dir_start + dir_size <= size, "the zip's directory is damaged or too large")
    directory = read_at(dir_start, dir_size)
    entries: list[ZipEntry] = []
    pos = 0
    for _ in range(count):
        need(pos + 46 <= len(directory) and struct.unpack_from("<I", directory, pos)[0] == 0x02014B50, "the zip's directory is damaged")
        flags, method = struct.unpack_from("<HH", directory, pos + 8)
        crc, csize, usize = struct.unpack_from("<III", directory, pos + 16)
        name_length, extra_length, comment_length = struct.unpack_from("<HHH", directory, pos + 28)
        offset = struct.unpack_from("<I", directory, pos + 42)[0]
        raw = directory[pos + 46:pos + 46 + name_length]
        unicode: Optional[str] = None
        e = pos + 46 + name_length
        stop = pos + 46 + name_length + extra_length
        while e + 4 <= stop:
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
                unicode = body[5:].decode("utf-8", "replace")
            e += 4 + length
        name = _decode_name(raw, flags, unicode).replace("\\", "/")
        entries.append(ZipEntry(name, flags, method, crc, csize, usize, offset, name.endswith("/")))
        pos += 46 + name_length + extra_length + comment_length
    return entries


def list_zip(file: str) -> list[ZipEntry]:
    """The entries of a zip file, from its central directory."""
    with open(file, "rb") as handle:
        return _central_directory(lambda position, length: _read_at(handle, position, length), os.fstat(handle.fileno()).st_size)


def _data_start(read_at: Callable[[int, int], bytes], entry: ZipEntry) -> int:
    """Where an entry's data begins, after its local header."""
    local = read_at(entry.offset, 30)
    need(len(local) == 30 and struct.unpack_from("<I", local, 0)[0] == 0x04034B50, f"damaged zip entry: {entry.name}")
    return entry.offset + 30 + struct.unpack_from("<H", local, 26)[0] + struct.unpack_from("<H", local, 28)[0]


def _check_readable(entry: ZipEntry) -> None:
    need((entry.flags & 1) == 0, f"a file in the zip is password-protected and cannot be read: {entry.name}")
    need(entry.method in (0, 8), f"a file in the zip uses an unsupported compression method: {entry.name}")


class ZipReader:
    """
    A zip held in memory (an .xlsx): an entry is read whole and checked against its declared size and CRC-32, and what all reads inflate
    to is capped against zip bombs.
    """

    MAX_ENTRY = 512 * 1024 * 1024
    MAX_TOTAL = 1024 * 1024 * 1024

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._inflated = 0
        self._entries: dict[str, ZipEntry] = {}
        for entry in _central_directory(self._slice, len(data)):
            need(entry.name not in self._entries, f"duplicate zip entry: {entry.name}")
            self._entries[entry.name] = entry

    def _slice(self, position: int, length: int) -> bytes:
        return self._data[position:position + length]

    def has(self, name: str) -> bool:
        return name in self._entries

    def names(self) -> list[str]:
        return list(self._entries)

    def read(self, name: str) -> bytes:
        entry = self._entries.get(name)
        if entry is None:
            raise Blocked(f"the zip has no entry {name}")
        _check_readable(entry)
        need(entry.usize <= self.MAX_ENTRY, f"zip entry too large: {name}")
        begin = _data_start(self._slice, entry)
        need(begin + entry.csize <= len(self._data), f"damaged zip entry: {name}")
        raw = self._data[begin:begin + entry.csize]
        if entry.method == 0:
            out = bytes(raw)
        else:
            try:
                out = zlib.decompressobj(-15).decompress(raw, self.MAX_ENTRY + 1)
            except zlib.error:
                raise Blocked(f"a zip entry cannot be inflated: {name}") from None
        need(len(out) == entry.usize and (zlib.crc32(out) & 0xFFFFFFFF) == (entry.crc & 0xFFFFFFFF),
             f"a zip entry is damaged (size or checksum differs): {name}")
        self._inflated += len(out)
        need(self._inflated <= self.MAX_TOTAL, "the zip inflates to too much")
        return out

    def text(self, name: str) -> str:
        text = self.read(name).decode("utf-8", "replace")
        return text[1:] if text.startswith("\ufeff") else text


def extract_entry(file: str, entry: ZipEntry, target: str) -> None:
    """Streams one entry into a new file and checks its size and CRC-32."""
    _check_readable(entry)
    with open(file, "rb") as handle:
        start = _data_start(lambda position, length: _read_at(handle, position, length), entry)
    size = 0
    crc = 0
    # Only a file this call made is removed on failure: a target that was there already (another entry's) is left alone.
    made = False
    try:
        with open(target, "xb") as out:
            made = True
            with open(file, "rb") as source:
                source.seek(start)
                left = entry.csize
                inflater = zlib.decompressobj(-15) if entry.method == 8 else None
                while left > 0:
                    chunk = source.read(min(1024 * 1024, left))
                    if not chunk:
                        break
                    left -= len(chunk)
                    data = inflater.decompress(chunk) if inflater else chunk
                    size += len(data)
                    if size > entry.usize:
                        raise Blocked(f"a zip entry is larger than recorded: {entry.name}")
                    crc = zlib.crc32(data, crc)
                    out.write(data)
                if inflater:
                    data = inflater.flush()
                    size += len(data)
                    if size > entry.usize:
                        raise Blocked(f"a zip entry is larger than recorded: {entry.name}")
                    crc = zlib.crc32(data, crc)
                    out.write(data)
    except Blocked:
        if made:
            _remove(target)
        raise
    except (OSError, zlib.error):
        if made:
            _remove(target)
        raise Blocked(f"a file in the zip could not be unpacked: {entry.name}") from None
    if size != entry.usize or (crc & 0xFFFFFFFF) != (entry.crc & 0xFFFFFFFF):
        _remove(target)
        raise Blocked(f"a file in the zip is damaged (size or checksum differs): {entry.name}")


def _remove(target: str) -> None:
    try:
        os.remove(target)
    except OSError:
        pass


@dataclass
class Unpacked:
    dir: str
    skipped: list[dict]
    #: files stored under another name than the zip gives them (characters a Windows file name cannot hold become `_`)
    renamed: list[dict]


_JUNK = re.compile(r"(\.DS_Store|Thumbs\.db|desktop\.ini)", re.IGNORECASE)
_ID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_BAD_CHARS = re.compile(r'[<>:"\\|?*\x00-\x1f]')


def unpack_zip(file: str, root: str, keep: Callable[[str], Optional[str]],
               limits: Optional[dict] = None) -> Unpacked:
    """
    Unpacks the files of a material zip into a folder of its own, flat, the way a folder pick reads one level: the files at the top, or
    in the one folder the zip was made of ("素材/a.mp4"). Anything deeper, and what the `keep` test turns down, is listed with the reason.
    The folder is named after the zip's hash, so the same zip is unpacked once.
    """
    limits = limits or {"files": 200, "bytes": 20 * 1024 ** 3, "entry": 5 * 1024 ** 3}
    sha = hashlib.sha256()
    with open(file, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            sha.update(chunk)
    stem = os.path.splitext(os.path.basename(file))[0]
    ident = f"{sha.hexdigest()[:16]}-{_ID_CHARS.sub('_', stem)[:40]}"
    base = os.path.join(root, ident)
    directory = os.path.join(base, "files")
    done = os.path.join(base, "complete.json")

    def label(name: str) -> str:
        return f"{os.path.basename(file)}：{name}"

    if os.path.exists(done):
        # Used again: it is in use, so the retention sweep (which judges each unpacked zip by its newest file) must not take it before the upload runs.
        now = time.time()
        os.utime(done, (now, now))
        with open(done, "r", encoding="utf-8") as handle:
            record = json.load(handle)
        return Unpacked(directory, record["skipped"], record.get("renamed") or [])
    shutil.rmtree(base, ignore_errors=True)
    os.makedirs(directory, exist_ok=True)
    entries = [e for e in list_zip(file)
               if not e.directory and not e.name.startswith("__MACOSX/") and not _JUNK.fullmatch(e.name.rsplit("/", 1)[-1])]
    skipped: list[dict] = []

    def parts(e: ZipEntry) -> list[str]:
        return [p for p in e.name.split("/") if p]

    safe = []
    for e in entries:
        bad = e.name.startswith("/") or bool(re.match(r"[a-zA-Z]:", e.name)) or any(p == ".." for p in parts(e))
        if bad:
            skipped.append({"file": label(e.name), "reason": "unsafe path, not unpacked"})
        else:
            safe.append(e)
    # A zip of one folder holds everything under that folder's name: that level is the folder itself.
    top = 1 if safe and all(len(parts(e)) > 1 and parts(e)[0] == parts(safe[0])[0] for e in safe) else 0
    chosen: list[tuple[ZipEntry, str]] = []
    #: Stored name, lower case (Windows does not tell case apart) → the name the zip gives the file kept under it.
    names: dict[str, str] = {}
    renamed: list[dict] = []
    total = 0
    for entry in safe:
        rest = parts(entry)[top:]
        if len(rest) > 1:
            skipped.append({"file": label(entry.name), "reason": "a folder inside the zip is not read (folders do not nest)"})
            continue
        # The file is read under the name it is stored by (zips made on a Mac may hold ? * : and the like): that name is checked, kept apart
        # from the others and reported when it differs.
        given = rest[0]
        name = _BAD_CHARS.sub("_", given)
        refused = keep(name)
        if refused:
            skipped.append({"file": label(entry.name), "reason": refused})
            continue
        other = names.get(name.lower())
        if other is not None:
            skipped.append({"file": label(entry.name), "reason": "the zip holds another file of this name: only the first is taken" if other.lower() == given.lower()
                            else f"the same name as \"{other}\" once the characters a Windows file name cannot hold are replaced ({name}): only the first is taken"})
            continue
        if entry.usize > limits["entry"]:
            skipped.append({"file": label(entry.name), "reason": "larger than 5 GB"})
            continue
        names[name.lower()] = given
        total += entry.usize
        if name != given:
            renamed.append({"file": label(entry.name), "as": name})
        chosen.append((entry, name))
    need(len(chosen) <= limits["files"], f"the zip holds more than {limits['files']} materials: split it")
    need(total <= limits["bytes"], "the zip unpacks to more than 20 GB: split it")
    for entry, name in chosen:
        extract_entry(file, entry, os.path.join(directory, name))
    with open(done, "w", encoding="utf-8", newline="") as handle:
        from .js import stringify

        handle.write(stringify({"zip": os.path.abspath(file), "skipped": skipped, "renamed": renamed}))
    return Unpacked(directory, skipped, renamed)
