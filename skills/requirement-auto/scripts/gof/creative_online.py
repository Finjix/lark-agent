"""The commands of the material upload that go online: `gof creative options | rows | files | plan | run | status | screenshot` (creative-online.ts).

`plan` and `rows` only read (CLS options, existing names, the progress sheet); `run` does the upload the approved plan describes. A run keeps
its record in the user's batch directory, so running it again goes on from where it stopped: before the create, the files are simply put up
again; once the create has been sent it is never sent again, and what it did is read back instead.
"""

from __future__ import annotations

import os
import re
import time
from datetime import datetime
from typing import Any, Callable, Optional

from . import js
from .cls_adapter import ClsError
from .creative import (DEFAULT_DIRECTORY, append_upload_event, build_upload_plan, find_directory, flatten_directories, hash_file, is_zip,
                       load_upload_plan, lookup_rows, match_people, material_refusal, name_body, name_parts, normalize_upload_request, pending_body,
                       registered_row, scan_materials, search_progress_rows, shown_file, upload_events, upload_plan_summary, variant_bases)
from .creative_adapter import SEARCH_PAGE, CreativeAdapter
from .snapshot import Snapshot
from .util import Blocked, feishu_doc_path, need, read_json, write_json_new

LIST_URL = "https://creative.centurygames.cn/creativities"


class UploadDeps:
    """Test seams: a local CLS and storage, short waits. Production passes nothing."""

    def __init__(self, adapter: Optional[dict] = None, batch_root: Optional[str] = None, tmp_dir: Optional[str] = None,
                 sleep: Optional[Callable[[float], None]] = None, now: Optional[Callable[[], datetime]] = None, reconcile_tries: Optional[int] = None,
                 shot: Optional[dict] = None) -> None:
        self.adapter = adapter or {}
        self.batch_root = batch_root
        self.tmp_dir = tmp_dir
        self.sleep = sleep
        self.now = now
        #: How long to look for creatives a create of unknown outcome may have made.
        self.reconcile_tries = reconcile_tries
        self.shot = shot or {}


def _tmp_dir(deps: UploadDeps) -> str:
    if deps.tmp_dir is not None:
        return deps.tmp_dir
    from .paths import resolve_data_paths

    return resolve_data_paths().tmp


def _adapter_for(session: dict, deps: UploadDeps, **extra: Any) -> CreativeAdapter:
    return CreativeAdapter(session["origin"], session["authorization"], **{**deps.adapter, **extra})


def read_options(api: CreativeAdapter, session: dict) -> dict:
    """What the page 上传创意 offers this account in GOFCN: upload directories, 创意人 and 制作人 candidates, 素材类型."""
    games, account, permissions = api.games(), api.account(), api.permissions()
    need(isinstance(games, list) and js.truthy(account) and isinstance(permissions, dict), "CLS 返回结构变化，停止")
    game = next((g for g in games if g.get("code") == "GOFCN"), None)
    need(game and js.is_safe_int(game.get("id")), f"CLS 账号“{session['account']['name']}”没有 GOFCN 项目的创意权限")
    need(account.get("id") == session["account"]["id"] and account.get("status") == 1, "CLS 当前账号无法核对")
    # The page goes by the role this account has in the game's group; without one, by its own roles.
    group = next((p for p in (account.get("group_permissions") or []) if isinstance(p.get("project_ids"), list) and game["id"] in p["project_ids"]), None)
    roles = [group["role_id"]] if group and group.get("role_id") else (account.get("roles") or [])
    granted = {p for role in roles for p in (permissions.get(role) or [])}
    pages = ((group or {}).get("permissions") or {}).get("pages") if isinstance((group or {}).get("permissions"), dict) else None
    directories, people, types = api.directories(game["id"]), api.people(game["id"]), api.types(game["id"])
    need(isinstance(directories, list) and isinstance((people or {}).get("creative_users"), list) and isinstance((people or {}).get("producers"), list)
         and isinstance((types or {}).get("types"), list), "CLS 上传选项结构变化，停止")

    def pick(items: list[dict]) -> list[dict]:
        return [{"id": p["id"], "name": js.to_str(p.get("name"))} for p in items if js.is_safe_int(p.get("id"))]

    return {
        "account": {"id": account["id"], "name": account.get("name")}, "game": {"id": game["id"], "code": game["code"], "name": game.get("name")},
        "can_upload": "creative.upload" in granted and (not isinstance(pages, list) or "PAGE_MODULE_CREATIVE" in pages),
        "directories": directories, "creators": pick(people["creative_users"]), "producers": pick(people["producers"]),
        "types": [{"id": js.to_str(t.get("id")), "name": js.to_str(t.get("name"))} for t in types["types"]],
    }


def upload_options(online: Any, find: Optional[str] = None, creator: Optional[str] = None, producers: Optional[list[str]] = None,
                   deps: Optional[UploadDeps] = None) -> dict:
    """
    `gof creative options`: what the page offers. `find` looks for directories by name; `creator` / `producers` are matched to the people
    the page offers (`people`: the CLS name of each, or the problem), as the plan would match them.
    """
    deps = deps or UploadDeps()
    session = online.session()
    options = read_options(_adapter_for(session, deps), session)
    wanted: dict[str, Any] = {}
    if creator is not None:
        wanted["creator"] = creator
    if producers is not None:
        wanted["producers"] = producers
    try:
        default_directory: Optional[dict] = find_directory(options["directories"], DEFAULT_DIRECTORY)
    except Blocked:
        default_directory = None  # reported as null
    every = flatten_directories(options["directories"])
    # Without --find only the first level under the game; with it, the directories whose own name contains the text, shallowest first.
    if find:
        directories = [d["path"] for d in sorted((d for d in every if find in d["name"]), key=lambda d: len(d["path"].split(" / ")))[:50]]
    else:
        directories = [d["path"] for d in every if len(d["path"].split(" / ")) == 2]
    result = {"status": "creative_options", "account": options["account"], "game": options["game"], "can_upload": options["can_upload"],
              "default_directory": default_directory, "directories": directories, "creators": [p["name"] for p in options["creators"]],
              "producers": [p["name"] for p in options["producers"]], "types": [t["name"] for t in options["types"]]}
    if wanted:
        result["people"] = match_people(options, wanted)
    return result


#: How many pages of a name search are read at most (1000 creatives): a short name (01, 封面) is in the names of very many.
SEARCH_PAGES = 20


def _same_name(api: CreativeAdapter, game_id: int, name: str, confirmed: bool, first: bool = False) -> list[dict]:
    """
    The creatives named exactly so, confirmed or waiting for confirmation (待确认). The search finds names that contain the text, newest first,
    so the one of exactly this name can be on any page: they are read until the list ends (or, with `first`, until one is found).
    """
    hits: list[dict] = []
    for page in range(1, SEARCH_PAGES + 1):
        data = api.search(game_id, name, confirmed, page)
        items = data.get("creatives") if isinstance(data, dict) and isinstance(data.get("creatives"), list) else []
        hits.extend(c for c in items if c.get("name") == name)
        total = data.get("total") if isinstance(data, dict) else None
        if (first and hits) or len(items) < SEARCH_PAGE or (js.is_num(total) and page * SEARCH_PAGE >= total):
            break
    return hits


def existing_names(api: CreativeAdapter, game_id: int, names: list[str]) -> set[str]:
    """Exact names CLS already has a creative of, confirmed or waiting for confirmation (待确认)."""
    found: set[str] = set()
    for name in dict.fromkeys(names):
        for confirmed in (True, False):
            if name not in found and _same_name(api, game_id, name, confirmed, True):
                found.add(name)
    return found


def _sheet_token(bridge: Any, url: str) -> dict:
    """The spreadsheet a progress sheet link (wiki node or sheets link) points at."""
    doc_path = feishu_doc_path(url, "wiki|sheets", True)
    need(doc_path, "进度表链接需要是公司飞书的 wiki 或 sheets 链接")
    parts = doc_path.rstrip("/").split("/")
    kind, token = parts[1], parts[2]
    if kind == "sheets":
        return {"token": token, "title": ""}
    node = (bridge.api_read("/open-apis/wiki/v2/spaces/get_node", {"token": token, "obj_type": "wiki"}) or {}).get("node") or {}
    need(node.get("obj_type") == "sheet" and re.fullmatch(r"[A-Za-z0-9]+", js.to_str(node.get("obj_token") if node.get("obj_token") is not None else "")),
         "这个链接不是电子表格")
    return {"token": node["obj_token"], "title": js.to_str(node["title"]) if node.get("title") is not None else ""}


def _read_progress_sheet(bridge: Any, source: Any, tmp_dir: str, use: Callable[[Snapshot, str], Any]) -> Any:
    """A fresh export of the skill's progress sheet, or of the sheet the user linked, read by `use` and removed again."""
    from .feishu import target

    bridge.doctor()
    found = {"token": target(bridge), "title": ""} if source is True else _sheet_token(bridge, source)
    os.makedirs(tmp_dir, exist_ok=True)
    file = os.path.join(tmp_dir, f"progress-lookup-{js.now_ms()}-{os.getpid()}.xlsx")
    bridge.export_sheet(found["token"], file)
    need(os.path.exists(file), "进度表导出没有落盘")
    try:
        label = "插件进度表" if source is True else f"{found['title'] or '用户给的进度表'}（{source}）"
        return use(Snapshot(file), label)
    finally:
        try:
            os.remove(file)
        except OSError:
            pass


def progress_lookup(bridge: Any, source: Any, numbers: list[int], tmp_dir: str, bodies: Optional[list[str]] = None) -> dict:
    """
    The progress sheet rows of these numbers, and of these 素材名称 without their number: read from a fresh export of the skill's progress
    sheet, or of the sheet the user linked. Returns {"rows", "named", "label"}.
    """
    def use(snapshot: Snapshot, label: str) -> dict:
        by_number, by_body = lookup_rows(snapshot, numbers, bodies or [])
        return {"rows": by_number, "named": by_body, "label": label}

    return _read_progress_sheet(bridge, source, tmp_dir, use)


def find_rows(bridge: Any, numbers: list[int], texts: list[str], progress: Any = True, deps: Optional[UploadDeps] = None) -> dict:
    """
    `gof creative rows`: the progress sheet rows of the given numbers and those whose 素材名称 or 提炼词 contains one of the texts, for
    working out which row each file is before planning. Reads only.
    """
    deps = deps or UploadDeps()
    need(len(numbers) > 0 or len(texts) > 0, "rows 需要 --numbers 或 --find")

    def use(snapshot: Snapshot, label: str) -> dict:
        found = search_progress_rows(snapshot, numbers, texts)
        result: dict[str, Any] = {"status": "progress_rows", "source": label, "count": len(found["rows"]),
                                  "rows": [{"number": r["number"], "name": r["name"], "keyword": r["keyword"], "head_reference": r["head_reference"],
                                            "creative_reference": r["creative_reference"], "primary_tag": r["primary_tag"], "secondary_tag": r["secondary_tag"],
                                            "creative_form": r["creative_form"], "supplier": r["supplier"], "reviewer": r["reviewer"],
                                            "where": f"{r['sheet']} 第 {r['row']} 行"} for r in found["rows"]]}
        if found["not_found"]:
            result["not_found"] = found["not_found"]
        if found["more"]:
            result["more"] = found["more"]
        return result

    return _read_progress_sheet(bridge, progress if progress is not None else True, _tmp_dir(deps), use)


def _unpack_archives(inputs: list[str], root: str) -> dict:
    """
    The zips among the given files (or directly inside a given folder), unpacked: zip path → its folder, what inside cannot go up, and the
    files that had to be stored under another name (characters a Windows file name cannot hold).
    """
    from .unzip import unpack_zip

    zips: list[str] = []
    for item in inputs:
        full = os.path.abspath(item)
        if os.path.isfile(full) and is_zip(full):
            zips.append(full)
        elif os.path.isdir(full):
            with os.scandir(full) as it:
                for entry in it:
                    if entry.is_file() and is_zip(entry.name):
                        zips.append(os.path.join(full, entry.name))
    unpacked: dict[str, str] = {}
    skipped: list[dict] = []
    renamed: list[dict] = []
    for archive in zips:
        result = unpack_zip(archive, root, material_refusal)
        unpacked[archive.lower()] = result.dir
        skipped.extend(result.skipped)
        renamed.extend(result.renamed)
    return {"unpacked": unpacked, "skipped": skipped, "renamed": renamed}


def _gather(inputs: list[str], deps: UploadDeps) -> dict:
    """The files behind what the user gave, zips unpacked into this user's tmp (a folder reaches lark-agent only as a zip), and what cannot go up."""
    from .retention import UNPACKED

    archives = _unpack_archives(inputs, os.path.join(_tmp_dir(deps), UNPACKED))
    materials, skipped = scan_materials(inputs, archives["unpacked"])
    return {"materials": materials, "skipped": [*skipped, *archives["skipped"]], "renamed": archives["renamed"]}


def _same_content(materials: list[dict]) -> dict[str, list[str]]:
    """
    Files of exactly the same content (one file twice: under two names, or given loose and in a zip), as file → the others. CLS makes one
    creative of one content, so such files are one row and one upload. Only files of the same size are read.
    """
    by_size: dict[int, list[dict]] = {}
    for m in materials:
        by_size.setdefault(m["size"], []).append(m)
    by_hash: dict[str, list[dict]] = {}
    for group in by_size.values():
        if len(group) < 2:
            continue
        for m in group:
            by_hash.setdefault(hash_file(m["file"])["md5"], []).append(m)
    twins: dict[str, list[str]] = {}
    for group in by_hash.values():
        if len(group) > 1:
            for m in group:
                twins[m["file"]] = [o.get("origin") or o["file_name"] for o in group if o is not m]
    return twins


def list_files(inputs: list[str], deps: Optional[UploadDeps] = None, check: Optional[dict] = None) -> dict:
    """
    `gof creative files`: what the given files, folders and zips hold, file by file, for planning from the files themselves before their rows
    exist (a file named GOFCNxxxxx_<name> is made for the row that will be called GOFCN<number>_<name>). Reads only, offline; with `check`
    (`--check`: {"bridge", "progress"}) it also reads the progress sheet (about a minute) and says of each file named for its row whether that
    row is already there (`row`, see registered_row): files that carry their numbers have most likely been registered, and are verified
    rather than appended.
    """
    deps = deps or UploadDeps()
    gathered = _gather(inputs, deps)
    materials, skipped, renamed = gathered["materials"], gathered["skipped"], gathered["renamed"]
    twins = _same_content(materials)
    bodies = [b for b in ((pending_body(m["name"]) if pending_body(m["name"]) is not None else name_body(m["name"])) for m in materials) if b is not None]
    numbers = [m["number"] for m in materials if m["number"] is not None]
    sheet = progress_lookup(check["bridge"], check["progress"], numbers, _tmp_dir(deps), bodies) if check and (numbers or bodies) else None
    listed = []
    for m in materials:
        pending = pending_body(m["name"])
        row = registered_row(m, sheet["rows"], sheet["named"]) if sheet else None
        entry: dict[str, Any] = {"file": m.get("origin") or m["file_name"], "path": m["file"], "name": m["name"], "kind": m["kind"], "size": m["size"]}
        if m["number"] is not None:
            entry["number"] = m["number"]
        if pending is not None:
            entry.update({"pending_number": True, "name_without_number": pending, "parts": name_parts(pending)})
        if m["file"] in twins:
            entry["same_content"] = twins[m["file"]]
        if row:
            entry["row"] = row
        listed.append(entry)
    registered = len([f for f in listed if (f.get("row") or {}).get("status") == "registered"])
    result: dict[str, Any] = {"status": "files", "count": len(materials), "files": listed}
    if check:
        result.update({"progress_source": sheet["label"] if sheet else None, "registered": registered,
                       "to_verify": len([f for f in listed if f.get("row") and f["row"]["status"] != "registered"])})
    if skipped:
        result["skipped"] = [{"file": shown_file(s["file"]), "reason": s["reason"]} for s in skipped]
    if renamed:
        result["zip_renamed"] = renamed
    return result


def plan_upload(online: Any, bridge: Any, request_file: str, output: str, deps: Optional[UploadDeps] = None) -> dict:
    """Reads the request, the files and what CLS offers, and writes the sealed plan the user approves. Sends nothing."""
    deps = deps or UploadDeps()
    file = os.path.abspath(output)
    need(not os.path.exists(file), "计划文件已存在，不覆盖")
    request = normalize_upload_request(read_json(request_file))
    gathered = _gather(request["files"], deps)
    materials, skipped, renamed = gathered["materials"], gathered["skipped"], gathered["renamed"]
    hashed = []
    for material in materials:
        digest = hash_file(material["file"])
        need(digest["size"] == material["size"], f"读取期间文件大小变化：{material['file']}")
        hashed.append({**material, "sha256": digest["sha256"], "md5": digest["md5"]})
    session = online.session()
    api = _adapter_for(session, deps)
    options = read_options(api, session)
    data: dict[str, Any] = {"request": request, "options": options, "materials": hashed, "skipped": skipped, "existing": set()}
    if request["progress"]:
        need(bridge is not None, "这个环境不能读飞书进度表")
        # Every row a file may be: the one the request names for it, the one its file name's number points at, and for a file named
        # GOFCNxxxxx_<name>, the one named GOFCN<number>_<name> (with variants, also those named as the start of it).
        numbers = list(dict.fromkeys(n for n in [*[i.get("number") for i in request.get("items") or []], *[m["number"] for m in hashed]] if js.is_num(n)))
        bodies: list[str] = []
        for m in hashed:
            body = pending_body(m["name"])
            if body is None:
                continue
            for b in ([body, *variant_bases(body)] if request.get("variants") else [body]):
                if b not in bodies:
                    bodies.append(b)
        lookup = progress_lookup(bridge, request["progress"], numbers, _tmp_dir(deps), bodies)
        data.update({"progress": lookup["rows"], "named": lookup["named"], "progress_source": lookup["label"]})
    if deps.now:
        data["now"] = deps.now()
    # The names come out of the plan itself (a file may be renamed), so they are known only after a first pass. They are asked for even
    # when that pass found other problems, so that a name CLS already has is in the same refusal as the rest.
    names: list[str] = []
    first: Optional[dict] = None
    try:
        first = build_upload_plan(data, names)
    except Blocked:
        pass
    existing = existing_names(api, options["game"]["id"], names)
    plan = first if first is not None and not existing else build_upload_plan({**data, "existing": existing})
    os.makedirs(os.path.dirname(file), exist_ok=True)
    write_json_new(file, plan)
    result = upload_plan_summary(plan, file)
    if renamed:
        result["zip_renamed"] = renamed
    return result


# ---- the run ----------------------------------------------------------------------------------------------------------------------


def _batch_of(plan: dict, deps: UploadDeps) -> str:
    root = deps.batch_root
    if root is None:
        from .paths import resolve_data_paths

        root = resolve_data_paths().batches
    return os.path.join(root, f"upload-{js.to_str(plan['plan_id'])[:24]}")


def _open_batch(plan: dict, deps: UploadDeps) -> str:
    """Opens (or starts) the batch of an approved plan: the batch holds its own copy of the plan, and only that plan's."""
    directory = _batch_of(plan, deps)
    copy = os.path.join(directory, "plan.json")
    if os.path.exists(copy):
        need(load_upload_plan(copy)["plan_id"] == plan["plan_id"], "上传批次目录与计划不符")
    else:
        os.makedirs(directory, exist_ok=True)
        write_json_new(copy, plan)
        append_upload_event(directory, "started", {"plan_id": plan["plan_id"], "files": len(plan["items"])}, deps.now)
    return directory


def _still_offered(plan: dict, options: dict) -> None:
    """The plan's people, directories and types must still be on offer, and the account still able to upload."""
    need(options["can_upload"], "CLS 账号已没有上传创意的权限")
    need(options["game"]["id"] == plan["game"]["id"], "GOFCN 项目变化")
    dirs = {d["id"] for d in flatten_directories(options["directories"])}
    creators = {p["id"] for p in options["creators"]}
    producers = {p["id"] for p in options["producers"]}
    types = {t["id"] for t in options["types"]}
    for item in plan["items"]:
        need(item["directory"]["id"] in dirs, f"上传目录已不存在：{item['directory']['path']}")
        need(item["creator"]["id"] in creators, f"创意人已不可选：{item['creator']['name']}")
        for p in item["producers"]:
            need(p["id"] in producers, f"制作人已不可选：{p['name']}")
        need(item["type"]["id"] in types, f"素材类型已不可选：{item['type']['name']}")


def _same_ids(a: list, b: list) -> bool:
    return len(a) == len(b) and sorted(a, key=js.number) == sorted(b, key=js.number)


def _check(detail: Any, item: dict, name: str, account: int) -> dict:
    """What CLS now says about one created creative, against the plan item it was made from."""
    detail = detail if isinstance(detail, dict) else {}
    problems: list[str] = []
    if detail.get("name") != name:
        problems.append("名称")
    if detail.get("upload_user_id") != account:
        problems.append("上传人")
    if detail.get("user_id") != item["creator"]["id"]:
        problems.append("创意人")
    if not isinstance(detail.get("user_ids"), list) or not _same_ids(detail["user_ids"], [p["id"] for p in item["producers"]]):
        problems.append("制作人")
    types = (detail.get("params") or {}).get("types") if isinstance(detail.get("params"), dict) else None
    if not isinstance(types, list) or len(types) != 1 or types[0] != item["type"]["id"]:
        problems.append("素材类型")
    if detail.get("adsdesk_dir_id") != item["directory"]["id"]:
        problems.append("上传目录")
    material = detail["materials"][0] if isinstance(detail.get("materials"), list) and detail["materials"] else None
    if isinstance(material, dict) and material.get("file_hash") and material["file_hash"] != item["md5"]:
        problems.append("文件内容")
    return {"ok": not problems, "problems": problems, "annotated": not any(p in problems for p in ("创意人", "制作人", "素材类型")), "processing": material is None}


def _find_created(api: CreativeAdapter, game_id: int, account: int, wanted: list[tuple[str, dict]]) -> dict[str, int]:
    """
    What a create of unknown outcome made: creatives of the exact name, uploaded by this account (confirmed or 待确认), whose material is the
    approved file. CLS takes a file (by its MD5) only once, so that one is this batch's; another of the same name (made by hand on the page
    meanwhile, say) is not, and is never annotated. A material CLS has not processed yet shows no hash: it is looked at again later.
    """
    found: dict[str, int] = {}
    for name, item in wanted:
        for confirmed in (True, False):
            hits = [c for c in _same_name(api, game_id, name, confirmed) if c.get("upload_user_id") == account]
            ours = []
            for hit in hits:
                detail = api.creative(hit["id"])
                materials = detail.get("materials") if isinstance(detail, dict) else None
                material = materials[0] if isinstance(materials, list) and materials else None
                if isinstance(material, dict) and material.get("file_hash") == item["md5"]:
                    ours.append(hit["id"])
            if len(ours) == 1:
                found[name] = ours[0]
    return found


def _last_data(events: list[dict], step: str) -> Optional[dict]:
    return next((e["data"] for e in reversed(events) if e["step"] == step), None)


CHUNK_FILES = 20
CHUNK_BYTES = 2 * 1024 ** 3


def _chunks(items: list[dict]) -> list[list[dict]]:
    """Groups of files signed and put together, so that each group's links are fresh when its files go up."""
    out: list[list[dict]] = []
    current: list[dict] = []
    size = 0
    for item in items:
        if current and (len(current) >= CHUNK_FILES or size + item["size"] > CHUNK_BYTES):
            out.append(current)
            current = []
            size = 0
        current.append(item)
        size += item["size"]
    if current:
        out.append(current)
    return out


def run_upload(online: Any, plan_file: str, deps: Optional[UploadDeps] = None) -> dict:
    """
    Runs an approved plan to the end, or goes on with it: file check, sign, put, create (once), annotate, read back. The result says what
    exists in CLS now; `status` is `uploaded` when every planned file is a creative with its annotation, `partial` when some are not (with
    the reason), `unknown` when a create's outcome could not be found out yet, `blocked` when nothing more can be done without the user.
    """
    from .cls_session import check_token

    deps = deps or UploadDeps()
    plan = load_upload_plan(plan_file)
    directory = _open_batch(plan, deps)

    def record(step: str, data: Optional[dict] = None) -> dict:
        return append_upload_event(directory, step, data or {}, deps.now)

    sleep = deps.sleep or (lambda ms: time.sleep(ms / 1000))

    def same_account(s: dict) -> None:
        need(s["account"]["id"] == plan["account"]["id"], f"当前登录的 CLS 账号“{s['account']['name']}”不是计划里的“{plan['account']['name']}”")

    session = online.session()
    same_account(session)
    create_marker = os.path.join(directory, "create-sent.json")

    # The create goes out once per batch, whatever happens to this process: the marker is made (exclusively) right before it is sent. An
    # annotation only sets values, so its record in the journal is all it needs.
    def before_write(api_path: str) -> None:
        if "multiple-upload" not in api_path:
            return
        try:
            stamp = js.iso(deps.now().timestamp() * 1000) if deps.now else js.iso()
            write_json_new(create_marker, {"plan_id": plan["plan_id"], "at": stamp})
        except Exception:
            raise ClsError("这个批次已经发出过创建请求；先回读核对，不重发", "unknown") from None

    api = _adapter_for(session, deps, before_write=before_write)
    game_id = plan["game"]["id"]
    items = plan["items"]

    if not os.path.exists(create_marker):
        # Nothing has been created yet: everything up to the create can be done (again) from the start.
        for item in items:
            need(os.path.isfile(item["file"]) and os.path.getsize(item["file"]) == item["size"], f"文件在批准后不见了或变了：{item['file']}")
            need(hash_file(item["file"])["sha256"] == item["sha256"], f"文件在批准后被改过：{item['file']}")
        _still_offered(plan, read_options(api, session))
        clash = existing_names(api, game_id, [i["name"] for i in items])
        need(not clash, f"CLS 现在已有同名创意：{'、'.join(sorted(clash, key=[i['name'] for i in items].index))}；请改名后重新做计划")
        mapped: dict[str, str] = {}
        finished: list[str] = []
        for group in _chunks(items):
            # The page signs "<creative name>/<file name>"; a file the plan renames goes up under its new name.
            keys = [f"{i['name']}/{i.get('upload_name') or i['file_name']}" for i in group]
            signed = api.sign(game_id, keys)
            by_key = {f["key"]: f for f in signed["files"]}
            missing = [k for k in keys if k not in by_key]
            need(not missing, f"CLS 没有给这些文件上传链接（格式不受支持）：{'、'.join(missing)}")
            # Lists, not objects keyed by name: a material name is never taken for a credential's field (see seal.py).
            record("signed", {"keys": keys, "objects": [f["object"] for f in signed["files"]],
                              "mapping": [{"key": key, "to": to} for key, to in signed["mapping"].items()]})
            api.log(f"{session['account']['name']}获取预签名链接: {js.stringify(keys)}（requirement-auto）")
            for item in group:
                key = f"{item['name']}/{item.get('upload_name') or item['file_name']}"
                link = by_key[key]
                receipt: Optional[dict] = None
                attempt = 1
                while receipt is None:
                    # A link about to run out (or one that failed) is asked for again: nothing is made of a link until the create.
                    if attempt > 1 or (link.get("expires_at") is not None and link["expires_at"] * 1000 - js.now_ms() < 5 * 60_000):
                        again = api.sign(game_id, [key])
                        fresh = next((f for f in again["files"] if f["key"] == key), None)
                        need(fresh, f"CLS 没有再给 {item['file_name']} 上传链接")
                        link = fresh
                        signed["mapping"][key] = again["mapping"].get(key)
                        record("signed", {"keys": [key], "objects": [fresh["object"]], "mapping": [{"key": key, "to": again["mapping"].get(key, js.UNDEFINED)}]})
                    try:
                        receipt = api.put(key, item["file"], item["size"])
                    except Exception:
                        if attempt >= 3:
                            raise
                        sleep(2_000 * attempt)
                    attempt += 1
                record("uploaded", {"key": key, "object": link["object"], **receipt})
                finished.append(key)
                mapped[item["name"]] = signed["mapping"][key].split("/")[0]
        api.log(f"成功上传以下文件至游戏: {plan['game']['name']}; 文件列表: {js.stringify(finished)}（requirement-auto）")
        # The uploads may have taken long: the create goes out with a login CLS still takes (taken again from the browser if it has run out),
        # and not at all while CLS cannot be asked, since nothing has been made yet and the next run starts over.
        still = check_token(session["origin"], session["authorization"], deps.adapter.get("fetch"))
        if "unreachable" in still:
            raise Blocked("CLS 暂时无法访问，还没有创建任何创意：稍后再运行本命令（文件会重新上传，不会重复创建）")
        if "rejected" in still:
            session = online.session()
            same_account(session)
            api = _adapter_for(session, deps, before_write=before_write)
        names = [mapped[i["name"]] for i in items]
        directories = {mapped[i["name"]]: i["directory"]["id"] for i in items}
        record("create_attempt", {"names": names, "directories": [{"name": mapped[i["name"]], "id": i["directory"]["id"]} for i in items],
                                  "renamed": [{"from": i["name"], "to": mapped[i["name"]]} for i in items if mapped[i["name"]] != i["name"]]})
        try:
            created = api.create_creatives(game_id, names, directories)
            record("created", created)
        except ClsError as error:
            record("create_unknown" if error.kind == "unknown" else "create_refused", {"reason": error.message})

    # From here on the create has been sent once and is never sent again.
    events = upload_events(directory)
    attempt_data = _last_data(events, "create_attempt")
    if not attempt_data:
        # The attempt is recorded before the marker is made, so a marker without one means the batch record is damaged: what went out is not known.
        return {"status": "unknown", "batch": directory, "reason": "批次记录不完整，创建请求是否发出无法确定；先在 CLS 创意列表里核对这些文件是否已有创意，不重发",
                "list_url": LIST_URL}
    names = attempt_data["names"]
    item_of = {names[i]: item for i, item in enumerate(items) if i < len(names)}
    refused = _last_data(events, "create_refused")
    ids: dict[str, int] = {}
    failed: list[str] = []
    created = _last_data(events, "created")
    if created:
        for s in created["succ"]:
            ids[s["name"]] = s["id"]
        failed = list(created["fail"])
        # A name the answer left out of both lists may have been made all the same: it is looked for, on this run and on every later one.
        unaccounted = [n for n in names if n not in ids and n not in failed]
        if unaccounted:
            for name, ident in _find_created(api, game_id, plan["account"]["id"], [(n, item_of[n]) for n in unaccounted]).items():
                ids[name] = ident
            record("reconciled", {"found": [{"name": n, "id": ids[n]} for n in unaccounted if n in ids], "missing": [n for n in unaccounted if n not in ids]})
    else:
        # A create whose answer was lost: look for what it made, for a while (the server may still be working on it). A refused one is looked
        # at once, since a refusal does not prove that nothing was made.
        tries = 1 if refused else (deps.reconcile_tries if deps.reconcile_tries is not None else 4)
        for i in range(tries):
            ids = _find_created(api, game_id, plan["account"]["id"], [(n, item_of[n]) for n in names])
            if len(ids) == len(names):
                break
            if i < tries - 1:
                sleep(15_000)
        record("reconciled", {"found": [{"name": n, "id": i} for n, i in ids.items()], "missing": [n for n in names if n not in ids]})
        events = upload_events(directory)
        if refused and not ids:
            return {"status": "blocked", "batch": directory, "reason": f"CLS 没有创建创意：{refused['reason']}", "uploaded": []}
        if refused:
            failed = [n for n in names if n not in ids]

    # Annotation (创意人, 制作人, 素材类型) for the creatives that do not show it yet. It only sets values, so it is sent again only when a
    # read-back shows it missing, at most three times per batch; a refusal stops it.
    def read_back() -> dict[str, dict]:
        return {name: _check(api.creative(ident), item_of[name], name, plan["account"]["id"]) for name, ident in ids.items()}

    report = read_back()
    annotate_error: Optional[str] = None
    while True:
        pending = [(name, ident) for name, ident in ids.items() if not report[name]["annotated"]]
        if not pending:
            break
        round_ = len([e for e in upload_events(directory) if e["step"] == "annotate_attempt"]) + 1
        if round_ > 3:
            annotate_error = "标注已发送 3 次仍未生效"
            break
        record("annotate_attempt", {"round": round_, "ids": [ident for _, ident in pending]})
        try:
            api.annotate(game_id, [{"id": ident, "sponsor": item_of[name]["creator"]["id"], "producers": [p["id"] for p in item_of[name]["producers"]],
                                    "types": [item_of[name]["type"]["id"]]} for name, ident in pending])
            record("annotated", {"round": round_, "ids": [ident for _, ident in pending]})
        except ClsError as error:
            record("annotate_failed", {"round": round_, "reason": error.message, "unknown": error.kind == "unknown"})
            if error.kind != "unknown":
                annotate_error = error.message
        report = read_back()
        if annotate_error:
            break
        if round_ < 3 and any(not report[name]["annotated"] for name in ids):
            sleep(2_000)

    # The AdsDesk sync the directory triggers, as the list shows it.
    sync: dict[Any, dict] = {}
    try:
        mine = api.mine(game_id, plan["account"]["id"], min(100, max(20, len(ids) + 10)))
        for c in (mine or {}).get("creatives") or []:
            sync[c.get("id")] = ((c.get("sync_status") or {}).get("adsdesk") or {})
    except Exception:
        pass  # the sync state is extra information

    def adsdesk(ident: int) -> str:
        s = sync.get(ident)
        if not s:
            return "未知"
        if js.number(s.get("fail", js.UNDEFINED)) > 0:
            return "同步失败"
        return "已同步" if s.get("status") == 1 and js.number(s.get("succ", js.UNDEFINED)) >= js.number(s.get("total", js.UNDEFINED)) else "同步中"

    uploaded = []
    for name, ident in ids.items():
        item = item_of[name]
        state = report[name]
        entry: dict[str, Any] = {"name": name, "id": ident, "file": item["file_name"], "type": item["type"]["name"], "creator": item["creator"]["name"],
                                 "producers": [p["name"] for p in item["producers"]], "verified": state["ok"]}
        if not state["ok"]:
            entry["problems"] = state["problems"]
        if state["processing"]:
            entry["processing"] = True
        entry["adsdesk"] = adsdesk(ident)
        uploaded.append(entry)
    not_created = [{"name": n, "file": (item_of.get(n) or {}).get("file_name", js.UNDEFINED),
                    "reason": f"CLS 拒绝创建：{refused['reason']}" if refused else "CLS 已有内容相同的素材（MD5 重复），没有创建"} for n in failed]
    missing = [{"name": n, "file": (item_of.get(n) or {}).get("file_name", js.UNDEFINED)} for n in names if n not in ids and n not in failed]
    verified_data: dict[str, Any] = {"uploaded": [{"id": u["id"], "name": u["name"], "verified": u["verified"], **({"problems": u["problems"]} if "problems" in u else {})}
                                                  for u in uploaded], "not_created": failed, "missing": [m["name"] for m in missing]}
    record("verified", verified_data)
    all_good = len(uploaded) == len(items) and all(u["verified"] for u in uploaded)
    result: dict[str, Any] = {"status": "uploaded" if all_good else "unknown" if missing else "partial", "batch": directory, "list_url": LIST_URL, "uploaded": uploaded}
    if annotate_error:
        result["annotate_error"] = annotate_error
    if not_created:
        result["not_created"] = not_created
    if missing:
        result["missing"] = missing
        result["reason"] = "创建请求的结果没能确认：这些文件在 CLS 里还没找到。稍后再运行一次本命令核对，不会重新创建"
    if attempt_data.get("renamed"):
        result["renamed"] = attempt_data["renamed"]
    if plan["skipped"]:
        result["skipped"] = [{"file": shown_file(s["file"]), "reason": s["reason"]} for s in plan["skipped"]]
    return result


def list_picture(online: Any, output: str, rows: Optional[int], deps: Optional[UploadDeps] = None) -> dict:
    """
    The picture of 我的创意 > 创意列表 (this account's uploads, newest first). The session is taken first, so a login that needs the consent
    page pressed is put right before the browser is opened for the picture.
    """
    from .cls_session import cls_profile_dir
    from .creative_shot import list_screenshot
    from .shot_page import not_loaded

    deps = deps or UploadDeps()
    session = online.session()
    games = _adapter_for(session, deps).games()
    game = next((g for g in (games if isinstance(games, list) else []) if g.get("code") == "GOFCN"), None)
    need(game, f"CLS 账号“{session['account']['name']}”没有 GOFCN 项目")
    options: dict[str, Any] = {"output": output, "account_id": session["account"]["id"], "game": game}
    if rows is not None:
        options["rows"] = rows
    options.update(deps.shot)
    if not options.get("profile_dir"):
        options["profile_dir"] = cls_profile_dir()
    result = list_screenshot(**options)
    if result["state"] == "unreachable":
        raise Blocked(not_loaded("创意列表", result["reason"]))
    if result["state"] == "login_required":
        raise Blocked("CLS 登录已失效（页面停在登录页）：先运行 gof login check 再截图")
    if result["state"] == "no_access":
        raise Blocked(f"创意列表打不开（停在 {result['where']}）：账号可能没有这个页面的权限")
    return {"status": "screenshot_saved", "file": result["file"], "rows": result["rows"], "total": result["total"], "list_url": LIST_URL}


def upload_status(plan_file: str, deps: Optional[UploadDeps] = None) -> dict:
    """What a batch has recorded so far, from its own files only."""
    deps = deps or UploadDeps()
    plan = load_upload_plan(plan_file)
    directory = _batch_of(plan, deps)
    if not os.path.exists(os.path.join(directory, "plan.json")):
        return {"status": "not_started", "plan": os.path.abspath(plan_file), "files": len(plan["items"])}
    events = upload_events(directory)
    return {"status": "recorded", "batch": directory, "files": len(plan["items"]), "steps": [e["step"] for e in events],
            "create_sent": os.path.exists(os.path.join(directory, "create-sent.json")), "last": events[-1] if events else None}
