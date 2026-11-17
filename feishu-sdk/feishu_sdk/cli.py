"""feishu.py, the command line of lark-agent's Feishu module (cli.ts).

The agent's Feishu tools (src/agent/feishu.ts) run it for the model, and skills run its bridge.

Every command prints one line of JSON on stdout (`serve` one per request) and exits with 0, or with 2 when it was refused or the answer is not
ok ({"status":"blocked","reason":…} for a refusal).

  info | doctor                          what is configured; doctor also needs a login with every permission
  login start [--extra-scope <scope>]…   a device authorization: link and code (the link is for the QR below, not for the chat)
  authorize [--qr <file.png>]            complete it in this user's hidden browser: the sign-in QR for the user, then 【授权】
  login poll [--wait <s>] | status | logout [--browser] | check
  doc fetch --url <link> --output <file.md> [--sheet <tab>]
  bridge <command>                       for other skills: one bridge command, its JSON input on stdin (see bridge.py)
  serve                                  for other skills: one {"id","command","input"} per line on stdin, one {"id","reply"} per line out
"""

from __future__ import annotations

import os
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, BinaryIO, Callable, Mapping

from .api import FeishuApi
from .auth import FeishuAuth
from .bridge import INPUT_COMMANDS, BridgeContext, BridgeReply, run_bridge
from .config import Endpoints, load_app
from .docs import fetch_doc
from .errors import Blocked, need, to_payload
from .jsutil import dumps, is_integer, js_number, js_trim, loads
from .paths import expected_open_id, resolve_data_paths
from .web_login import DeviceWeb, authorize, real_device_web, sign_out_browser

Result = dict[str, Any]
SHOWN = {"auth_required", "config", "identity_mismatch", "scope_missing", "invalid_request", "login_denied", "login_expired"}
NO_ACCESS = {1770032, 131006, 1310213}
LOGIN_TODO = ("Feishu: call the feishu_login tool and send the QR image it makes to the user only, to scan with the Feishu phone app; "
              "once they have confirmed, call feishu_login_finish")
_SCOPE = re.compile(r"[a-z_]+(:[a-z_.]+)+")


@dataclass
class Context:
    """Whom a command serves (the environment the host started this process with), and the test seams: a local Feishu, a browser stand-in."""

    env: Mapping[str, str] = field(default_factory=lambda: os.environ)
    endpoints: Endpoints | None = None
    web: DeviceWeb | None = None
    stdin: BinaryIO | None = None
    stdout: BinaryIO | None = None


# ---- arguments: node:util parseArgs with strict: true, and its messages ----------------------------------------------------------------

def parse(args: list[str], options: Mapping[str, str], multiple: frozenset[str] = frozenset()) -> dict[str, Any]:
    """Long options only (`--name value`, `--name=value`, booleans as `--flag`); no positionals. Refusals are Blocked with Node's wording."""
    values: dict[str, Any] = {}
    i = 0
    rest_positional = False
    while i < len(args):
        arg = args[i]
        if rest_positional or arg == "-" or not arg.startswith("-"):
            raise Blocked(f"Unexpected argument '{arg}'. This command does not take positional arguments")
        if arg == "--":
            rest_positional = True
            i += 1
            continue
        if not arg.startswith("--"):
            raise Blocked(f"Unknown option '-{arg[1]}'")
        name, has_value, inline = arg[2:].partition("=")
        kind = options.get(name)
        if kind is None:
            raise Blocked(f"Unknown option '--{name}'")
        if kind == "boolean":
            if has_value:
                raise Blocked(f"Option '--{name}' does not take an argument")
            value: Any = True
        elif has_value:
            value = inline
        else:
            if i + 1 >= len(args):
                raise Blocked(f"Option '--{name} <value>' argument missing")
            value = args[i + 1]
            if value.startswith("-") and value != "-":
                raise Blocked(f"Option '--{name}' argument is ambiguous.\nDid you forget to specify the option argument for '--{name}'?\n"
                              f"To specify an option argument starting with a dash use '--{name}=-XYZ'.")
            i += 1
        if name in multiple:
            values.setdefault(name, []).append(value)
        else:
            values[name] = value
        i += 1
    return values


# ---- the bridge, as a command sees it --------------------------------------------------------------------------------------------------

def _bridged(ctx: Context) -> BridgeContext:
    return BridgeContext(env=ctx.env, endpoints=ctx.endpoints)


def _answer(reply: BridgeReply) -> Any:
    """The bridge's answer, or a refusal that carries its reason (already scrubbed of credentials by the bridge)."""
    if reply["ok"]:
        return reply["data"]
    error = reply["error"]
    kind, message = error["kind"], error["message"]
    if kind in SHOWN:
        raise Blocked(message)
    code = f"，错误码 {error['code']}" if "code" in error else ""
    raise Blocked(f"飞书接口调用失败（{kind}{code}）：{message}")


def _call(command: str, value: Any, ctx: Context) -> Any:
    return _answer(run_bridge(command, value, _bridged(ctx)))


def check_login(ctx: Context) -> Result:
    """Is the login good right now?

    Unlike `status` this asks Feishu with the stored login (renewing it once if Feishu turns the token down, which also keeps an idle login
    alive), so a login the user withdrew or let lapse is found here and not halfway through a task.
    ok | login_required | identity_mismatch | scopes_missing | unreachable | blocked.
    """
    reply = run_bridge("auth-verify", None, _bridged(ctx))
    if reply["ok"]:
        data = reply["data"] if isinstance(reply["data"], dict) else {}
        missing = data.get("missing_scopes") if isinstance(data.get("missing_scopes"), list) else []
        who = {"name": data["name"]} if data.get("name") else {}
        if missing:
            return {"status": "scopes_missing", **who, "missing_scopes": missing, "reason": "飞书登录缺少必要的权限，需要重新授权", "todo": [LOGIN_TODO]}
        return {"status": "ok", **who}
    kind, message = reply["error"]["kind"], reply["error"]["message"]
    if kind == "auth_required":
        return {"status": "login_required", "reason": message, "todo": [LOGIN_TODO]}
    if kind == "identity_mismatch":
        return {"status": "identity_mismatch", "reason": message}
    if kind == "scope_missing":
        return {"status": "scopes_missing", "reason": message, "todo": [LOGIN_TODO]}
    if kind == "network":
        return {"status": "unreachable", "reason": "暂时连不上飞书，稍后再查；这不说明登录失效"}
    if kind in SHOWN:
        return {"status": "blocked", "reason": message}
    return {"status": "blocked", "reason": "飞书接口调用失败，无法确认登录状态"}


def _doctor(ctx: Context) -> Result:
    info = _call("info", None, ctx)
    need(info.get("app_configured") is True, "未配置飞书应用（FEISHU_APP_ID / FEISHU_APP_SECRET），由宿主或管理员配置")
    auth = _call("auth-status", None, ctx)
    need(auth.get("logged_in") is True, "飞书尚未授权：用 feishu_login 让用户扫码登录")
    missing = auth.get("missing_scopes") or []
    need(not missing, f"飞书登录缺少权限：{'、'.join(missing)}；需重新授权")
    result = {"version": info.get("bridge"), "python": info.get("python"), "logged_in": True, "identity": "user", "user_name": auth.get("name"),
              "open_id": auth.get("open_id"), "storage": auth.get("storage"), "root": info.get("root"), "required_scopes": info.get("required_scopes")}
    return {key: value for key, value in result.items() if value is not None}


def _login(action: str, args: list[str], ctx: Context) -> Result:
    if action == "start":
        extra = parse(args, {"extra-scope": "string"}, frozenset({"extra-scope"})).get("extra-scope", [])
        need(all(_SCOPE.fullmatch(scope) for scope in extra), "--extra-scope 需要飞书权限名，如 sheets:spreadsheet:create")
        return _call("auth-login", {"extra_scopes": extra}, ctx)
    if action == "poll":
        wait = parse(args, {"wait": "string"}).get("wait")
        seconds = 40.0 if wait is None else js_number(wait)
        need(is_integer(seconds) and 0 <= seconds <= 50, "--wait 必须在 0 到 50 秒之间")
        reply = run_bridge("auth-poll", {"wait_ms": int(seconds) * 1000}, _bridged(ctx))
        if not reply["ok"] and reply["error"]["kind"] == "identity_mismatch":
            # Somebody else is signed in to Feishu web in this user's hidden browser (they scanned the code): it pressed 【授权】 for them and
            # would again at every login, so they are signed out of it.
            try:
                sign_out_browser(resolve_data_paths(ctx.env))
                after = "浏览器里那个账号的飞书网页登录也已退出：重新调用 feishu_login，把新的二维码只发给用户本人扫"
            except Exception as error:  # noqa: BLE001
                after = f"浏览器里那个账号的飞书网页登录没能退出（{error}）：稍后调用 feishu_logout（browser: true）"
            raise Blocked(f"{reply['error']['message']}。{after}")
        return _answer(reply)
    if action == "status":
        parse(args, {})
        return _call("auth-status", None, ctx)
    if action == "logout":
        browser = parse(args, {"browser": "boolean"}).get("browser") is True
        result = _call("auth-logout", None, ctx)
        # --browser: the hidden browser's Feishu web session goes too (the next login shows a sign-in code again).
        if browser:
            sign_out_browser(resolve_data_paths(ctx.env))
        return {**result, **({"browser_signed_out": True} if browser else {})}
    if action == "check":
        parse(args, {})
        return check_login(ctx)
    raise Blocked("login 需要 start、poll、status、logout 或 check")


def _doc(action: str, args: list[str], ctx: Context) -> Result:
    need(action == "fetch", "doc 只有 fetch")
    values = parse(args, {"url": "string", "output": "string", "sheet": "string"})
    need(isinstance(values.get("url"), str) and values["url"], "缺少 --url")
    need(isinstance(values.get("output"), str) and values["output"], "缺少 --output（新的 .md 文件）")
    paths = resolve_data_paths(ctx.env)
    auth = FeishuAuth(paths=paths, app=load_app(ctx.env, paths), expected_open_id=expected_open_id(ctx.env), endpoints=ctx.endpoints)
    try:
        return fetch_doc(FeishuApi(auth), values["url"], values["output"], values.get("sheet"))
    except Blocked:
        raise
    except Exception as error:  # noqa: BLE001
        payload = to_payload(error, auth.safe_secrets())
        if payload["kind"] == "auth_required":
            return {"status": "login_required", "reason": payload["message"], "todo": [LOGIN_TODO]}
        if payload["kind"] == "scope_missing":
            return {"status": "scopes_missing", "reason": payload["message"], "todo": [LOGIN_TODO]}
        # The user's account may not read this document (docx 1770032, wiki 131006, sheets 1310213, or a plain 403): the owner has to share it.
        code = payload.get("code")
        if (code or 0) in NO_ACCESS or "HTTP 403" in payload["message"]:
            raise Blocked(f"这个飞书账号没有这份文档的阅读权限（错误码 {code if code is not None else 403}）：请文档所有者给你开权限，或把内容另存一份发过来") from None
        if payload["kind"] in SHOWN:
            raise Blocked(payload["message"]) from None
        shown = f"，错误码 {code}" if code is not None else ""
        raise Blocked(f"读取飞书文档失败（{payload['kind']}{shown}）：{payload['message']}") from None


def _serve(ctx: Context) -> None:
    """One bridge command per line until stdin ends; answers go out as they are ready, each with the id of its request."""
    source = ctx.stdin or sys.stdin.buffer
    sink = ctx.stdout or sys.stdout.buffer
    write_lock = threading.Lock()
    _NO_ID = object()

    def answer(request: Any) -> None:
        ident = request.get("id", _NO_ID) if isinstance(request, dict) else _NO_ID
        command = request.get("command") if isinstance(request, dict) else None
        if isinstance(command, str):
            reply = run_bridge(command, request.get("input"), _bridged(ctx))
        else:
            reply = {"ok": False, "error": {"kind": "invalid_request", "message": '请求不是 {"id","command","input"}'}}
        line = dumps({"reply": reply} if ident is _NO_ID else {"id": ident, "reply": reply})
        with write_lock:
            sink.write(f"{line}\n".encode("utf-8"))
            sink.flush()

    with ThreadPoolExecutor(max_workers=8, thread_name_prefix="serve") as pool:
        while True:
            raw = source.readline()
            if not raw:
                break
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            if not js_trim(line):
                continue
            try:
                parsed = loads(line)
            except ValueError:
                parsed = None  # not JSON: answered as an invalid request
            pool.submit(answer, parsed if isinstance(parsed, dict) else {})


def main(argv: list[str], ctx: Context | None = None) -> tuple[Result | None, int]:
    ctx = ctx or Context()
    env = ctx.env
    group = argv[0] if argv else None
    command = argv[1] if len(argv) > 1 else None
    rest = argv[2:]
    try:
        if group == "serve":
            _serve(ctx)
            return None, 0
        if group == "bridge":
            name = command or ""
            try:
                text = ""
                if name in INPUT_COMMANDS:
                    source = ctx.stdin or sys.stdin.buffer
                    text = js_trim(source.read().decode("utf-8", errors="replace"))
                value = loads(text) if text else None
            except ValueError:
                return {"ok": False, "error": {"kind": "invalid_request", "message": "标准输入不是有效的 JSON"}}, 2
            reply = run_bridge(name, value, _bridged(ctx))
            return reply, 0 if reply["ok"] else 2
        if group == "info":
            return _call("info", None, ctx), 0
        if group == "doctor":
            return _doctor(ctx), 0
        if group == "login":
            result = _login(command or "", rest, ctx)
            return result, 2 if command == "check" and result.get("status") != "ok" else 0
        if group == "authorize":
            qr = parse([a for a in [command, *rest] if a is not None], {"qr": "string"}).get("qr")
            paths = resolve_data_paths(env)
            result = authorize(_call("auth-status", None, ctx), paths, ctx.web or real_device_web, qr)
            return result, 0
        if group == "doc":
            result = _doc(command or "", rest, ctx)
            return result, 0 if result.get("status") == "saved" else 2
        raise Blocked("用法：feishu.py <login|authorize|doc|doctor|info|bridge|serve> …")
    except Exception as error:  # noqa: BLE001 - every failure is one line of JSON
        return {"status": "blocked", "reason": str(error) if isinstance(error, Exception) else repr(error)}, 2


def run(argv: list[str] | None = None, write: Callable[[bytes], Any] | None = None) -> int:
    """Entry of feishu-sdk/feishu.py: runs the command and prints its line."""
    result, code = main(sys.argv[1:] if argv is None else argv)
    if result is not None:
        out = write or sys.stdout.buffer.write
        out(f"{dumps(result)}\n".encode("utf-8"))
        sys.stdout.buffer.flush()
    return code
