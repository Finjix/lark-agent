"""The picture of a CLS requirement draft the user gets after the requirement flow (draft-shot.ts).

The draft's own page (需求草稿, with the imported scripts), opened in this user's hidden CLS browser the way a person reaches it, and
photographed once it has been drawn. Like the list picture (creative_shot.py) nothing is clicked or typed and nothing in CLS changes; a
draft is only looked at.
"""

from __future__ import annotations

import json
import re
from typing import Mapping, Optional

from . import js
from .cls_session import CLS_ORIGIN
from .shot_page import ShotPage, check_picture_target, pause, with_shot_page, write_picture

WIDTH = 1700
MIN_HEIGHT = 1000
#: A draft with many scripts is long; the picture stops here (it is a proof that the draft is there, not a copy of it).
MAX_HEIGHT = 2600


def draft_path(draft_id: int) -> str:
    """The draft's page, as `cls.py` records its URL."""
    return f"/requirement/{js.to_str(draft_id)}/edit-outer"


def draft_screenshot(profile_dir: str, output: str, draft_id: int, game: dict, origin: Optional[str] = None, exe: Optional[str] = None,
                     env: Optional[Mapping[str, str]] = None, wait_ms: Optional[int] = None) -> dict:
    """{"state": "ok", "file", "title"} | login_required | no_access | {"state": "unreachable", "where", "reason"}."""
    check_picture_target(output)
    target = draft_path(draft_id)

    def run(page: ShotPage) -> dict:
        def unreachable() -> Optional[dict]:
            broken = page.load_error()
            return {"state": "unreachable", "where": page.where(), "reason": broken} if broken else None

        if not page.open_app_for_game(game):
            return unreachable() or {"state": "login_required", "where": page.where()}
        read = page.count(f"/api/workflow/script/{js.to_str(draft_id)}")
        # In-app navigation, as a click on the draft in the requirement list does: a fresh page load can be turned away before the app knows its pages.
        routed = page.evaluate(f"(async () => {{ const vm = window.DD_VUE_INSTANCE; if (!vm || !vm.$router) return false; await vm.$router.push({json.dumps(target)}); return true; }})()")
        if not routed:
            page.navigate(f"{page.origin}{target}")
        page.until(lambda: page.where().endswith(target) or bool(re.search("feishu|larksuite", page.where())), page.wait)
        here = page.where()
        if not here.endswith(target):
            return unreachable() or {"state": "login_required" if re.search("feishu|larksuite", here) else "no_access", "where": here}
        # The page asks for the draft itself; if it asks otherwise than this tool knows, the loading marks and the text below decide.
        page.until(lambda: page.count(f"/api/workflow/script/{js.to_str(draft_id)}") > read, 8_000)
        # The page is drawn when nothing on it is still loading; then its images, and a moment for the last repaint.
        page.until(lambda: page.evaluate("!document.querySelector('.el-loading-mask') && !!document.body && document.body.innerText.trim().length > 0") is True, 15_000)
        page.images_drawn("img", 10_000)
        pause(800)
        measured = js.number(page.evaluate("Math.max(document.documentElement.scrollHeight, document.body ? document.body.scrollHeight : 0)"))
        height = measured if js.truthy(measured) else MIN_HEIGHT
        page.resize(WIDTH, int(min(MAX_HEIGHT, max(MIN_HEIGHT, height))))
        pause(500)
        title = page.evaluate("document.title || null")
        return {"state": "ok", "file": write_picture(output, page.capture()), "title": title if isinstance(title, str) else None}

    return with_shot_page(profile_dir, origin or CLS_ORIGIN, WIDTH, MIN_HEIGHT, run, exe=exe, env=env, wait_ms=wait_ms)
