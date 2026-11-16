"""The bridge other skills reach Feishu through (bridge.ts).

`feishu.py bridge <command>` (one command, JSON on stdin) and `feishu.py serve` (one command per line) run it. The user this serves comes
from the environment the host started the process with; the input has no way to name another one. Never returns a token.
"""

from __future__ import annotations

import os
import platform
from dataclasses import dataclass
from typing import Any, Mapping

from .api import FeishuApi
from .auth import FeishuAuth
from .config import REQUIRED_SCOPES, AppCredentials, Endpoints, load_app
from .errors import BridgeError, to_payload
from .jsutil import is_number, js_number, js_string
from .paths import expected_open_id, resolve_data_paths

BRIDGE_VERSION = "feishu-sdk-bridge/1"
COMMANDS: tuple[str, ...] = (
    "info", "auth-status", "auth-verify", "auth-login", "auth-poll", "auth-logout", "auth-check-scopes",
    "api-get", "sheets-revision", "sheets-export", "sheets-cells-set", "sheets-create", "sheets-dropdown-add",
    "docx-blocks", "docx-insert", "docx-set-texts",
)
#: Commands that read their arguments from stdin; the rest take none.
INPUT_COMMANDS: frozenset[str] = frozenset({
    "auth-login", "auth-poll", "auth-check-scopes", "api-get", "sheets-revision", "sheets-export", "sheets-cells-set", "sheets-create",
    "sheets-dropdown-add", "docx-blocks", "docx-insert", "docx-set-texts",
})
#: Commands that only touch local files, and so work without the app configured.
_LOCAL = {"auth-status", "auth-logout", "info"}

BridgeReply = dict[str, Any]


@dataclass
class BridgeContext:
    env: Mapping[str, str] | None = None
    endpoints: Endpoints | None = None


def _object(value: Any, command: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise BridgeError("invalid_request", f"{command} 需要 JSON 对象参数")
    return value


def _strings(value: Any, name: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) and len(item) < 200 for item in value):
        raise BridgeError("invalid_request", f"{name} 必须是字符串数组")
    return value


def _text(args: dict[str, Any], key: str) -> str:
    """`String(args[key] ?? '')`."""
    value = args.get(key)
    return "" if value is None else js_string(value)


def run_bridge(command: str, value: Any = None, context: BridgeContext | None = None) -> BridgeReply:
    context = context or BridgeContext()
    env = context.env if context.env is not None else os.environ
    auth: FeishuAuth | None = None
    try:
        paths = resolve_data_paths(env)
        app: AppCredentials | None = None
        missing_app: str | None = None
        try:
            app = load_app(env, paths)
        except BridgeError as error:
            if command not in _LOCAL:
                raise
            missing_app = error.message
        auth = FeishuAuth(paths=paths, app=app, missing_app=missing_app, expected_open_id=expected_open_id(env), endpoints=context.endpoints)
        api = FeishuApi(auth)
        args = _object(value, command)
        data: Any
        if command == "info":
            # Where this user's data is, for the skills that call this one: the hidden browser with the Feishu web session is shared with them.
            data = {"bridge": BRIDGE_VERSION, "python": platform.python_version(), "storage": paths.mode, "app_configured": bool(app),
                    "required_scopes": list(REQUIRED_SCOPES), "root": paths.root, "browser_profile": paths.browser_profile}
        elif command == "auth-status":
            data = auth.status()
        elif command == "auth-verify":
            user = api.verify_user()
            expected = expected_open_id(env)
            if expected and user["open_id"] != expected:
                raise BridgeError("identity_mismatch", "当前保存的飞书登录不是当前用户本人的账号，需要用本人账号重新授权")
            status = auth.status()
            data = {"logged_in": True, "open_id": user["open_id"], **({"name": user["name"]} if user.get("name") else {}),
                    "missing_scopes": status.get("missing_scopes") or [], "refresh_expires_at": status.get("refresh_expires_at")}
            data = {key: item for key, item in data.items() if item is not None}
        elif command == "auth-login":
            data = auth.start_login(_strings(args.get("extra_scopes"), "extra_scopes"))
        elif command == "auth-poll":
            wait = args.get("wait_ms")
            data = auth.poll_login(float(wait)) if is_number(wait) else auth.poll_login()
        elif command == "auth-logout":
            auth.logout()
            data = {"logged_in": False}
        elif command == "auth-check-scopes":
            required = _strings(args.get("required"), "required")
            status = auth.status(required)
            data = {"complete": bool(status["logged_in"] and not status.get("missing_scopes")), "granted": status.get("scopes") or [],
                    "missing": (status.get("missing_scopes") or []) if status["logged_in"] else required}
        elif command == "api-get":
            data = api.get(_text(args, "path"), _object(args.get("params"), "params"))
        elif command == "sheets-revision":
            data = {"revision": api.revision(_text(args, "token"))}
        elif command == "sheets-export":
            data = api.export_xlsx(_text(args, "token"), _text(args, "output"))
        elif command == "sheets-cells-set":
            data = api.cells_set(_text(args, "token"), args.get("writes"))
        elif command == "sheets-create":
            data = api.create_workbook(_text(args, "title"))
        elif command == "sheets-dropdown-add":
            data = api.add_dropdown_option(_text(args, "token"), _text(args, "sheet_id"), _text(args, "column"), js_number(args.get("first_row", _MISSING)),
                                           js_number(args.get("last_row", _MISSING)), _text(args, "option"), _text(args, "color"))
        elif command == "docx-blocks":
            data = api.docx_blocks(_text(args, "document"))
        elif command == "docx-insert":
            data = api.docx_insert(_text(args, "document"), _text(args, "parent"), js_number(args.get("index", _MISSING)),
                                   _strings(args.get("children_id"), "children_id"), args.get("descendants"), _text(args, "client_token"))
        elif command == "docx-set-texts":
            data = api.docx_set_texts(_text(args, "document"), args.get("updates"), _text(args, "client_token"))
        else:
            raise BridgeError("invalid_request", f"未知命令：{command}")
        return {"ok": True, "data": data}
    except Exception as error:  # noqa: BLE001 - every failure becomes a reply, scrubbed of credentials
        return {"ok": False, "error": to_payload(error, auth.safe_secrets() if auth else [])}


class _Missing:
    """An argument that was not given at all: `Number(undefined)` is NaN, where `Number(null)` is 0."""


_MISSING = _Missing()
