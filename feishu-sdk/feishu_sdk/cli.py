"""feishu.py, the command line of the agent's Feishu module (lark-agent runs it for its feishu_* tools; skills reach it through the bridge).

Every command prints one line of JSON on stdout (`serve` one per request) and exits with 0, or with 2 when it was refused or the answer is not
ok ({"status":"blocked","reason":…} for a refusal).

  login check                                        is the user's Feishu login good right now (asks Feishu)
  login qr --output <file.png> [--extra-scope <s>]…  start a login: the sign-in QR for the user (refused in a group chat)
  login finish                                       after the user scanned: press 【授权】 in the hidden browser, collect and check the login
  login status | logout [--browser]                  the local record; forget the login (and the browser's Feishu web session)
  doc fetch --url <link> --output <file.md> [--sheet <tab>]
  info | doctor                                      what is configured; doctor also needs a login with every permission
  prune                                              remove expendable data (stale temporary files, the hidden browser's caches)
  bridge <command>                                   for other skills: one bridge command, its JSON input on stdin (see bridge.py)
  serve                                              for other skills: one {"id","command","input"} per line on stdin, one {"id","reply"} out

The steps `login qr` and `login finish` are made of are commands too, for tests and for doing a step by hand: `login start
[--extra-scope <s>]…` (a device authorization), `authorize [--qr <file.png>]` (drive it in the hidden browser), `login poll [--wait <s>]`.
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
from .paths import CHAT_TYPE_ENV, expected_open_id, resolve_data_paths
from .prune import prune
from .web_login import DeviceWeb, authorize, real_device_web, sign_out_browser

Result = dict[str, Any]
SHOWN = {"auth_required", "config", "identity_mismatch", "scope_missing", "invalid_request", "login_denied", "login_expired"}
NO_ACCESS = {1770032, 131006, 1310213}
LOGIN_TODO = ("Log the user in to Feishu with your Feishu login (feishu_login: a QR picture for this user alone to scan with the Feishu "
              "phone app; once they say they confirmed, feishu_login_finish. On the command line: `login qr --output <new .png>`, then "
              "`login finish`); then retry")
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
    code = f", code {error['code']}" if "code" in error else ""
    raise Blocked(f"Feishu API call failed ({kind}{code}): {message}")


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
            return {"status": "scopes_missing", **who, "missing_scopes": missing, "reason": "The Feishu login lacks required permissions: log in again", "todo": [LOGIN_TODO]}
        return {"status": "ok", **who}
    kind, message = reply["error"]["kind"], reply["error"]["message"]
    if kind == "auth_required":
        return {"status": "login_required", "reason": message, "todo": [LOGIN_TODO]}
    if kind == "identity_mismatch":
        return {"status": "identity_mismatch", "reason": message}
    if kind == "scope_missing":
        return {"status": "scopes_missing", "reason": message, "todo": [LOGIN_TODO]}
    if kind == "network":
        return {"status": "unreachable", "reason": "Feishu cannot be reached right now: check again shortly; the login is not lost"}
    if kind in SHOWN:
        return {"status": "blocked", "reason": message}
    return {"status": "blocked", "reason": "A Feishu API call failed, so the login could not be checked"}


def _doctor(ctx: Context) -> Result:
    info = _call("info", None, ctx)
    need(info.get("app_configured") is True, "No Feishu app configured (FEISHU_APP_ID / FEISHU_APP_SECRET): the administrator has to set it")
    auth = _call("auth-status", None, ctx)
    need(auth.get("logged_in") is True, "Not logged in to Feishu: log the user in with `login qr`")
    missing = auth.get("missing_scopes") or []
    need(not missing, f"The Feishu login lacks permissions: {', '.join(missing)}; log in again")
    result = {"version": info.get("bridge"), "python": info.get("python"), "logged_in": True, "identity": "user", "user_name": auth.get("name"),
              "open_id": auth.get("open_id"), "storage": auth.get("storage"), "root": info.get("root"), "required_scopes": info.get("required_scopes")}
    return {key: value for key, value in result.items() if value is not None}


def _login(action: str, args: list[str], ctx: Context) -> Result:
    if action == "start":
        extra = parse(args, {"extra-scope": "string"}, frozenset({"extra-scope"})).get("extra-scope", [])
        need(all(_SCOPE.fullmatch(scope) for scope in extra), "--extra-scope takes a Feishu permission name, e.g. sheets:spreadsheet:create")
        return _call("auth-login", {"extra_scopes": extra}, ctx)
    if action == "poll":
        wait = parse(args, {"wait": "string"}).get("wait")
        seconds = 40.0 if wait is None else js_number(wait)
        need(is_integer(seconds) and 0 <= seconds <= 50, "--wait must be between 0 and 50 seconds")
        reply = run_bridge("auth-poll", {"wait_ms": int(seconds) * 1000}, _bridged(ctx))
        if not reply["ok"] and reply["error"]["kind"] == "identity_mismatch":
            # Somebody else is signed in to Feishu web in this user's hidden browser (they scanned the code): it pressed 【授权】 for them and
            # would again at every login, so they are signed out of it.
            try:
                sign_out_browser(resolve_data_paths(ctx.env))
                after = "That account was signed out of Feishu web in the hidden browser too: run `login qr` again and give the new code to the user alone"
            except Exception as error:  # noqa: BLE001
                after = f"That account could not be signed out of Feishu web in the hidden browser ({error}): run `login logout --browser` shortly"
            raise Blocked(f"{reply['error']['message']}. {after}")
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
    if action == "qr":
        return _login_qr(args, ctx)
    if action == "finish":
        parse(args, {})
        return _login_finish(ctx)
    raise Blocked("login takes check, qr, finish, status or logout (or the steps start and poll)")


#: The browser could not be started: the user confirms the authorization through its link instead.
_NO_BROWSER = re.compile(r"browser did not start|websockets|Neither Chrome nor Edge", re.IGNORECASE)


def _step(run: Callable[[], Result]) -> Result:
    """One step of the login flow; a refusal is a result like any other."""
    try:
        return run()
    except Exception as error:  # noqa: BLE001
        return {"status": "blocked", "reason": str(error)}


def _collect(ctx: Context) -> Result:
    """Collects the login once it has been confirmed (in the hidden browser, or by the user on the link), then checks it with Feishu."""
    poll = _step(lambda: _login("poll", ["--wait", "40"], ctx))
    if poll.get("status") == "blocked":
        return poll
    if poll.get("state") == "authorized":
        return check_login(ctx)
    if poll.get("state") == "pending":
        return {"status": "pending", "message": "The user has not confirmed on the phone yet: once they say they have, run `login finish` again"}
    return {"status": poll.get("state") or "blocked", "message": "The authorization was denied or has expired: start over with `login qr`"}


def _authorize(qr: str | None, ctx: Context) -> Result:
    return authorize(_call("auth-status", None, ctx), resolve_data_paths(ctx.env), ctx.web or real_device_web, qr)


def _login_qr(args: list[str], ctx: Context) -> Result:
    """`login qr`: a device authorization, driven in this user's hidden browser up to Feishu's sign-in QR, saved as a picture for the user."""
    values = parse(args, {"output": "string", "extra-scope": "string"}, frozenset({"extra-scope"}))
    output = values.get("output")
    need(isinstance(output, str) and output, "--output (a new .png file for the QR picture) is missing")
    # The QR signs in whoever scans it: it goes to this user alone, never to a group.
    if (ctx.env.get(CHAT_TYPE_ENV) or "").strip() == "group":
        return {"status": "blocked", "reason": "The Feishu login QR is never shown in a group chat: ask the user to message you privately and log in there"}
    started = _step(lambda: _login("start", [arg for scope in values.get("extra-scope", []) for arg in ("--extra-scope", scope)], ctx))
    if started.get("status") == "blocked":
        return started
    shown = _step(lambda: _authorize(os.path.abspath(output), ctx))
    status = shown.get("status")
    if status == "qr_ready":
        return {"status": "qr_ready", "qr_file": shown.get("qr_file"),
                "message": "Give the QR picture to the user to scan with the Feishu phone app and confirm on the phone (it expires in a few minutes; "
                           "the same scan signs the hidden browser in to Feishu web, so skills that use it do not ask again). Once the user says "
                           "they confirmed, run `login finish`"}
    if status == "approved":
        return _collect(ctx)
    if status == "stuck":
        after = _collect(ctx)
        return after if after.get("status") == "ok" else {"status": "stuck", "message": "The authorization page did not open properly: run `login qr` again"}
    if status == "blocked" and _NO_BROWSER.search(str(shown.get("reason") or "")):
        return {"status": "no_browser", "verification_uri_complete": started.get("verification_uri_complete"), "user_code": started.get("user_code"),
                "expires_in_s": started.get("expires_in_s"),
                "message": "No usable browser on this computer, so there is no QR: give the user the link and the code to open and confirm in "
                           "Feishu; once they say they confirmed, run `login finish`"}
    return shown


def _login_finish(ctx: Context) -> Result:
    """`login finish`: after the user scanned, press 【授权】 in the hidden browser, then collect and check the login."""
    done = _step(lambda: _authorize(None, ctx))
    status = done.get("status")
    if status == "login_required":
        return done
    if status == "no_pending":
        return check_login(ctx) if done.get("logged_in") else done
    if status == "blocked" and not _NO_BROWSER.search(str(done.get("reason") or "")):
        return done
    return _collect(ctx)


def _doc(action: str, args: list[str], ctx: Context) -> Result:
    need(action == "fetch", "doc takes fetch")
    values = parse(args, {"url": "string", "output": "string", "sheet": "string"})
    need(isinstance(values.get("url"), str) and values["url"], "--url is missing")
    need(isinstance(values.get("output"), str) and values["output"], "--output (a new .md file) is missing")
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
            raise Blocked(f"This Feishu account may not read the document (code {code if code is not None else 403}): the owner has to share it with the user, or the user can send its content another way") from None
        if payload["kind"] in SHOWN:
            raise Blocked(payload["message"]) from None
        shown = f", code {code}" if code is not None else ""
        raise Blocked(f"Reading the Feishu document failed ({payload['kind']}{shown}): {payload['message']}") from None


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
            reply = {"ok": False, "error": {"kind": "invalid_request", "message": 'The request is not {"id","command","input"}'}}
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
                return {"ok": False, "error": {"kind": "invalid_request", "message": "Standard input is not valid JSON"}}, 2
            reply = run_bridge(name, value, _bridged(ctx))
            return reply, 0 if reply["ok"] else 2
        if group == "info":
            return _call("info", None, ctx), 0
        if group == "doctor":
            return _doctor(ctx), 0
        if group == "login":
            result = _login(command or "", rest, ctx)
            failed = result.get("status") == "blocked" or (command in ("check", "finish") and result.get("status") != "ok")
            return result, 2 if failed else 0
        if group == "authorize":
            qr = parse([a for a in [command, *rest] if a is not None], {"qr": "string"}).get("qr")
            return _authorize(qr, ctx), 0
        if group == "prune":
            parse([a for a in [command, *rest] if a is not None], {})
            return prune(env), 0
        if group == "doc":
            result = _doc(command or "", rest, ctx)
            return result, 0 if result.get("status") == "saved" else 2
        raise Blocked("Usage: feishu.py <login|doc|doctor|info|prune|bridge|serve|authorize> …")
    except Exception as error:  # noqa: BLE001 - every failure is one line of JSON
        return {"status": "blocked", "reason": str(error) if isinstance(error, Exception) else repr(error)}, 2


def run(argv: list[str] | None = None, write: Callable[[bytes], Any] | None = None) -> int:
    """Entry of scripts/feishu.py: runs the command and prints its line."""
    result, code = main(sys.argv[1:] if argv is None else argv)
    if result is not None:
        out = write or sys.stdout.buffer.write
        out(f"{dumps(result)}\n".encode("utf-8"))
        sys.stdout.buffer.flush()
    return code
