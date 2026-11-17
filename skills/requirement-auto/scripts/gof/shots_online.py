"""The two pictures that close the requirement flow.

`gof feishu screenshot` (the 创意进度表 on the rows that were appended) and `gof cls screenshot` (a group's CLS 需求草稿). Both are worked
out from the batch's own record, both only look at a page in this user's hidden browser (see sheet_shot.py and draft_shot.py), and neither
can change anything in Feishu or CLS.
"""

from __future__ import annotations

import re
from typing import Any, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from . import js
from .util import Blocked, NODE, check_target, feishu_doc_path, need


def _with_profile(options: dict) -> dict:
    """The picture's options with this user's browser profile, unless a test names another (asking the Feishu module only when needed)."""
    from .cls_session import cls_profile_dir

    return options if options.get("profile_dir") else {**options, "profile_dir": cls_profile_dir()}


def sheet_picture(online: Any, batch: str, output: str, deps: Optional[dict] = None) -> dict:
    """The progress sheet after the append: the tab the rows were put into, opened on those rows. `deps` (tests): {"sheet": {...}}."""
    from .batch import report, sheet_url
    from .cls_session import cls_profile_dir
    from .sheet_shot import sheet_screenshot
    from .shot_page import not_loaded

    deps = deps or {}
    result = report(batch)
    need(result.get("append_verified"), "this batch has not appended and verified rows yet: there is nothing to screenshot")
    url = sheet_url(result.get("sheet_id"))
    check_target(url)
    # The browser profile is the CLS one: it must be this user's own, and the Feishu login that fills it is checked with the CLS one.
    online.session()
    shot = sheet_screenshot(**_with_profile({"output": output, "url": url, "range_": result.get("range"), **(deps.get("sheet") or {})}))
    if shot["state"] == "unreachable":
        raise Blocked(not_loaded("progress sheet", shot["reason"]))
    if shot["state"] == "login_required":
        raise Blocked(f"the Feishu web login has lapsed (the page stopped at {shot['where']}): run gof login check, then take the screenshot again")
    if shot["state"] == "no_access":
        raise Blocked(f"the progress sheet did not open (stopped at {shot['where']}): the account may have no access to it")
    return {"status": "screenshot_saved", "file": shot["file"], "worksheet": result["worksheet"], "range": result["range"], "progress_sheet": url}


def draft_of(batch: str, group: Optional[str]) -> dict:
    """The draft a group of the batch has in CLS (the one its scripts were imported into, else the one that was saved)."""
    from .batch import report

    groups = [g for g in report(batch)["groups"] if group is None or g["group"] == group]
    need(len(groups) > 0, "this batch has no groups" if group is None else f"this batch has no group {group}")
    with_draft = []
    for g in groups:
        step = g.get("scripts_imported") or g.get("draft_saved") or {}
        if js.is_safe_int(step.get("draft_id")):
            with_draft.append({"group": js.to_str(g["group"]), **step})
    if not with_draft:
        raise Blocked("this batch has no CLS draft saved yet" if group is None else f"group {group} has no CLS draft saved yet")
    need(len(with_draft) == 1, f"this batch has {len(with_draft)} CLS drafts; name one with --group: {', '.join(g['group'] for g in with_draft)}")
    only = with_draft[0]
    return {"group": only["group"], "id": only["draft_id"], "url": only.get("url")}


def draft_picture(online: Any, batch: str, group: Optional[str], output: str, deps: Optional[dict] = None) -> dict:
    """A group's CLS draft, opened in the app as a person would and photographed. `deps` (tests): {"adapter": {...}, "draft": {...}}."""
    from .cls_session import cls_profile_dir
    from .creative_adapter import CreativeAdapter
    from .draft_shot import draft_screenshot
    from .shot_page import not_loaded

    deps = deps or {}
    draft = draft_of(batch, group)
    session = online.session()
    games = CreativeAdapter(session["origin"], session["authorization"], **(deps.get("adapter") or {})).games()
    game = next((g for g in (games if isinstance(games, list) else []) if g.get("code") == "GOFCN"), None)
    need(game, f"the CLS account \"{session['account']['name']}\" has no GOFCN project")
    shot = draft_screenshot(**_with_profile({"output": output, "draft_id": draft["id"], "game": game, **(deps.get("draft") or {})}))
    if shot["state"] == "unreachable":
        raise Blocked(not_loaded("CLS draft page", shot["reason"]))
    if shot["state"] == "login_required":
        raise Blocked("the CLS login has lapsed (the page stopped on the sign-in page): run gof login check, then take the screenshot again")
    if shot["state"] == "no_access":
        raise Blocked(f"the draft page did not open (stopped at {shot['where']}): the account may have no access to it")
    return {"status": "screenshot_saved", "file": shot["file"], "group": draft["group"], "draft_id": draft["id"], "draft_url": draft["url"]}


def doc_picture(online: Any, url: str, focus: dict, output: str, deps: Optional[dict] = None) -> dict:
    """
    A requirement document after an edit (`gof feishu doc-screenshot`): a docx opened on the block the edit wrote (its `focus.block`), a sheet
    on its tab and the cells it filled (`focus.sheet_id`, `focus.range`). Only looks, as the user, in this user's hidden browser.
    """
    from .cls_session import cls_profile_dir
    from .doc_shot import doc_screenshot
    from .sheet_shot import sheet_screenshot
    from .shot_page import not_loaded

    deps = deps or {}
    doc_path = feishu_doc_path(url, "docx|wiki|sheets", True)
    need(doc_path, "not a company Feishu document link (a wiki, docx or sheets link is needed)")
    need(NODE not in doc_path, "the progress sheet is screenshotted with gof feishu screenshot --batch")
    need(focus.get("block") is None or re.fullmatch(r"[A-Za-z0-9]{8,64}", focus["block"]), "--block takes the block ID of focus.block in the doc-edit result")
    need(focus.get("sheet_id") is None or re.fullmatch(r"[A-Za-z0-9_-]{1,64}", focus["sheet_id"]), "--sheet takes focus.sheet_id of the doc-edit result")
    need(focus.get("range") is None or re.fullmatch(r"[A-Z]{1,3}[0-9]+(:[A-Z]{1,3}[0-9]+)?", focus["range"]), "--range is written like A9 or A9:B12")
    online.session()
    sheet = focus.get("sheet_id") is not None or doc_path.startswith("/sheets/")
    if sheet:
        parts = urlsplit(url)
        query = parse_qsl(parts.query, keep_blank_values=True)
        if focus.get("sheet_id"):
            query = [(k, v) for k, v in query if k != "sheet"] + [("sheet", focus["sheet_id"])]
        located = urlunsplit((parts.scheme, parts.netloc, parts.path or "/", urlencode(query), parts.fragment))
        shot = sheet_screenshot(**_with_profile({"output": output, "url": located, "range_": focus.get("range"), **(deps.get("sheet") or {})}))
        if shot["state"] == "unreachable":
            raise Blocked(not_loaded("spreadsheet", shot["reason"]))
        if shot["state"] == "login_required":
            raise Blocked(f"the Feishu web login has lapsed (the page stopped at {shot['where']}): run gof login check, then take the screenshot again")
        if shot["state"] == "no_access":
            raise Blocked(f"the spreadsheet did not open (stopped at {shot['where']}): the account may have no access to it")
        out = {"status": "screenshot_saved", "file": shot["file"], "url": located}
        if focus.get("range"):
            out["range"] = focus["range"]
        return out
    shot = doc_screenshot(**_with_profile({"output": output, "url": url, "block": focus.get("block"), **(deps.get("doc") or {})}))
    if shot["state"] == "unreachable":
        raise Blocked(not_loaded("document", shot["reason"]))
    if shot["state"] == "login_required":
        raise Blocked(f"the Feishu web login has lapsed (the page stopped at {shot['where']}): run gof login check, then take the screenshot again")
    if shot["state"] == "no_access":
        raise Blocked(f"the document did not open (stopped at {shot['where']}): the account may have no access to it")
    out = {"status": "screenshot_saved", "file": shot["file"], "url": url}
    if focus.get("block"):
        out["block"] = focus["block"]
    return out
