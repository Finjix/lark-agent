"""Pressing 【授权】 on Feishu's consent page, the one thing ever done to a page here (consent.ts).

The page is only reached through the verification link of this user's own pending device authorization, after the user scanned the sign-in
code with their own phone for exactly this login; who ends up logged in is still decided by the login itself (it has to be the user this
process serves).
"""

from __future__ import annotations

from typing import Any, Callable
from urllib.parse import SplitResult

from .browser import Cdp, CdpError
from .jsutil import parse_url

# Runs in the consent page: where its "授权" button is, if it is drawn and nothing covers it.
CONSENT_PROBE = r"""(() => {
  const norm = s => (s || '').replace(/\s+/g, '');
  const candidates = [...document.querySelectorAll('button, [role="button"], a, div, span')].filter(e => e.children.length === 0 && norm(e.textContent) === '授权');
  const element = candidates.map(e => e.closest('button, [role="button"]') || e).find(e => {
    const r = e.getBoundingClientRect();
    return r.width > 0 && r.height > 0 && !e.disabled && e.getAttribute('aria-disabled') !== 'true';
  });
  let button = null;
  if (element) {
    element.scrollIntoView({ block: 'center' });
    const r = element.getBoundingClientRect();
    const x = r.left + r.width / 2, y = r.top + r.height / 2;
    const hit = document.elementFromPoint(x, y);
    // Only a button that really is under the pointer: a banner or dialog over it would take the click.
    if (hit && (element === hit || element.contains(hit))) button = { x, y };
  }
  return { href: location.href, button };
})()"""


def approve_consent(cdp: Cdp, session_id: str, is_consent_page: Callable[[SplitResult], bool]) -> dict[str, Any]:
    """Presses 【授权】 (never 【拒绝】) when the page is a consent page and the button is there: {"state": "clicked" | "not_ready"}."""
    probe = cdp.send("Runtime.evaluate", {"expression": CONSENT_PROBE, "returnByValue": True}, session_id)
    page = (probe.get("result") or {}).get("value")
    if not isinstance(page, dict) or not page.get("button"):
        return {"state": "not_ready"}  # not a consent page (it also shows the sign-in code), or not drawn yet
    # The page may have moved on since it was looked at: check where it is now.
    here = parse_url(str(page.get("href") or ""))
    if here is None or not is_consent_page(here):
        return {"state": "not_ready"}
    x, y = page["button"].get("x"), page["button"].get("y")
    try:
        cdp.send("Page.bringToFront", {}, session_id)
    except CdpError:
        pass
    cdp.send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y}, session_id)
    cdp.send("Input.dispatchMouseEvent", {"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1}, session_id)
    cdp.send("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1}, session_id)
    return {"state": "clicked"}
