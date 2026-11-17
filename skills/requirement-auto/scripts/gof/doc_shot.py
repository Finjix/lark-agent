"""The picture of a requirement document after the agent edited it.

`gof feishu doc-screenshot` for a docx (a sheet goes through sheet_shot.py): the document opened in this user's hidden browser on the block
that was written, photographed once it has drawn. Feishu scrolls a docx to a block when its link ends in #<block ID>. Nothing is clicked or
typed.
"""

from __future__ import annotations

from typing import Mapping, Optional
from urllib.parse import urlsplit, urlunsplit

from . import js
from .cls_session import CLS_ORIGIN
from .sheet_shot import sign_in
from .shot_page import ShotPage, check_picture_target, pause, with_shot_page, write_picture

WIDTH = 1600
HEIGHT = 1100


def doc_screenshot(profile_dir: str, output: str, url: str, block: Optional[str] = None, exe: Optional[str] = None,
                   env: Optional[Mapping[str, str]] = None, wait_ms: Optional[int] = None) -> dict:
    """{"state": "ok", "file", "where"} | login_required | no_access | {"state": "unreachable", "where", "reason"}."""
    check_picture_target(output)
    parts = urlsplit(url)
    home = (parts.hostname or "") + (f":{parts.port}" if parts.port else "")
    target = urlunsplit((parts.scheme, parts.netloc, parts.path or "/", parts.query, block)) if block else url

    def run(page: ShotPage) -> dict:
        page.navigate(target)

        # On the document's host with its blocks drawn, or sent to sign in.
        def ready() -> bool:
            here = page.where()
            if not here.startswith(home) or sign_in(here):
                return here != "" and here != "about:blank"
            return page.evaluate("document.readyState === 'complete' && document.querySelectorAll('[data-block-id]').length > 1") is True

        page.until(ready, page.wait)
        here = page.where()
        broken = page.load_error()
        if broken:
            return {"state": "unreachable", "where": here, "reason": broken}
        if not here.startswith(home) or sign_in(here):
            return {"state": "login_required", "where": here}
        if js.number(page.evaluate("document.querySelectorAll('[data-block-id]').length")) <= 1:
            return {"state": "no_access", "where": here}
        # Images and the scroll to the block come after the text.
        page.quiet(2_000, 20_000)
        pause(1_500)
        return {"state": "ok", "file": write_picture(output, page.capture()), "where": here}

    return with_shot_page(profile_dir, CLS_ORIGIN, WIDTH, HEIGHT, run, exe=exe, env=env, wait_ms=wait_ms)
