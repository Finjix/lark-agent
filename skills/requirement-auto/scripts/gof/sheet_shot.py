"""The picture of the 创意进度表 the user gets after the requirement flow (sheet-shot.ts).

The progress sheet's tab, opened in this user's hidden browser on the rows that were appended, and photographed once the sheet has drawn.
The browser is the one the Feishu and CLS logins live in (a Feishu scan logs in the web page too), so the sheet opens as the user. Nothing
is clicked or typed, and nothing in the sheet changes: the rows are reached by the link, never by moving around in the sheet.
"""

from __future__ import annotations

import base64
import re
from typing import Mapping, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .cls_session import CLS_ORIGIN
from .shot_page import ShotPage, check_picture_target, pause, with_shot_page, write_picture

WIDTH = 2000
HEIGHT = 1100


def located_url(url: str, range_: Optional[str]) -> str:
    """
    Feishu's own link to a range of cells (复制选区链接) names it in `range`, as the base64 of its A1 form. The link only moves the view onto
    the rows; if Feishu does not understand it the sheet opens at its top, which is why the skill looks at the picture before it hands it over.
    """
    if not range_:
        return url
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != "range"]
    query.append(("range", base64.b64encode(range_.encode("utf-8")).decode("ascii")))
    return urlunsplit((parts.scheme, parts.netloc, parts.path or "/", urlencode(query), parts.fragment))


def sign_in(here: str) -> bool:
    """A page that asks the person to sign in, as Feishu's single sign-on words it."""
    return bool(re.search(r"^(accounts|passport)\.|/accounts/|/login|/passport/", here))


def sheet_screenshot(profile_dir: str, output: str, url: str, range_: Optional[str] = None, exe: Optional[str] = None,
                     env: Optional[Mapping[str, str]] = None, wait_ms: Optional[int] = None) -> dict:
    """{"state": "ok", "file", "where"} | login_required | no_access | {"state": "unreachable", "where", "reason"}."""
    check_picture_target(output)
    parts = urlsplit(url)
    home = (parts.hostname or "") + (f":{parts.port}" if parts.port else "")

    def run(page: ShotPage) -> dict:
        page.navigate(located_url(url, range_))

        # On the sheet's own host with a drawing surface, or sent to sign in.
        def ready() -> bool:
            here = page.where()
            if not here.startswith(home) or sign_in(here):
                return here != "" and here != "about:blank"
            return page.evaluate("document.readyState === 'complete' && document.querySelectorAll('canvas').length > 0") is True

        page.until(ready, page.wait)
        here = page.where()
        broken = page.load_error()
        if broken:
            return {"state": "unreachable", "where": here, "reason": broken}
        if not here.startswith(home) or sign_in(here):
            return {"state": "login_required", "where": here}
        if page.evaluate("document.querySelectorAll('canvas').length") == 0:
            return {"state": "no_access", "where": here}
        # The sheet's cells come in after the page: wait for its data to stop arriving, then for the canvas to be painted.
        page.quiet(2_000, 20_000)
        pause(1_500)
        return {"state": "ok", "file": write_picture(output, page.capture()), "where": here}

    return with_shot_page(profile_dir, CLS_ORIGIN, WIDTH, HEIGHT, run, exe=exe, env=env, wait_ms=wait_ms)
