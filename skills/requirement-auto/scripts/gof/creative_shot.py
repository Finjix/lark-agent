"""The picture of 我的创意 > 创意列表 the user gets after an upload (creative-shot.ts).

It is taken in this user's own hidden CLS browser (the profile the login lives in), the way a person would see the list: the app is opened,
the list is shown for GOFCN with the uploads of this account, newest first (the list's own saved filter, set in this profile's local
storage), and a screenshot is taken once the rows and their thumbnails are drawn. Nothing is clicked or typed, and nothing in CLS changes.
"""

from __future__ import annotations

import json
import re
from typing import Mapping, Optional

from . import js
from .cls_session import CLS_ORIGIN
from .shot_page import ShotPage, check_picture_target, pause, with_shot_page, write_picture

#: The list's search conditions as the page saves them (DD_CLS_CREATIVES_CONFIRMED_SEARCH_CONDITIONS).
FILTER_KEY = "DD_CLS_CREATIVES_CONFIRMED_SEARCH_CONDITIONS"


def list_screenshot(profile_dir: str, output: str, account_id: int, game: dict, rows: Optional[int] = None, origin: Optional[str] = None,
                    exe: Optional[str] = None, env: Optional[Mapping[str, str]] = None, wait_ms: Optional[int] = None) -> dict:
    """{"state": "ok", "file", "rows", "total"} | login_required | no_access | {"state": "unreachable", "where", "reason"}."""
    check_picture_target(output)
    count = max(1, min(rows if rows is not None else 8, 12))

    def run(page: ShotPage) -> dict:
        def unreachable() -> Optional[dict]:
            broken = page.load_error()
            return {"state": "unreachable", "where": page.where(), "reason": broken} if broken else None

        if not page.open_app_for_game(game):
            return unreachable() or {"state": "login_required", "where": page.where()}
        filter_ = {"page": 1, "page_size": 50, "upload_users": [account_id], "tags": [], "custom_tags": [], "sort_field": "updated_at",
                   "sort_order": "desc", "game_id": game.get("id")}
        value = json.dumps(json.dumps(filter_, ensure_ascii=False, separators=(",", ":")), ensure_ascii=False)
        page.evaluate(f"localStorage.setItem({json.dumps(FILTER_KEY)}, {value}); true")
        listed = page.count("/api/creatives")
        # In-app navigation, as the menu item does: a fresh page load of the list can be turned away before the app knows its pages.
        routed = page.evaluate("(async () => { const vm = window.DD_VUE_INSTANCE; if (!vm || !vm.$router) return false; await vm.$router.push({ name: 'CREATIVITY-LIST' }); return true; })()")
        if not routed:
            page.navigate(f"{page.origin}/creativities")
        page.until(lambda: page.count("/api/creatives") > listed, page.wait)
        here = page.where()
        if not here.endswith("/creativities"):
            return unreachable() or {"state": "login_required" if re.search("feishu|larksuite", here) else "no_access", "where": here}
        # The rows (or the empty-list text), then their thumbnails.
        page.until(lambda: page.evaluate("document.querySelectorAll('.el-table__body-wrapper tbody tr').length > 0 || !!document.querySelector('.el-table__empty-text')") is True, 15_000)
        page.images_drawn(".el-table__body-wrapper img", 10_000)
        pause(800)
        state = page.evaluate("({ rows: document.querySelectorAll('.el-table__body-wrapper tbody tr').length, total: (document.querySelector('.el-pagination__total') || {}).textContent || null })")
        state = state if isinstance(state, dict) else {}
        shown = js.num(state.get("rows") if state.get("rows") is not None else 0)
        return {"state": "ok", "file": write_picture(output, page.capture()), "rows": shown, "total": state.get("total")}

    # Wide enough for every column of the list (创意人, 制作人, 上传人, 同步AdsDesk), tall enough for the rows; the table takes its height from the window.
    return with_shot_page(profile_dir, origin or CLS_ORIGIN, 2000, max(1000, 420 + 160 * count), run, exe=exe, env=env, wait_ms=wait_ms)
