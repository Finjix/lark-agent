"""Unpacks the zip a user sends instead of a folder (unzip.ts).

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
    need(value <= MAX_SAFE, "压缩包过大")
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


def list_zip(file: str) -> list[ZipEntry]:
    """The entries of a zip, from its central directory."""
    with open(file, "rb") as handle:
        size = os.fstat(handle.fileno()).st_size
        tail_length = min(size, 65_557)
        tail = _read_at(handle, size - tail_length, tail_length)
        eocd = -1
        i = len(tail) - 22
        while i >= 0:
            if struct.unpack_from("<I", tail, i)[0] == 0x06054B50:
                eocd = i
                break
            i -= 1
        need(eocd >= 0, "不是有效的 zip 压缩包")
        count = struct.unpack_from("<H", tail, eocd + 10)[0]
        dir_size = struct.unpack_from("<I", tail, eocd + 12)[0]
        dir_start = struct.unpack_from("<I", tail, eocd + 16)[0]
        if count == 0xFFFF or dir_size == 0xFFFFFFFF or dir_start == 0xFFFFFFFF:
            # zip64: the locator just before the end record points at the zip64 end record.
            need(eocd >= 20 and struct.unpack_from("<I", tail, eocd - 20)[0] == 0x07064B50, "zip64 压缩包结构损坏")
            record = _read_at(handle, _big(tail, eocd - 20 + 8), 56)
            need(len(record) == 56 and struct.unpack_from("<I", record, 0)[0] == 0x06064B50, "zip64 压缩包结构损坏")
            count, dir_size, dir_start = _big(record, 32), _big(record, 40), _big(record, 48)
        need(dir_size <= MAX_DIRECTORY and dir_start + dir_size <= size, "压缩包目录损坏或过大")
        directory = _read_at(handle, dir_start, dir_size)
    entries: list[ZipEntry] = []
    pos = 0
    for _ in range(count):
        need(pos + 46 <= len(directory) and struct.unpack_from("<I", directory, pos)[0] == 0x02014B50, "压缩包目录损坏")
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


def extract_entry(file: str, entry: ZipEntry, target: str) -> None:
    """Streams one entry into a new file and checks its size and CRC-32."""
    need((entry.flags & 1) == 0, f"压缩包里的文件加了密码，无法读取：{entry.name}")
    need(entry.method in (0, 8), f"压缩包里的文件用了不支持的压缩方式：{entry.name}")
    with open(file, "rb") as handle:
        local = _read_at(handle, entry.offset, 30)
        need(len(local) == 30 and struct.unpack_from("<I", local, 0)[0] == 0x04034B50, f"压缩包条目损坏：{entry.name}")
        start = entry.offset + 30 + struct.unpack_from("<H", local, 26)[0] + struct.unpack_from("<H", local, 28)[0]
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
                        raise Blocked(f"压缩包条目比记录的大：{entry.name}")
                    crc = zlib.crc32(data, crc)
                    out.write(data)
                if inflater:
                    data = inflater.flush()
                    size += len(data)
                    if size > entry.usize:
                        raise Blocked(f"压缩包条目比记录的大：{entry.name}")
                    crc = zlib.crc32(data, crc)
                    out.write(data)
    except Blocked:
        if made:
            _remove(target)
        raise
    except (OSError, zlib.error):
        if made:
            _remove(target)
        raise Blocked(f"压缩包里的文件解压失败：{entry.name}") from None
    if size != entry.usize or (crc & 0xFFFFFFFF) != (entry.crc & 0xFFFFFFFF):
        _remove(target)
        raise Blocked(f"压缩包里的文件已损坏（大小或校验不符）：{entry.name}")


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
            skipped.append({"file": label(e.name), "reason": "路径不安全，没有解压"})
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
            skipped.append({"file": label(entry.name), "reason": "压缩包里的子文件夹不读取（文件夹不可嵌套）"})
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
            skipped.append({"file": label(entry.name), "reason": "压缩包里有同名文件，只取第一个" if other.lower() == given.lower()
                            else f"和“{other}”换掉 Windows 文件名不能有的字符后同名（{name}），只取第一个"})
            continue
        if entry.usize > limits["entry"]:
            skipped.append({"file": label(entry.name), "reason": "单个文件超过 5 GB"})
            continue
        names[name.lower()] = given
        total += entry.usize
        if name != given:
            renamed.append({"file": label(entry.name), "as": name})
        chosen.append((entry, name))
    need(len(chosen) <= limits["files"], f"压缩包里的素材超过 {limits['files']} 个，请分批")
    need(total <= limits["bytes"], "压缩包解压后超过 20 GB，请分批")
    for entry, name in chosen:
        extract_entry(file, entry, os.path.join(directory, name))
    with open(done, "w", encoding="utf-8", newline="") as handle:
        from .js import stringify

        handle.write(stringify({"zip": os.path.abspath(file), "skipped": skipped, "renamed": renamed}))
    return Unpacked(directory, skipped, renamed)
