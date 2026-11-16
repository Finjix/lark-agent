"""Getting a material file ready to send to Gemini inline (port of media.ts; the gateway has no Files API).

Gemini samples a video into frames at the fps it is told and scales every frame down, so a 1080p master carries nothing a 720p copy
does not: big videos are re-encoded to 720p with ffmpeg when the computer has it (FFMPEG, or ffmpeg on the PATH), which cuts the
upload from minutes to seconds. Without ffmpeg a video is sent as it is, up to INLINE_MAX. Duration and orientation are read from the
MP4/MOV header itself, so they need no ffmpeg either.
"""

from __future__ import annotations

import math
import os
import re
import struct
import subprocess
import tempfile
from dataclasses import dataclass
from typing import Mapping

from .errors import Blocked, need
from .jsutil import js_round, num, to_fixed


@dataclass(frozen=True)
class _Type:
    kind: str
    mime: str
    native: bool


#: What Gemini takes (native), and video containers it does not take, which ffmpeg turns into MP4 first.
TYPES: dict[str, _Type] = {
    ".mp4": _Type("video", "video/mp4", True), ".m4v": _Type("video", "video/mp4", True),
    ".mov": _Type("video", "video/mov", True), ".webm": _Type("video", "video/webm", True),
    ".mpg": _Type("video", "video/mpeg", True), ".mpeg": _Type("video", "video/mpeg", True),
    ".avi": _Type("video", "video/avi", True), ".wmv": _Type("video", "video/wmv", True),
    ".flv": _Type("video", "video/x-flv", True), ".3gp": _Type("video", "video/3gpp", True),
    ".mkv": _Type("video", "video/mp4", False), ".ts": _Type("video", "video/mp4", False),
    ".png": _Type("image", "image/png", True), ".jpg": _Type("image", "image/jpeg", True),
    ".jpeg": _Type("image", "image/jpeg", True), ".webp": _Type("image", "image/webp", True),
    ".heic": _Type("image", "image/heic", True), ".heif": _Type("image", "image/heif", True),
}

MB = 1024 * 1024
#: Above this a video is re-encoded when ffmpeg is there (a 68 MB request took 76 s through the gateway, a 15 MB one 8 s).
COMPRESS_OVER = 40 * MB
#: The most that is sent inline (the gateway took a 112 MB video; the request is a third bigger again as base64).
INLINE_MAX = 150 * MB
IMAGE_MAX = 20 * MB

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _ext(name: str) -> str:
    """path.extname, lower-cased: '.mp4', or '' for a name without one (or a dotfile)."""
    base = re.split(r"[\\/]", name.rstrip("\\/"))[-1]
    dot = base.rfind(".")
    return base[dot:].lower() if dot > 0 else ""


def refusal(name: str) -> str | None:
    """Why a file is not material that can be reviewed, or None when it is."""
    ext = _ext(name)
    if ext in TYPES:
        return None
    if ext == ".gif":
        return "GIF 动图不支持，请转成视频（MP4）或截成图片再发"
    return "不是视频或图片，没有审"


def kind_of(name: str) -> str | None:
    found = TYPES.get(_ext(name))
    return found.kind if found else None


# ---- what is in an MP4 / MOV

@dataclass
class Probe:
    duration: float | None = None
    width: int | None = None
    height: int | None = None


@dataclass
class _Box:
    type: str
    start: int
    end: int
    body: int


def _box_at(buffer: bytes, at: int, limit: int) -> _Box | None:
    if at + 8 > limit:
        return None
    size = struct.unpack_from(">I", buffer, at)[0]
    kind = buffer[at + 4:at + 8].decode("latin-1")
    body = at + 8
    if size == 1:
        if at + 16 > limit:
            return None
        size = struct.unpack_from(">Q", buffer, at + 8)[0]
        body = at + 16
    elif size == 0:
        size = limit - at
    if size < body - at or at + size > limit:
        return None
    return _Box(kind, at, at + size, body)


def _children(buffer: bytes, box: _Box) -> list[_Box]:
    found: list[_Box] = []
    at = box.body
    while True:
        child = _box_at(buffer, at, box.end)
        if not child:
            return found
        found.append(child)
        at = child.end


def _child(buffer: bytes, box: _Box | None, kind: str) -> _Box | None:
    if not box:
        return None
    return next((c for c in _children(buffer, box) if c.type == kind), None)


def probe(file: str) -> Probe:
    """Duration and picture size (as shown, rotation applied) from the movie header; nothing when the file is not an MP4/MOV or is damaged."""
    try:
        with open(file, "rb") as handle:
            size = os.fstat(handle.fileno()).st_size
            moov: bytes | None = None
            at = 0
            while at + 8 <= size:
                handle.seek(at)
                head = handle.read(16)
                if len(head) < 8:
                    break
                length = struct.unpack_from(">I", head, 0)[0]
                kind = head[4:8].decode("latin-1")
                if length == 1 and len(head) >= 16:
                    length = struct.unpack_from(">Q", head, 8)[0]
                elif length == 0:
                    length = size - at
                if length < 8:
                    break
                if kind == "moov":
                    if length > 64 * MB:
                        break
                    handle.seek(at)
                    moov = handle.read(length).ljust(length, b"\0")
                    break
                at += length
        return _movie(moov) if moov else Probe()
    except Exception:  # noqa: BLE001 - a damaged file says nothing
        return Probe()


def _movie(buffer: bytes) -> Probe:
    moov = _box_at(buffer, 0, len(buffer))
    if not moov:
        return Probe()
    out = Probe()
    mvhd = _child(buffer, moov, "mvhd")
    if mvhd and mvhd.body + 32 <= mvhd.end:
        v1 = buffer[mvhd.body] == 1
        scale = struct.unpack_from(">I", buffer, mvhd.body + (20 if v1 else 12))[0]
        length = struct.unpack_from(">Q", buffer, mvhd.body + 24)[0] if v1 else struct.unpack_from(">I", buffer, mvhd.body + 16)[0]
        if scale > 0 and length > 0:
            out.duration = num(js_round((length / scale) * 100) / 100)
    for trak in (c for c in _children(buffer, moov) if c.type == "trak"):
        hdlr = _child(buffer, _child(buffer, trak, "mdia"), "hdlr")
        if not hdlr or hdlr.body + 12 > hdlr.end or buffer[hdlr.body + 8:hdlr.body + 12].decode("latin-1") != "vide":
            continue
        tkhd = _child(buffer, trak, "tkhd")
        if not tkhd:
            continue
        base = tkhd.body + (36 if buffer[tkhd.body] == 1 else 24)
        if base + 60 > tkhd.end:
            continue
        a, b = struct.unpack_from(">ii", buffer, base + 16)
        width = js_round(struct.unpack_from(">I", buffer, base + 52)[0] / 65536)
        height = js_round(struct.unpack_from(">I", buffer, base + 56)[0] / 65536)
        if not width or not height:
            continue
        # A phone video is stored on its side with a 90° or 270° rotation in the matrix (a = 0, b = ±1).
        turned = a == 0 and b != 0
        out.width = height if turned else width
        out.height = width if turned else height
        break
    return out


def orientation(p: Probe) -> str | None:
    if not p.width or not p.height:
        return None
    return "横版" if p.width > p.height else "竖版" if p.width < p.height else "方版"


def fps_for(duration: float | None) -> int:
    """How many frames a second Gemini looks at: ad cuts and subtitles are short, so short videos get up to 4 (about 160 frames at
    most, some 280 tokens each at high media resolution), long ones Gemini's own 1."""
    if not duration or not math.isfinite(duration):
        return 2
    return min(4, max(1, math.floor(160 / duration)))


# ---- ffmpeg

def find_ffmpeg(env: Mapping[str, str] | None = None) -> str | None:
    """ffmpeg if this computer has it (FFMPEG, else ffmpeg on the PATH), or None."""
    env = os.environ if env is None else env
    candidate = (env.get("FFMPEG") or "").strip() or "ffmpeg"
    try:
        done = subprocess.run([candidate, "-hide_banner", "-version"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=30, creationflags=_NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return None
    return candidate if done.returncode == 0 else None


def compress(ffmpeg: str, source: str, output: str, timeout: float = 10 * 60) -> None:
    """A 720p H.264 copy (the short side at most 720, at most 30 frames a second, AAC sound), streamable from its start."""
    scale = "scale='if(gte(iw,ih),-2,min(720,iw))':'if(gte(iw,ih),min(720,ih),-2)'"
    args = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-i", source, "-map", "0:v:0", "-map", "0:a:0?", "-vf", scale,
            "-fpsmax", "30", "-c:v", "libx264", "-preset", "veryfast", "-crf", "25", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
            "-ac", "2", "-movflags", "+faststart", output]
    try:
        done = subprocess.run(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=timeout,
                              creationflags=_NO_WINDOW)
    except subprocess.TimeoutExpired:
        raise RuntimeError("ffmpeg timed out") from None
    errors = done.stderr.decode("utf-8", errors="replace")[-2000:].strip()
    if done.returncode == 0 and os.path.isfile(output) and os.path.getsize(output) > 0:
        return
    raise RuntimeError(f"ffmpeg exited with {done.returncode}: {errors.splitlines()[-1] if errors else ''}")


# ---- one file, ready to send

@dataclass
class Prepared:
    """`file` is what is sent (the original, or the compressed copy in the scratch folder)."""
    file: str
    kind: str
    mime: str
    bytes: int
    sent: int
    compressed: bool
    duration: float | None = None
    orientation: str | None = None
    fps: int | None = None
    warning: str | None = None


def _mb(n: int) -> str:
    return to_fixed(n / MB, 1 if n < 10 * MB else 0)


def prepare(file: str, scratch: str, ffmpeg: str | None) -> Prepared:
    kind = TYPES.get(_ext(file))
    need(kind, refusal(file) or "不支持的文件")
    assert kind is not None
    size_in = os.stat(file).st_size
    need(size_in > 0, "文件是空的")
    if kind.kind == "image":
        need(size_in <= IMAGE_MAX, f"图片太大（{_mb(size_in)} MB），请压缩到 {IMAGE_MAX // MB} MB 以内再发")
        return Prepared(file=file, kind="image", mime=kind.mime, bytes=size_in, sent=size_in, compressed=False)
    original = probe(file)
    sent, info, warning = file, original, None
    if not kind.native or size_in > COMPRESS_OVER:
        if ffmpeg:
            # A folder of its own: two files of the same name may be compressed at the same time.
            stem = os.path.splitext(os.path.basename(file))[0][:60]
            output = os.path.join(tempfile.mkdtemp(prefix="c-", dir=scratch), f"{stem}-720p.mp4")
            try:
                compress(ffmpeg, file, output)
                sent = output
                copy = probe(output)
                info = Probe(duration=copy.duration if copy.duration is not None else original.duration, width=copy.width, height=copy.height)
            except Exception:  # noqa: BLE001 - sent as it is, when Gemini takes it
                need(kind.native, "这种视频格式需要先转码，但转码失败了：请导出成 MP4 再发")
                warning = "压缩失败，按原文件发送，审得会慢一些"
        else:
            need(kind.native, "这种视频格式（如 MKV）需要先转成 MP4 再发")
    size = size_in if sent == file else os.stat(sent).st_size
    if size > INLINE_MAX:
        raise Blocked(f"视频太大（{_mb(size)} MB），请压缩到 {INLINE_MAX // MB} MB 以内再发")
    return Prepared(
        file=sent, kind="video", mime=kind.mime if sent == file else "video/mp4", bytes=size_in, sent=size,
        duration=info.duration or None, orientation=orientation(original), fps=fps_for(info.duration), compressed=sent != file, warning=warning,
    )
