"""The Feishu app, the endpoints and the permissions a login asks for."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Mapping

from .errors import BridgeError
from .paths import DataPaths


@dataclass(frozen=True)
class AppCredentials:
    app_id: str
    app_secret: str


@dataclass(frozen=True)
class Endpoints:
    #: Open API host, e.g. https://open.feishu.cn
    open: str
    #: Accounts host: device authorization and the unified OAuth token endpoint
    accounts: str


FEISHU = Endpoints(open="https://open.feishu.cn", accounts="https://accounts.feishu.cn")

#: What a login is asked for, the needs of the agent and of every skill that calls the bridge put together: read wiki nodes, documents and sheets
#: (requirement-auto), export workbooks and append to the progress sheet (requirement-auto). A skill that needs another permission adds
#: it here, so that one login serves them all.
REQUIRED_SCOPES: tuple[str, ...] = (
    "wiki:wiki:readonly", "sheets:spreadsheet:readonly", "sheets:spreadsheet:read", "sheets:spreadsheet:write_only",
    "sheets:spreadsheet", "drive:export:readonly", "docx:document:readonly",
)
#: Editing a Feishu document (writing into a requirement doc). Every login asks for it, but a login made before it was added still does
#: everything else: only the document edit checks for it, and asks the user to authorize once more when it is missing.
DOC_EDIT_SCOPE = "docx:document"
#: Requested on top so the refresh token is issued. It is not a permission and is not part of the checks.
OFFLINE_SCOPE = "offline_access"


def load_app(env: Mapping[str, str], paths: DataPaths | None = None) -> AppCredentials:
    """The Feishu app the logins belong to.

    The host passes FEISHU_APP_ID / FEISHU_APP_SECRET in the environment of the process it starts. Without a host (used on its own) the
    same two values can sit in <data root>/app.json as {"appId", "appSecret"}; in host mode the file is never read, so no secret lands in
    a user's directory.
    """
    app_id = (env.get("FEISHU_APP_ID") or "").strip()
    app_secret = (env.get("FEISHU_APP_SECRET") or "").strip()
    if (not app_id or not app_secret) and paths is not None and paths.mode != "host":
        try:
            with open(os.path.join(paths.root, "app.json"), encoding="utf-8") as handle:
                text = handle.read()
            data = json.loads(text[1:] if text.startswith("﻿") else text)
            if isinstance(data, dict):
                app_id = app_id or _text(data.get("appId"))
                app_secret = app_secret or _text(data.get("appSecret"))
        except (OSError, ValueError):
            pass  # not there, or not readable: reported below
    if not app_id or not app_secret:
        alone = paths is not None and paths.mode != "host"
        if alone:
            assert paths is not None
            where = os.path.join(paths.root, "app.json")
            raise BridgeError("config", f'No Feishu app configured: set FEISHU_APP_ID / FEISHU_APP_SECRET in the environment, or write {{"appId": "...", "appSecret": "..."}} to {where}')
        raise BridgeError("config", "No Feishu app configured: FEISHU_APP_ID and FEISHU_APP_SECRET must be set in the environment")
    return AppCredentials(app_id, app_secret)


def _text(value: object) -> str:
    from .jsutil import js_string

    return "" if value is None else js_string(value).strip()


_SEPARATORS = re.compile(r"[\s,]+")


def scope_list(scope: str | None) -> list[str]:
    """The scopes of a space- or comma-separated list, each once, in order."""
    return list(dict.fromkeys(item for item in _SEPARATORS.split(scope or "") if item))
