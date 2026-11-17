"""The material upload, offline.

Which files go up as which creatives, with what annotation, from a request and the options CLS offers. One file is one creative, as on the
page 上传创意 (a folder is read one level deep, like the page's folder pick, and each file in it is its own creative). Its name is the
素材名称 of the file's row in the progress sheet: the row the request names for the file, else the one the GOFCN number in the file name
points at. A file named otherwise goes up renamed (creative and uploaded file alike); the file on disk is left as it is. Without the
progress sheet (`"progress": false`) the name is the file name without its extension. The plan made here is sealed; `gof creative run`
checks the seal and the files against it before anything is sent.
"""

from __future__ import annotations

import hashlib
import os
import re
from datetime import datetime
from typing import Any, Callable, Iterable, Iterator, Optional

from . import js
from .a1 import column_letter
from .cls import match_user
from .seal import check_seal, reject_secrets, seal
from .snapshot import Columns, Snapshot, scan_sheets
from .util import Blocked, digest, is_object, need, read_json, write_json_new

DEFAULT_DIRECTORY = "双端共用-视频"

#: The formats CLS hands out an upload link for (asked of the real signing call, 2026-10-21), by what they are. Others get no link.
KINDS: dict[str, str] = {
    "mp4": "video", "mov": "video", "avi": "video", "m4v": "video",
    "jpg": "image", "jpeg": "image", "png": "image", "gif": "image", "webp": "image", "bmp": "image",
    "html": "other", "mp3": "other", "wav": "other",
}
#: 素材类型 by what the file is: a picture is 图片, a video 视频, anything else 其他.
TYPE_OF_KIND: dict[str, str] = {"video": "video", "image": "image", "other": "other"}
_ARCHIVES = {"zip", "rar", "7z", "tar", "gz"}
#: One PUT to object storage carries at most this much.
MAX_BYTES = 5 * 1024 ** 3
MAX_FILES = 200

#: Characters the page strips from names (bidirectional embedding and override marks).
BIDI = re.compile("[\u202a-\u202d]")


def ext_of(file_name: str) -> str:
    """``path.extname`` without its dot: '' for a name without one, or one that only starts with a dot."""
    base = file_name.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    dot = base.rfind(".")
    return base[dot + 1:] if dot > 0 else ""


def creative_name(file_name: str) -> str:
    dot = file_name.rfind(".")
    return BIDI.sub("", file_name[:dot] if dot > 0 else file_name)


def material_number(name: str) -> Optional[int]:
    """The GOFCN material number in a name ("GOFCN67471_AI剧情…" → 67471); GOFCNIM… and other series are not material numbers."""
    # At most 9 digits: GOFCN plus 11 digits is a requirement number (GOFCN20261019002), not a material.
    m = re.search(r"GOFCN([0-9]{1,9})(?![0-9])", name, re.IGNORECASE)
    return int(m.group(1)) if m else None


def pending_body(name: str) -> Optional[str]:
    """
    The rest of a name made for a row whose number is not known yet ("GOFCNxxxxx_AI玩法优化_…" → "AI玩法优化_…"), or None for any other
    name. Such a file is the row whose 素材名称 is GOFCN<its number>_<the rest>, once that row exists.
    """
    m = re.fullmatch("GOFCN(?:[xX×*＊?？#]+)?_(.+)", name)
    return m.group(1) if m else None


def name_parts(body: str) -> dict:
    """
    The parts of a material name without its number, read the way materials are named: 一级标签_二级标签_[创意形式_]提炼词. The two tags
    are the first two parts; `rest` is everything after them: the 提炼词, with a 创意形式 in front when the name has one (竖版 / 横版 and
    versions are part of the 提炼词). Which it is, is for the agent to judge (… _3D_围炉 has one, …_兵临城下塔防_企鹅 does not). A name of
    fewer parts is `unclear`.
    """
    parts = body.split("_")
    if len(parts) < 3 or any(p == "" for p in parts):
        return {"unclear": f"\"{body}\" is not named 一级标签_二级标签_提炼词: its parts cannot be told apart"}
    return {"primary_tag": parts[0], "secondary_tag": parts[1], "rest": "_".join(parts[2:])}


def variant_bases(body: str) -> list[str]:
    """
    The shorter names a name without its number may be a variant of: cut at its _, longest first, keeping at least 一级标签_二级标签_提炼词
    ("AI剧情_短剧表达_原生家庭WM_剧情衔接_竖版" → "AI剧情_短剧表达_原生家庭WM_剧情衔接", "AI剧情_短剧表达_原生家庭WM"), then with the first
    word after the tags (or after a 创意形式) cut short, since that word can be a variant too ("…_得加钱WM熔炉版_…" → "…_得加钱WM").
    """
    parts = body.split("_")
    out: list[str] = []
    for n in range(len(parts) - 1, 2, -1):
        out.append("_".join(parts[:n]))
    for n in (4, 3):
        if len(parts) < n:
            continue
        head = "_".join(parts[:n - 1])
        word = list(parts[n - 1])
        for k in range(len(word) - 1, 0, -1):
            out.append(f"{head}_{''.join(word[:k])}")
    return list(dict.fromkeys(out))


def name_body(name: Optional[str]) -> Optional[str]:
    """A 素材名称 without its number ("GOFCN67738_AI玩法优化_…" → "AI玩法优化_…"), or None when it does not start with one."""
    m = re.fullmatch(r"GOFCN[0-9]+_(.+)", name) if name else None
    return m.group(1) if m else None


def material_refusal(file_name: str) -> Optional[str]:
    """Why a file by this name cannot be a material, or None when it can (the same rule for files on disk and files inside a zip)."""
    if file_name.startswith("."):
        return "hidden file"
    ext = ext_of(file_name).lower()
    if ext == "zip":
        return "a zip inside a zip is not read"
    if ext in _ARCHIVES:
        return "only zip archives are read (not rar, 7z and the like): send it as a zip"
    if ext not in KINDS:
        return f"CLS does not take this format ({'.' + ext if ext else 'no extension'})"
    return None


def is_zip(file: str) -> bool:
    """Is this a zip (a folder the user packed, since Feishu cannot hand over a folder)?"""
    return ext_of(file).lower() == "zip"


def _sorted_entries(folder: str) -> list[os.DirEntry]:
    with os.scandir(folder) as it:
        return sorted(it, key=lambda e: js.locale_key(e.name))


def scan_materials(inputs: list[str], unpacked: Optional[dict[str, str]] = None) -> tuple[list[dict], list[dict]]:
    """
    The files behind what the user gave: files as they are, a folder's files one level deep, and a zip by the files it was unpacked to
    (`unpacked`: zip path → its unpacked folder, see unzip.py). What cannot go up is said, not dropped.
    """
    unpacked = unpacked or {}
    need(isinstance(inputs, list) and len(inputs) > 0, "no files to upload")
    materials: list[dict] = []
    skipped: list[dict] = []
    seen: set[str] = set()

    def consider(file: str, origin: Optional[str] = None) -> None:
        key = os.path.abspath(file).lower()
        if key in seen:
            return
        seen.add(key)
        try:
            st = os.stat(file)
            is_file = os.path.isfile(file)
        except OSError:
            st, is_file = None, False
        file_name = os.path.basename(file)
        if not is_file:
            skipped.append({"file": file, "reason": "not a regular file"})
            return
        folder = unpacked.get(key) if is_zip(file) else None
        if folder:
            for entry in _sorted_entries(folder):
                if entry.is_file():
                    consider(os.path.join(folder, entry.name), f"{file_name}：{entry.name}")
            return
        if is_zip(file):
            skipped.append({"file": file, "reason": "the zip was not unpacked"})
            return
        refused = material_refusal(file_name)
        if refused:
            skipped.append({"file": file, "reason": refused})
            return
        ext = ext_of(file_name).lower()
        kind = KINDS[ext]
        if st.st_size == 0:
            skipped.append({"file": file, "reason": "empty file"})
            return
        if st.st_size > MAX_BYTES:
            skipped.append({"file": file, "reason": "larger than 5 GB"})
            return
        name = creative_name(file_name)
        if not js.trim(name):
            skipped.append({"file": file, "reason": "empty file name"})
            return
        material = {"file": os.path.abspath(file), "file_name": file_name, "size": st.st_size, "mtime_ms": st.st_mtime_ns // 1_000_000, "ext": ext,
                    "kind": kind, "name": name, "number": material_number(name)}
        if origin:
            material["origin"] = origin
        materials.append(material)

    for entry_input in inputs:
        need(isinstance(entry_input, str) and js.trim(entry_input) != "", "files holds an empty path")
        full = os.path.abspath(entry_input)
        if not os.path.exists(full):
            skipped.append({"file": full, "reason": "no such file"})
            continue
        if os.path.isdir(full):
            for entry in _sorted_entries(full):
                inner = os.path.join(full, entry.name)
                if entry.is_dir():
                    skipped.append({"file": inner, "reason": "a folder inside the folder: its files were not read"})
                else:
                    consider(inner)
        else:
            consider(full)
    need(len(materials) <= MAX_FILES, f"at most {MAX_FILES} files per upload: split it")
    return materials, skipped


def hash_file(file: str) -> dict:
    """SHA-256 (to know the approved file is the one sent) and MD5 (what CLS records as the file's hash) in one read."""
    sha = hashlib.sha256()
    md5 = hashlib.md5()
    size = 0
    with open(file, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            sha.update(chunk)
            md5.update(chunk)
            size += len(chunk)
    return {"sha256": sha.hexdigest(), "md5": md5.hexdigest(), "size": size}


# ---- the request ------------------------------------------------------------------------------------------------------------------

TOP_KEYS = ["files", "directory", "creator", "producers", "progress", "variants", "items"]
ITEM_KEYS = ["file", "number", "sheet", "name", "variant", "type", "creator", "producers", "directory", "skip"]


def number_of(value: Any, label: str) -> int:
    """A material number as a request or a command line gives it: 67680, "67680" or "GOFCN67680"."""
    # At most 9 digits, as in material_number: GOFCN plus 11 digits is a requirement number, not a material.
    text = js.to_str(value) if js.is_safe_int(value) else value if isinstance(value, str) else ""
    m = re.fullmatch(r"\s*(?:GOFCN)?([0-9]{1,9})\s*", text, re.IGNORECASE)
    need(m and int(m.group(1)) > 0, f"{label} must be a GOFCN material number (like 67680 or GOFCN67680)")
    return int(m.group(1))


def _text(value: Any, label: str, maximum: int = 255) -> str:
    need(isinstance(value, str) and js.trim(value) != "" and len(value) <= maximum and not re.search(r"[\r\n\t\x00]", value), f"{label} must be one non-empty line of text")
    return js.trim(value)


def _names(value: Any, label: str) -> list[str]:
    need(isinstance(value, list) and len(value) > 0, f"{label} must be a list of at least one name")
    items = [_text(v, f"{label}[{i}]") for i, v in enumerate(value)]
    need(len(set(items)) == len(items), f"{label} has repeats")
    return items


def normalize_upload_request(data: Any) -> dict:
    need(is_object(data), "the upload request must be a JSON object")
    unknown = [k for k in data.keys() if k not in TOP_KEYS]
    need(len(unknown) == 0, f"unknown fields in the upload request: {', '.join(unknown)}")
    need(isinstance(data.get("files"), list) and len(data["files"]) > 0, "files must list the files or folders to upload")
    request: dict[str, Any] = {"files": [_text(f, f"files[{i}]", 1000) for i, f in enumerate(data["files"])]}
    if "directory" in data:
        request["directory"] = _text(data["directory"], "directory", 500)
    if "creator" in data:
        request["creator"] = _text(data["creator"], "creator")
    if "producers" in data:
        request["producers"] = _names(data["producers"], "producers")
    progress = data.get("progress", js.UNDEFINED)
    need(progress is js.UNDEFINED or progress is True or progress is False or isinstance(progress, str), "progress is true, false or a progress-sheet link")
    request["progress"] = True if progress is js.UNDEFINED or progress is True else False if progress is False else _text(progress, "progress", 1000)
    variants = data.get("variants", js.UNDEFINED)
    need(variants is js.UNDEFINED or isinstance(variants, bool), "variants is true or false")
    if variants is True:
        need(request["progress"] is not False, "variants (合并变体名称) only works with the progress sheet (progress cannot be false)")
        request["variants"] = True
    if "items" in data:
        need(isinstance(data["items"], list), "items must be a list")
        items = []
        for i, raw in enumerate(data["items"]):
            need(is_object(raw), f"items[{i}] must be an object")
            extra = [k for k in raw.keys() if k not in ITEM_KEYS]
            need(len(extra) == 0, f"items[{i}] has unknown fields: {', '.join(extra)}")
            item: dict[str, Any] = {"file": _text(raw.get("file"), f"items[{i}].file", 1000)}
            if "number" in raw:
                item["number"] = number_of(raw["number"], f"items[{i}].number")
            if "sheet" in raw:
                item["sheet"] = _text(raw["sheet"], f"items[{i}].sheet")
            if "name" in raw:
                item["name"] = _text(raw["name"], f"items[{i}].name")
            if "variant" in raw:
                need(request.get("variants"), f"items[{i}].variant only works with variants: true")
                need("name" not in item, f"items[{i}] takes name or variant, not both")
                item["variant"] = _text(raw["variant"], f"items[{i}].variant")
                # It goes into the creative's name after the row's 素材名称 and a _, as the parts of a name do.
                need(not re.search(r"^_|_$|__", item["variant"]) and not re.search(r"[\\/]", item["variant"]),
                     f"items[{i}].variant \"{item['variant']}\" cannot start or end with _, hold __, or contain / or \\")
            need(request["progress"] is not False or ("number" not in item and "sheet" not in item),
                 f"items[{i}].number / sheet only work with the progress sheet (progress cannot be false)")
            if "type" in raw:
                item["type"] = _text(raw["type"], f"items[{i}].type")
            if "creator" in raw:
                item["creator"] = _text(raw["creator"], f"items[{i}].creator")
            if "producers" in raw:
                item["producers"] = _names(raw["producers"], f"items[{i}].producers")
            if "directory" in raw:
                item["directory"] = _text(raw["directory"], f"items[{i}].directory", 500)
            if "skip" in raw:
                need(isinstance(raw["skip"], bool), f"items[{i}].skip is true or false")
                item["skip"] = raw["skip"]
            items.append(item)
        request["items"] = items
    return request


# ---- what CLS offers --------------------------------------------------------------------------------------------------------------


def flatten_directories(tree: list[dict]) -> list[dict]:
    """Every upload directory with its path from the top ("无尽冬日 - NEW / 常规素材 / 双端共用-视频")."""
    out: list[dict] = []

    def walk(nodes: list[dict], trail: list[str]) -> None:
        for node in nodes or []:
            parts = [*trail, js.to_str(node.get("name"))]
            out.append({"id": node.get("id"), "name": js.to_str(node.get("name")), "path": " / ".join(parts)})
            walk(node.get("children") or [], parts)

    walk(tree, [])
    return out


def find_directory(tree: list[dict], wanted: str) -> dict:
    """The one directory a name, a path ("常规素材/双端共用-视频", any end of the full path) or an ID means."""
    every = flatten_directories(tree)
    want = [js.trim(s) for s in wanted.split("/") if js.trim(s)]
    need(len(want) > 0, "the upload directory is empty")
    if re.fullmatch(r"[0-9]+", js.trim(wanted)):
        found = [d for d in every if d["id"] == int(js.trim(wanted))]
    else:
        def matches(d: dict) -> bool:
            parts = d["path"].split(" / ")
            start = len(parts) - len(want)
            return all(0 <= start + i < len(parts) and parts[start + i] == w for i, w in enumerate(want))

        found = [d for d in every if matches(d)]
    need(len(found) > 0, f"CLS has no upload directory \"{wanted}\"")
    need(len(found) == 1, f"the upload directory \"{wanted}\" is not unique ({'; '.join(d['path'] for d in found[:5])}): give its parent too")
    return {"id": found[0]["id"], "path": found[0]["path"]}


def _person(people: list[dict], wanted: str, label: str) -> dict:
    """
    The person a name means among the people the page offers: the exact name, else the short name a company name carries, else the one
    containing it. A CLS user ID picks one of two people with the same name (the 创意人 list has two 李晨阳).
    """
    if re.fullmatch(r"[0-9]+", wanted):
        by_id = next((p for p in people if p.get("id") == int(wanted)), None)
        need(by_id, f"{label} is not among the people on offer")
        return {"id": by_id["id"], "name": by_id["name"]}
    users = [{**p, "status": 1, "roles": []} for p in people]
    found = match_user(users, wanted, False, label)
    return {"id": found["id"], "name": found["name"]}


def match_people(options: dict, wanted: dict) -> dict:
    """
    Who the names of a 创意人 and 制作人 are in CLS, or why a name finds no one (or several): for files whose rows do not exist yet, which no
    plan can be made of before they are appended.
    """

    def one(people: list[dict], name: str, label: str) -> dict:
        try:
            return {"wanted": name, **_person(people, name, label)}
        except Blocked as error:
            return {"wanted": name, "problem": error.message}

    result: dict[str, Any] = {}
    if wanted.get("creator") is not None:
        result["creator"] = one(options["creators"], wanted["creator"], f"创意人 \"{wanted['creator']}\"")
    if wanted.get("producers") is not None:
        result["producers"] = [one(options["producers"], name, f"制作人 \"{name}\"") for name in wanted["producers"]]
    return result


def _with_hint(pick: Callable[[], Any], hint: str) -> Any:
    """A refusal of `pick` with a hint added at its end."""
    try:
        return pick()
    except Blocked as error:
        if hint:
            raise Blocked(f"{error.message}{hint}") from None
        raise


def _material_type(types: list[dict], wanted: Optional[str], kind: str) -> dict:
    """The 素材类型 a name or ID means; without one, by what the file is."""
    ident = wanted if wanted is not None else TYPE_OF_KIND[kind]
    found = [t for t in types if t["id"] == ident or t["name"] == ident]
    need(len(found) == 1, f"CLS has no 素材类型 \"{ident}\"")
    return {"id": found[0]["id"], "name": found[0]["name"]}


# ---- the plan ---------------------------------------------------------------------------------------------------------------------


def split_names(value: str) -> list[str]:
    """Names of several people in one sheet cell (the supplier column is a multiple choice)."""
    return [js.trim(s) for s in re.split(r"[,，、;；\n]", value) if js.trim(s)]


class _Unsupported(Exception):
    pass


def formula_text(formula: str, value_at: Callable[[str], Optional[str]]) -> Optional[str]:
    """
    The text a naming formula gives, the way the sheet shows it: CONCATENATE / CONCAT of texts, numbers and cells (an empty argument is an
    empty text), inside any number of SUBSTITUTE(…, "old", "new"). The progress sheet holds all of these ("…,D22487,,L22487)",
    SUBSTITUTE(CONCATENATE(…),"__","_"), a cell of the row above); anything else, or a cell that is itself a formula, gives None. Feishu's
    export carries no computed values, so this is the only way to know a row's 素材名称.
    """
    if not formula.startswith("="):
        return None
    src = formula[1:]
    pos = [0]

    def at(i: int) -> str:
        return src[i] if i < len(src) else ""

    def space() -> None:
        while pos[0] < len(src) and re.match(r"\s", src[pos[0]]):
            pos[0] += 1

    def expr() -> str:
        space()
        if at(pos[0]) == '"':
            out = ""
            pos[0] += 1
            while True:
                if pos[0] >= len(src):
                    raise _Unsupported()
                if src[pos[0]] != '"':
                    out += src[pos[0]]
                    pos[0] += 1
                    continue
                if at(pos[0] + 1) != '"':
                    pos[0] += 1
                    return out
                out += '"'
                pos[0] += 2
        rest = src[pos[0]:]
        call = re.match(r"([A-Za-z][A-Za-z0-9.]*)\s*\(", rest)
        if call:
            pos[0] += len(call.group(0))
            args: list[str] = []
            space()
            if at(pos[0]) == ")":
                pos[0] += 1
            else:
                while True:
                    space()
                    args.append("" if at(pos[0]) in (",", ")") else expr())
                    space()
                    if at(pos[0]) == ",":
                        pos[0] += 1
                        continue
                    if at(pos[0]) == ")":
                        pos[0] += 1
                        break
                    raise _Unsupported()
            fn = call.group(1).upper()
            if fn in ("CONCATENATE", "CONCAT"):
                return "".join(args)
            if fn == "SUBSTITUTE" and len(args) == 3 and args[1] != "":
                return args[0].replace(args[1], args[2])
            raise _Unsupported()
        ref = re.match(r"\$?([A-Za-z]{1,3})\$?([0-9]+)(?![A-Za-z0-9_:(])", rest)
        if ref:
            pos[0] += len(ref.group(0))
            value = value_at(f"{ref.group(1).upper()}{ref.group(2)}")
            if value is None:
                raise _Unsupported()
            return value
        number = re.match(r"[0-9]+(?:\.[0-9]+)?(?![A-Za-z0-9_:])", rest)
        if number:
            pos[0] += len(number.group(0))
            return js.num_to_str(float(number.group(0)))
        raise _Unsupported()

    try:
        out = expr()
        space()
        return out if pos[0] == len(src) else None
    except _Unsupported:
        return None
    except RecursionError:
        return None


def _shown_text(ws: Any, coord: str, depth: int = 0) -> Optional[str]:
    """A cell as the sheet shows it, as text: a number in its plain form, a formula by what it gives (see formula_text), '' when empty."""
    cell = ws.cells.get(coord)
    if cell is None or cell.value is None or cell.value == "":
        return ""
    if cell.kind == "n":
        return js.num_to_str(js.number(cell.value))
    if cell.kind == "s":
        return cell.value
    if cell.kind == "f" and depth == 0:
        return formula_text(cell.value, lambda ref: _shown_text(ws, ref, depth + 1))
    return None


def _row_at(snapshot: Snapshot, sheet: str, cols: Columns, row: int) -> Optional[dict]:
    cell = snapshot.cell(sheet, row, cols["number"])
    raw = cell.value if cell is not None else None
    if not raw or not re.fullmatch(r"[0-9]+", raw):
        return None
    ws = snapshot.sheet(sheet)

    def text(col: Optional[int]) -> str:
        if not col:
            return ""
        shown = _shown_text(ws, f"{column_letter(col)}{row}")
        return js.trim(shown if shown is not None else "")

    name = None
    if cols.get("name"):
        shown = _shown_text(ws, f"{column_letter(cols['name'])}{row}")
        name = js.trim(shown) if shown else None
        name = name or None
    return {"sheet": sheet, "row": row, "number": int(raw), "name": name, "supplier": text(cols.get("supplier")), "reviewer": text(cols.get("creator")),
            "keyword": text(cols.get("keyword")), "head_reference": text(cols.get("head_reference")),
            "creative_reference": text(cols.get("creative_reference")), "primary_tag": text(cols.get("primary_tag")),
            "secondary_tag": text(cols.get("secondary_tag")), "creative_form": text(cols.get("creative_form"))}


def progress_rows(snapshot: Snapshot) -> Iterator[dict]:
    """Every row of every usable GOFCN tab that has a material number, in sheet order."""
    for sheet, cols in scan_sheets(snapshot)[0].items():
        for row in range(2, snapshot.last[sheet] + 1):
            found = _row_at(snapshot, sheet, cols, row)
            if found:
                yield found


def lookup_rows(snapshot: Snapshot, numbers: Iterable[int], bodies: Iterable[str] = ()) -> tuple[dict[int, list[dict]], dict[str, list[dict]]]:
    """
    The progress sheet rows of the wanted material numbers, and of the wanted 素材名称 without their number, from every usable GOFCN tab (a
    number on two rows is two rows), in one pass over the sheet. Returns (by_number, by_body).
    """
    wanted = set(numbers)
    names = set(bodies)
    by_number: dict[int, list[dict]] = {}
    by_body: dict[str, list[dict]] = {}
    if not wanted and not names:
        return by_number, by_body
    for entry in progress_rows(snapshot):
        if entry["number"] in wanted:
            by_number.setdefault(entry["number"], []).append(entry)
        body = name_body(entry["name"])
        if body is not None and body in names:
            by_body.setdefault(body, []).append(entry)
    return by_number, by_body


def progress_rows_from(snapshot: Snapshot, numbers: Iterable[int]) -> dict[int, list[dict]]:
    """The progress sheet rows of the wanted material numbers, from every usable GOFCN tab (a number on two rows is two rows)."""
    return lookup_rows(snapshot, numbers)[0]


MAX_LISTED = 40


def search_progress_rows(snapshot: Snapshot, numbers: list[int], texts: list[str]) -> dict:
    """
    The rows `gof creative rows` lists, for working out which row a file is: those of the given numbers, and those whose 素材名称 or 提炼词
    contains one of the texts (newest numbers first, at most 40; `more` says how many were left out).
    """
    wanted = set(numbers)
    needles = [t.lower() for t in texts]
    by_number: list[dict] = []
    by_text: list[dict] = []
    for entry in progress_rows(snapshot):
        if entry["number"] in wanted:
            by_number.append(entry)
        elif any(t in (entry["name"] or "").lower() or t in entry["keyword"].lower() for t in needles):
            by_text.append(entry)
    by_text.sort(key=lambda r: -r["number"])
    room = max(0, MAX_LISTED - len(by_number))
    by_number.sort(key=lambda r: r["number"])
    return {"rows": [*by_number, *by_text[:room]], "not_found": [n for n in numbers if not any(r["number"] == n for r in by_number)],
            "more": max(0, len(by_text) - room)}


def parse_numbers(text: str) -> list[int]:
    """Material numbers as a command line gives them: "67680-67683,67690", "GOFCN67680-GOFCN67683" (a CLS requirement name), "67680 67681"."""
    out: list[int] = []
    for part in [p for p in re.split(r"[,，、;；\s]+", text) if p]:
        m = re.fullmatch(r"(?:GOFCN)?([0-9]{1,9})(?:[-~～至](?:GOFCN)?([0-9]{1,9}))?", part, re.IGNORECASE)
        need(m, f"cannot read the number {part} (write 67680, GOFCN67680 or 67680-67683)")
        start = int(m.group(1))
        stop = start if m.group(2) is None else int(m.group(2))
        need(start > 0 and stop >= start and stop - start < 500, f"invalid number range: {part}")
        for n in range(start, stop + 1):
            if n not in out:
                out.append(n)
    need(0 < len(out) <= 500, "give 1 to 500 numbers")
    return out


#: How many files a refusal names for each problem: the agent works out a row for every file it is told of, so it is told of them all.
LISTED_FILES = 30


def _grouped(problems: list[dict]) -> str:
    """Problems grouped by what is wrong, each with the files it concerns ("创意人 not decided: a.mp4, b.mp4 and 38 more files")."""
    by_reason: dict[str, list[str]] = {}
    for p in problems:
        by_reason.setdefault(p["reason"], [])
        if p.get("file"):
            by_reason[p["reason"]].append(p["file"])
    out = []
    for reason, files in by_reason.items():
        if not files:
            out.append(reason)
        else:
            more = f" and {len(files) - LISTED_FILES} more files" if len(files) > LISTED_FILES else ""
            out.append(f"{reason}: {', '.join(files[:LISTED_FILES])}{more}")
    return "; ".join(out)


def _unusable(name: Optional[str]) -> Optional[str]:
    """Why a 素材名称 cannot name a creative, or None when it can."""
    if not name:
        return "is empty or cannot be worked out"
    if re.search(r"[\\/]", name):
        return "contains / or \\ (a CLS creative name cannot)"
    return None


def _named_as(material: dict, name: str) -> bool:
    """Whether the file is already named as this 素材名称 (letter case aside, and what the page strips)."""
    return BIDI.sub("", name).lower() == material["name"].lower()


def _number_by_name(body: str, named: dict[str, list[dict]], variants: bool) -> dict:
    """
    The one row named GOFCN<number>_<body>, for a file named GOFCNxxxxx_<body>: its number and the body it was found by. With variants, a
    file without a row of its own name is a variant of the one row named as the start of it (cut at a _, see variant_bases). Refused when
    there is none yet (the files are to be registered first: their rows appended to the progress sheet, no CLS ticket) or several.
    """

    def numbers_of(b: str) -> list[int]:
        return sorted({r["number"] for r in named.get(b, [])})

    base = body
    if variants and not numbers_of(body):
        bases = [b for b in variant_bases(body) if numbers_of(b)]
        listing = "; ".join(", ".join(f"GOFCN{n}_{b}" for n in numbers_of(b)) for b in bases)
        need(len(bases) <= 1, f"the file name \"GOFCNxxxxx_{body}\" can be a variant of several progress-sheet rows ({listing}): name the row with items[].number")
        if bases:
            base = bases[0]
    numbers = numbers_of(base)
    need(len(numbers) > 0, f"the file name's GOFCN number is open and the progress sheet has no row whose 素材名称 is \"GOFCN<number>_{body}\"{' or its start (the row the variants merge into)' if variants else ''}: register these materials first (append their rows, no CLS ticket), then upload; for rows registered under another name, name the row with items[].number")
    need(len(numbers) == 1, f"{len(numbers)} progress-sheet rows have the 素材名称 \"GOFCN<number>_{base}\" ({', '.join(f'GOFCN{n}' for n in numbers)}): name the row with items[].number")
    return {"number": numbers[0], "body": base}


def _variant_tail(material: dict, row: dict) -> Optional[str]:
    """
    With variants: what follows the row's 素材名称 in the file's name, its number filled in (GOFCNxxxxx_…, or the row's own number), ''
    when the file is named as the row, None when its name does not start with the row's.
    """
    pending = pending_body(material["name"])
    if pending is not None:
        filled = f"GOFCN{row['number']}_{pending}"
    elif material["number"] == row["number"]:
        filled = material["name"]
    else:
        filled = None
    if filled and row["name"]:
        start = BIDI.sub("", row["name"])
        if filled.lower().startswith(start.lower()):
            return filled[len(start):]
    return None


def _row_of(material: dict, item: dict, rows: dict[int, list[dict]], named: dict[str, list[dict]], variants: bool) -> tuple[dict, str]:
    """
    The progress sheet row a file is: the one the request names for it (`items[].number`, worked out from the requirement), else the one
    the GOFCN number in its file name points at, but only when the file is already named as that row, since a number in a file name can be
    the original the piece builds on; a file named GOFCNxxxxx_… (its number not known when it was named) is the one row named so with a
    number. A number on several rows is one row when they all say the same (a row carried into another month's tab); otherwise
    `items[].sheet` picks the tab. With variants, a file may also be a variant of the row: named as the start of its name (see
    _number_by_name and _variant_tail). Refused, saying what to do, when there is no such row or it cannot be told which.
    """
    pending = pending_body(material["name"]) if "number" not in item else None
    by = "item" if "number" in item else "name" if pending is not None else "file_name"
    by_name = _number_by_name(pending, named, variants) if pending is not None else None
    number = item["number"] if "number" in item else by_name["number"] if by_name else material["number"]
    need(number is not None, "the file name is no 素材名称 of the progress sheet and has no GOFCN number: work out its row from the requirement documents and scripts and give it in items[].number")
    found = rows.get(number)
    if found is None:
        found = [r for r in named.get(by_name["body"] if by_name else "", []) if r["number"] == number] if named.get(by_name["body"] if by_name else "") is not None else []
    need(len(found) > 0, f"the progress sheet has no GOFCN{number}{' (the number in the file name)' if by == 'file_name' else ''}")
    if "sheet" in item:
        tabs = list(dict.fromkeys(r["sheet"] for r in found))
        found = [r for r in found if r["sheet"] == item["sheet"]]
        need(len(found) > 0, f"the progress-sheet tab \"{item['sheet']}\" has no GOFCN{number} (it is on {', '.join(tabs)})")
    row = found[0]
    where = ", ".join(f"{r['sheet']} row {r['row']}" for r in found)
    advice = "name the tab with items[].sheet" if len({r["sheet"] for r in found}) == len(found) else "a number repeated within one tab: the user checks the progress sheet, then plan again"
    need(all(r["name"] == row["name"] and r["supplier"] == row["supplier"] and r["reviewer"] == row["reviewer"] for r in found),
         f"GOFCN{number} is on {len(found)} progress-sheet rows that differ ({where}): {advice}")
    need(by != "file_name" or not row["name"] or _named_as(material, row["name"]) or (variants and _variant_tail(material, row) is not None),
         f"the row of GOFCN{number} (the number in the file name) has the 素材名称 \"{js.to_str(row['name'])}\", not the file's name{', and the file name does not start with it (no variant of it)' if variants else ''}: the number may be the earlier piece it builds on. Work out the file's row from the requirement documents and give it in items[].number (GOFCN{number} too, if that is the row)")
    return row, by


def _listed_row(r: dict) -> dict:
    """A row as the commands list it to the agent."""
    return {"number": r["number"], "name": r["name"], "where": f"{r['sheet']} row {r['row']}"}


def registered_row(material: dict, by_number: dict[int, list[dict]], by_body: dict[str, list[dict]]) -> Optional[dict]:
    """
    Whether a file named for its row is already in the progress sheet, before any row is planned for it: a batch whose files carry their
    GOFCN numbers has most likely been registered already. `registered`: a row is named as the file (for GOFCNxxxxx_…, but for the number),
    so the file is only uploaded; `variant`: the file's name starts with the 素材名称 of its number's row; `differs`: its number's row is
    named otherwise (the number may be the original the piece builds on, or either name was changed); `not_found`: no row has its number.
    Rows named as the file under another number are listed as `same_name_rows`. None for a file without a number, and a GOFCNxxxxx_… file
    of no row. `by_number` / `by_body` are lookup_rows of the files' numbers and of their names without the number.
    """
    pending = pending_body(material["name"])
    if pending is not None:
        rows = by_body.get(pending, [])
        return {"status": "registered", "rows": [_listed_row(r) for r in rows]} if rows else None
    if material["number"] is None:
        return None
    rows = by_number.get(material["number"], [])
    body = name_body(material["name"])
    others = [r for r in (by_body.get(body, []) if body is not None else []) if r["number"] != material["number"]]
    elsewhere = {"same_name_rows": [_listed_row(r) for r in others]} if others else {}
    if not rows:
        return {"status": "not_found", **elsewhere}
    if any(r["name"] and _named_as(material, r["name"]) for r in rows):
        return {"status": "registered", "rows": [_listed_row(r) for r in rows if r["name"] and _named_as(material, r["name"])]}
    variant = [r for r in rows if r["name"] and (_variant_tail(material, r) or "").startswith("_")]
    if variant:
        return {"status": "variant", "rows": [_listed_row(r) for r in variant]}
    return {"status": "differs", "rows": [_listed_row(r) for r in rows], **elsewhere}


def build_upload_plan(data: dict, names: Optional[list[str]] = None) -> dict:
    """
    The sealed plan, or a refusal that lists every problem at once. `names` gets the creative name of each file that got as far as one,
    problems or not, so that the names CLS already has can be asked for before the refusal is made (see plan_upload).

    `data` holds: request, options, materials (with sha256 and md5), skipped, existing (a set of names CLS has), and optionally progress
    (rows by number), named (rows by name body), progress_source, now (a datetime).
    """
    names = [] if names is None else names
    request = data["request"]
    options = data["options"]
    materials = data["materials"]
    existing: set[str] = data.get("existing") or set()
    need(options["can_upload"], f"the CLS account \"{options['account']['name']}\" may not upload creatives in {options['game']['code']}")
    by_sheet = request.get("progress") is not False
    need(not by_sheet or data.get("progress") is not None, "the progress sheet was not read: no 素材名称 can be decided")
    problems: list[dict] = []
    skipped = list(data["skipped"])
    by_file: dict[str, dict] = {}
    for item in request.get("items") or []:
        matches = [m for m in materials if m["file_name"] == item["file"] or m["file"] == os.path.abspath(item["file"]) or m["name"] == item["file"]
                   or m.get("origin") == item["file"]]
        if len(matches) != 1:
            listed = ", ".join(m.get("origin") or m["file"] for m in matches)
            problems.append({"reason": f"items names \"{item['file']}\", which {f'matches several files ({listed}): name one of them' if matches else 'is not among the files to upload'}"})
            continue
        by_file[matches[0]["file"]] = item
    default_directory = find_directory(options["directories"], request.get("directory") or DEFAULT_DIRECTORY)
    items: list[dict] = []
    named: dict[str, str] = {}
    by_content: dict[str, str] = {}
    for material in materials:
        item = by_file.get(material["file"]) or {"file": material["file_name"]}
        if item.get("skip"):
            skipped.append({"file": material["file"], "reason": "skipped as asked"})
            continue
        label = material.get("origin") or material["file_name"]
        # CLS makes one creative of one content (by its MD5): the second copy would be refused at the create, after its row was registered.
        twin = by_content.get(material["md5"])
        if twin:
            problems.append({"reason": f"the same content as {twin} (one file twice): CLS makes one creative of one content, so upload one and give the other items[].skip", "file": label})
            continue
        by_content[material["md5"]] = label
        bound = None
        try:
            if by_sheet:
                bound = _row_of(material, item, data["progress"], data.get("named") or {}, request.get("variants") is True)
        except Blocked as error:
            problems.append({"reason": error.message, "file": label})
            continue
        row = bound[0] if bound else None
        # The name is the row's 素材名称. Only a row whose 素材名称 cannot name a creative takes the one the user confirmed (items[].name);
        # without the sheet, the file's own name or items[].name.
        why = _unusable(row["name"]) if row else None
        if row and why and "name" not in item:
            problems.append({"reason": f"the 素材名称 of GOFCN{row['number']} ({row['sheet']} row {row['row']}) {why}: the user fixes the progress sheet and the upload is planned again, or the user confirms a name, given in items[].name", "file": label})
            continue
        if row and not why and "name" in item:
            problems.append({"reason": "that progress-sheet row has a 素材名称, which is the name: drop items[].name", "file": label})
            continue
        # With variants, a file goes up as one variant of its row: the 素材名称 and what follows it in the file's name, or items[].variant.
        tail = ""
        if request.get("variants") and row and not why:
            tail = f"_{item['variant']}" if "variant" in item else (_variant_tail(material, row) or "")
        # Every name is checked, whatever else is wrong with the file, so that the user hears of all of it at once.
        name = BIDI.sub("", row["name"] + tail if row and not why else item.get("name") or material["name"])
        names.append(name)
        renamed = name != material["name"]
        key = name.lower()
        if key in named:
            if not row:
                reason = f"two files would get the same creative name \"{name}\" ({named[key]}, {label}): rename one or keep one"
            elif request.get("variants"):
                reason = f"two files would get the same creative name \"{name}\" ({named[key]}, {label}): each variant of a row needs its own variant name (give it in items[].variant when the file name has none); of one file keep one"
            else:
                reason = f"two files are the same progress-sheet row GOFCN{row['number']} ({named[key]}, {label}): keep one"
            problems.append({"reason": reason})
        else:
            named[key] = label
        if name in existing:
            if not bound:
                reason = "CLS has a creative of this name already (it would refuse the whole upload): rename it or leave it out"
            elif bound[1] != "item":
                reason = "CLS has a creative of this name already, so the file looks uploaded before (CLS would refuse the whole upload): do not upload it again"
            else:
                reason = "CLS has this row's creative already (it would refuse the whole upload): do not upload it again, or check items[].number"
            problems.append({"file": f"{label} → {name}" if renamed else name, "reason": reason})
        try:
            need(not re.search(r"[\\/]", name), "a creative name cannot contain / or \\")
            directory = find_directory(options["directories"], item["directory"]) if item.get("directory") else default_directory
            kind_type = _material_type(options["types"], item.get("type"), material["kind"])
            # 创意人 / 制作人: the file's own setting, then the request's, then the file's row of the progress sheet. A finished material was made
            # by someone: a row without a 设计师供应商 gives no 制作人 (never CLS's 待定供应商), which is then to be named.
            creator_name = item.get("creator") if item.get("creator") is not None else request.get("creator") if request.get("creator") is not None else (row["reviewer"] if row else None)
            row_producers = split_names(row["supplier"]) if row else []
            producer_names = item.get("producers") if item.get("producers") is not None else request.get("producers") if request.get("producers") is not None else (row_producers or None)
            row_number = row["number"] if row else None
            no_creator = f" (the 创意者脚本审核人 of GOFCN{row_number} in the progress sheet is empty)" if row else ""
            no_producer = (f" (the 设计师供应商 of GOFCN{row_number} in the progress sheet is empty, as it is when the supplier is the reviewer: ask the user who made the material (most likely the reviewer) "
                           "and give it in items[].producers)") if row else ""
            need(creator_name and js.trim(creator_name), f"创意人 not decided{no_creator}")
            need(producer_names and len(producer_names) > 0, f"制作人 not decided{no_producer}")

            # The sheet's short names are not always CLS's (廿十一 / 长沙廿拾一文化传媒有限公司): a refusal says where the name came from, and
            # to name the person for this file only, since a top-level field would replace every other file's people from its own row too.
            def sheet_says(field: str, column: str) -> str:
                given = item.get(field) if item.get(field) is not None else request.get(field)
                if row and given is None:
                    return (f" (this is the {column} of GOFCN{row['number']} in the progress sheet; when CLS spells it otherwise, give the CLS name in this file's items[].{field}"
                            f" (in the top-level {field} only when it holds for every file); gof creative options lists the people on offer)")
                return ""

            creator = _with_hint(lambda: _person(options["creators"], creator_name, f"创意人 \"{creator_name}\""), sheet_says("creator", "创意者脚本审核人"))
            producers = [_with_hint(lambda n=n: _person(options["producers"], n, f"制作人 \"{n}\""), sheet_says("producers", "设计师供应商")) for n in producer_names]
            need(len({p["id"] for p in producers}) == len(producers), "a 制作人 is given twice")
            source: dict[str, Any] = {"name": "progress" if row and not why else "item" if "name" in item else "file"}
            if tail:
                source["variant"] = "item" if "variant" in item else "file"
            source["creator"] = "item" if "creator" in item else "request" if "creator" in request else "progress"
            source["producers"] = "item" if "producers" in item else "request" if "producers" in request else "progress"
            if bound:
                r0 = bound[0]
                source["progress_row"] = {"sheet": r0["sheet"], "row": r0["row"], "number": r0["number"], "name": r0["name"],
                                          "supplier": r0["supplier"], "reviewer": r0["reviewer"], "by": bound[1]}
            entry: dict[str, Any] = {"file": material["file"], "file_name": material["file_name"]}
            if material.get("origin"):
                entry["origin"] = material["origin"]
            entry.update({"size": material["size"], "mtime_ms": material["mtime_ms"], "sha256": material["sha256"], "md5": material["md5"],
                          "kind": material["kind"], "name": name, "number": row["number"] if row else material["number"]})
            if tail:
                entry["variant"] = re.sub(r"^_", "", tail, count=1)
            # A renamed file goes up under its new name too, as if it had been renamed before the upload; the file on disk stays as it is.
            if renamed:
                ext = ext_of(material["file_name"])
                entry["renamed_from"] = material["name"]
                entry["upload_name"] = f"{name}{'.' + ext if ext else ''}"
            entry.update({"type": kind_type, "directory": directory, "creator": creator, "producers": producers, "source": source})
            items.append(entry)
        except Blocked as error:
            problems.append({"reason": error.message, "file": label})
    if not items and not problems:
        listed = "; ".join(f"{os.path.basename(s['file'])} ({s['reason']})" for s in skipped)
        raise Blocked("no file to upload" + (f": {listed}" if skipped else ""))
    if problems:
        raise Blocked(_grouped(problems))
    now: datetime = data.get("now") or None
    created = js.iso(now.timestamp() * 1000) if now is not None else js.iso()
    plan: dict[str, Any] = {"schema": 1, "kind": "creative_upload", "created_at": created, "account": options["account"], "game": options["game"],
                            "items": items, "skipped": skipped}
    if data.get("progress_source"):
        plan["progress_source"] = data["progress_source"]
    if request.get("variants"):
        plan["variants"] = True
    return seal(plan, "plan_id")


def load_upload_plan(file: str) -> dict:
    plan = read_json(file)
    need(isinstance(plan, dict) and plan.get("schema") == 1 and plan.get("kind") == "creative_upload" and isinstance(plan.get("items"), list)
         and len(plan["items"]) > 0, "not an upload plan file")
    check_seal(plan, "plan_id")
    return plan


def shown_file(file: str) -> str:
    """A skipped file as the user knows it: a file on disk by its name, an entry of a zip as "素材.zip：子文件夹/a.mp4"."""
    return os.path.basename(file) if os.path.isabs(file) else file


def upload_plan_summary(plan: dict, file: str, limit: int = 40) -> dict:
    """
    What the user is shown before approving: counts by type, creator and producer, the directory, how many files go up under another name,
    and each file in short (its name in CLS, and the progress sheet row it is: `row_from` request = as the request said, file_name = by the
    GOFCN number in the file name, name = by the rest of a GOFCNxxxxx_… file name). With variants, how many rows the files are of, and each
    file's variant.
    """

    def count(values: list[str]) -> dict[str, int]:
        out: dict[str, int] = {}
        for v in values:
            out[v] = out.get(v, 0) + 1
        return out

    items = plan["items"]
    renamed = len([i for i in items if "renamed_from" in i])
    result: dict[str, Any] = {
        "status": "upload_plan", "plan": file, "count": len(items),
        "directories": count([i["directory"]["path"] for i in items]),
        "types": count([i["type"]["name"] for i in items]),
        "creators": count([i["creator"]["name"] for i in items]),
        "producers": count(["、".join(p["name"] for p in i["producers"]) for i in items]),
    }
    if plan.get("progress_source"):
        result["progress_source"] = plan["progress_source"]
    if renamed:
        result["renamed"] = renamed
    if plan.get("variants"):
        def row_key(i: dict) -> str:
            row = i["source"].get("progress_row")
            return f"{js.to_str(row.get('sheet') if row else js.UNDEFINED)}/{js.to_str(row.get('row') if row else js.UNDEFINED)}"

        result["rows"] = len({row_key(i) for i in items})
    listed = []
    for i in items[:limit]:
        entry: dict[str, Any] = {"file": i.get("origin") or i["file_name"], "name": i["name"]}
        if "variant" in i:
            entry["variant"] = i["variant"]
        if "renamed_from" in i:
            entry["renamed"] = True
        entry.update({"type": i["type"]["name"], "creator": i["creator"]["name"], "producers": [p["name"] for p in i["producers"]]})
        row = i["source"].get("progress_row")
        if row:
            entry["progress_row"] = f"{row['sheet']} row {row['row']}"
            entry["row_from"] = "request" if row["by"] == "item" else row["by"]
        listed.append(entry)
    result["items"] = listed
    if len(items) > limit:
        result["more_items"] = len(items) - limit
    if plan["skipped"]:
        result["skipped"] = [{"file": shown_file(s["file"]), "reason": s["reason"]} for s in plan["skipped"]]
    return result


# ---- the batch journal ------------------------------------------------------------------------------------------------------------


def upload_events(directory: str) -> list[dict]:
    """The events of an upload batch, in order, each chained to the one before: a missing, reordered or edited event is refused."""
    folder = os.path.join(directory, "events")
    if not os.path.exists(folder):
        return []
    files = sorted((f for f in os.listdir(folder) if f.endswith(".json")), key=js.utf16_key)
    records: list[dict] = []
    previous = ""
    for offset, file in enumerate(files):
        need(file == f"{str(offset + 1).zfill(6)}.json", "an upload record is missing or out of order")
        event = read_json(os.path.join(folder, file))
        token = event.get("event_id")
        rest = {k: v for k, v in event.items() if k != "event_id"}
        need(rest.get("previous") == previous and digest(rest) == token, "an upload record was modified")
        records.append({**rest, "event_id": token})
        previous = token
    return records


def append_upload_event(directory: str, step: str, data: Optional[dict] = None, now: Optional[Callable[[], datetime]] = None) -> dict:
    data = {} if data is None else data
    reject_secrets(data)
    records = upload_events(directory)
    time_text = js.iso(now().timestamp() * 1000) if now else js.iso()
    event: dict[str, Any] = {"step": step, "data": data, "time": time_text, "previous": records[-1]["event_id"] if records else ""}
    event["event_id"] = digest(event)
    os.makedirs(os.path.join(directory, "events"), exist_ok=True)
    write_json_new(os.path.join(directory, "events", f"{str(len(records) + 1).zfill(6)}.json"), event)
    return event
