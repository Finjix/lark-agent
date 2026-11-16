"""audit.py, the skill's one command line (port of cli.ts).

Progress goes to stderr while it works; the result is one line of JSON on stdout, and the exit code is 0, or 2 when it was refused
({"status":"blocked","reason":…}) or failed.

  review --project GOF|GOT --file <path> [--file <path>]… [--note <text>]   review videos / images (zips are unpacked)
  rules watch --project GOF|GOT [--since YYYY-MM-DD]                         listen to this turn's group, reading its history now
  rules unwatch                                                              stop listening to this turn's group
  rules sync [--here]                                                        read what is new in every group (or this one); the daily job
  rules status                                                               the groups listened to, rules per project
  rules show --project GOF|GOT [--output <file.md>]                          the dynamic spec as Markdown: a file for the user, or in the answer
  rules remove --project GOF|GOT --id D<n> [--id D<n>]…                      take rules out of the dynamic spec
  doctor | info                                                              is everything in place / what is configured
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Mapping

from .config import feishu_app, gateway, scratch_root, shared_dir, skill_root, skill_version, turn_of
from .errors import Blocked, message_of, need, redact
from .gemini import JsonRequest, Model, gateway_model
from .jsutil import SHANGHAI, iso_now_name
from .lark import Lark, LarkError, bot_lark, explain_lark
from .media import find_ffmpeg
from .projects import PROJECTS, fixed_spec, project_of
from .review import ReviewContext, review
from .rules import Busy, RulesContext, load_book, remove_rules, show, status, sync_all, unwatch, watch

Result = dict[str, Any]
#: Context.ffmpeg when the caller says nothing: look for one.
FIND = object()


@dataclass
class Context:
    """Whom a command serves (the environment the host gave), and the test seams. `ffmpeg` None: as if this computer had no ffmpeg."""
    env: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))
    model: Model | None = None
    lark: Lark | None = None
    now: Callable[[], int] | None = None
    log: Callable[[str], None] | None = None
    ffmpeg: Any = FIND
    home: str | None = None


def _parse(args: list[str], options: dict[str, tuple[str, bool]]) -> dict[str, Any]:
    """Options as node:util parseArgs takes them in strict mode, without positionals: `options` maps a name to (type, multiple).
    A string option takes the next argument whatever it starts with: a value that starts with "-" (a note like "-B 面从 5 秒开始") is
    still the value."""
    values: dict[str, Any] = {}
    i = 0
    while i < len(args):
        arg = args[i]
        i += 1
        if arg == "--":
            if i < len(args):
                raise Blocked(f"Unexpected argument '{args[i]}'. This command does not take positional arguments")
            break
        if not arg.startswith("-") or arg == "-":
            raise Blocked(f"Unexpected argument '{arg}'. This command does not take positional arguments")
        name, has_value, inline = (arg[2:].partition("=") if arg.startswith("--") else (arg[1:], "", ""))
        spec = options.get(name) if arg.startswith("--") else None
        if spec is None:
            raise Blocked(f"Unknown option '{arg.split('=')[0]}'")
        kind, multiple = spec
        if kind == "boolean":
            if has_value:
                raise Blocked(f"Option '--{name}' does not take an argument")
            value: Any = True
        else:
            if has_value:
                value = inline
            elif i < len(args):
                value = args[i]
                i += 1
            else:
                raise Blocked(f"Option '--{name} <value>' argument missing")
        if multiple:
            values.setdefault(name, []).append(value)
        else:
            values[name] = value
    return values


def _model_of(ctx: Context) -> Model:
    return ctx.model if ctx.model is not None else gateway_model(gateway(ctx.env))


def _lark_of(ctx: Context) -> Lark:
    return ctx.lark if ctx.lark is not None else bot_lark(feishu_app(ctx.env))


def _log_of(ctx: Context) -> Callable[[str], None]:
    if ctx.log:
        return ctx.log

    def log(line: str) -> None:
        sys.stderr.write(f"{line}\n")
        sys.stderr.flush()

    return log


def _home(ctx: Context) -> str:
    return ctx.home or os.path.expanduser("~")


def _ffmpeg(ctx: Context) -> str | None:
    return find_ffmpeg(ctx.env) if ctx.ffmpeg is FIND else ctx.ffmpeg


def _rules_context(ctx: Context) -> RulesContext:
    return RulesContext(lark=_lark_of(ctx), model=_model_of(ctx), root=skill_root(ctx.env), shared=shared_dir(ctx.env, _home(ctx)),
                        log=_log_of(ctx), now=ctx.now)


def _since_of(value: Any) -> int | None:
    if value is None:
        return None
    text = str(value).strip()
    need(re.fullmatch(r"\d{4}-\d{2}-\d{2}", text), "--since 要写成 YYYY-MM-DD，比如 2026-09-01")
    try:
        # A day the month does not have (2026-02-30) is refused, not taken for one in the next month.
        at = datetime(int(text[:4]), int(text[5:7]), int(text[8:10]), tzinfo=SHANGHAI)
    except ValueError:
        raise Blocked(f"--since 不是有效的日期：{text}") from None
    return int(at.timestamp() * 1000)


def _keep_result(ctx: Context, payload: Result, markdown: str) -> str | None:
    """Where the results of a user's reviews are kept for follow-up questions (never the workspace); the newest 30 stay."""
    user = (ctx.env.get("LARK_AGENT_USER_DIR") or "").strip()
    directory = os.path.join(os.path.abspath(user), "audit-auto", "reviews") if user else os.path.join(_home(ctx), ".audit-auto", "reviews")
    try:
        os.makedirs(directory, exist_ok=True)
        name = f"{iso_now_name()}-{os.getpid()}"
        with open(os.path.join(directory, f"{name}.json"), "w", encoding="utf-8", newline="") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, indent=2))
        with open(os.path.join(directory, f"{name}.md"), "w", encoding="utf-8", newline="") as handle:
            handle.write(markdown)
        kept = sorted(f for f in os.listdir(directory) if f.endswith(".json"))
        for old in kept[:max(0, len(kept) - 30)]:
            for ext in (".json", ".md"):
                try:
                    os.remove(os.path.join(directory, old[:-5] + ext))
                except OSError:
                    pass
        return os.path.join(directory, f"{name}.md")
    except OSError:
        return None


def _scratch_folder(ctx: Context) -> str:
    """A scratch folder of this run, gone when it ends; ones left by runs that died are swept after a day."""
    root = scratch_root(ctx.env)
    os.makedirs(root, exist_ok=True)
    for old in os.listdir(root):
        at = os.path.join(root, old)
        try:
            if time.time() - os.stat(at).st_mtime > 24 * 60 * 60:
                shutil.rmtree(at, ignore_errors=True) if os.path.isdir(at) else os.remove(at)
        except OSError:
            pass  # in use
    return tempfile.mkdtemp(prefix="run-", dir=root)


def _review_command(args: list[str], ctx: Context) -> Result:
    values = _parse(args, {"project": ("string", False), "file": ("string", True), "note": ("string", False)})
    need(values.get("project"), "缺少 --project（GOF 或 GOT）：先问用户这是哪个项目的素材")
    project = project_of(values["project"])
    files: list[str] = values.get("file") or []
    need(files, "缺少 --file：要审的视频或图片")
    scratch = _scratch_folder(ctx)
    try:
        note = values.get("note")
        outcome = review(project, [os.path.abspath(f) for f in files], note.strip() if isinstance(note, str) and note.strip() else None,
                         ReviewContext(model=_model_of(ctx), root=skill_root(ctx.env), shared=shared_dir(ctx.env, _home(ctx)), scratch=scratch,
                                       ffmpeg=_ffmpeg(ctx), log=_log_of(ctx)))
        details: Result = {"project": project.id, "rules": outcome.rules, "files": outcome.results, "skipped": outcome.skipped}
        kept = _keep_result(ctx, details, outcome.markdown)
        reviewed = sum(1 for r in outcome.results if r["status"] == "reviewed")
        listed = []
        for r in outcome.results:
            item: Result = {"name": r["name"], "status": r["status"]}
            if r.get("reason"):
                item["reason"] = r["reason"]
            if r.get("verdict"):
                item["verdict"] = r["verdict"]
            if r.get("findings"):
                item["issues"] = len(r["findings"]["issues"])
            listed.append(item)
        # A host may shorten a long output by leaving out its middle: what the skill needs to find the whole result goes first, the
        # Markdown last.
        result: Result = {"status": "done" if reviewed else "failed"}
        if kept:
            result["result_file"] = kept
        result.update({"project": project.id, "rules": outcome.rules, "files": listed})
        if outcome.skipped:
            result["skipped"] = outcome.skipped
        result["markdown"] = outcome.markdown
        return result
    finally:
        # A file still held open (Windows) is left to the sweep after a day: the review itself is done.
        shutil.rmtree(scratch, ignore_errors=True)


def _rules_command(action: str | None, args: list[str], ctx: Context) -> Result:
    turn = turn_of(ctx.env)

    def shared() -> str:
        return shared_dir(ctx.env, _home(ctx))

    if action == "watch":
        values = _parse(args, {"project": ("string", False), "since": ("string", False)})
        need(values.get("project"), "缺少 --project：先问用户这个群是 GOF 还是 GOT 的审核群")
        return watch(project_of(values["project"]), _since_of(values.get("since")), turn, _rules_context(ctx))
    if action == "unwatch":
        _parse(args, {})
        return unwatch(turn, shared())
    if action == "sync":
        values = _parse(args, {"here": ("boolean", False)})
        need(not values.get("here") or turn.chat, "不知道这条消息来自哪个群：请在要更新的群里 @机器人 再说一次")
        try:
            return sync_all(_rules_context(ctx), chat=turn.chat if values.get("here") else None, wait_ms=5 * 60_000 if turn.chat else 0)
        except Busy as error:
            # The daily job finding a user's watch (or the previous run) still at work: nothing is lost, the next run reads on from there.
            if not turn.chat:
                return {"status": "busy", "reason": error.message}
            raise
    if action == "status":
        _parse(args, {})
        return status(shared(), turn)
    if action == "show":
        values = _parse(args, {"project": ("string", False), "output": ("string", False)})
        need(values.get("project"), "缺少 --project（GOF 或 GOT）")
        output = values.get("output")
        return show(shared(), project_of(values["project"]), output if isinstance(output, str) and output else None)
    if action == "remove":
        values = _parse(args, {"project": ("string", False), "id": ("string", True)})
        need(values.get("project"), "缺少 --project（GOF 或 GOT）")
        return remove_rules(project_of(values["project"]), values.get("id") or [], turn, shared(), ctx.now)
    raise Blocked("rules 需要 watch、unwatch、sync、status、show 或 remove")


def _info(ctx: Context) -> Result:
    root, shared = skill_root(ctx.env), shared_dir(ctx.env, _home(ctx))
    model: str | None = None
    key = False
    try:
        g = gateway(ctx.env)
        model = f"{g.model} (thinking {g.thinking_level.lower()}) @ {g.base_url}/{g.api_version}"
        key = True
    except Blocked:
        pass  # reported by doctor
    return {
        "status": "ok", "skill": "audit-auto", "version": skill_version(root), "python": platform.python_version(), "model": model,
        "gateway_key": key, "shared": shared,
        "projects": [{"id": p.id, "fixed_spec": not fixed_spec(root, p).placeholder, "dynamic_rules": len(load_book(shared, p.id)["rules"])}
                     for p in PROJECTS.values()],
    }


def _doctor(ctx: Context) -> Result:
    checks: list[Result] = []

    def check(name: str, fn: Callable[[], str | None]) -> None:
        try:
            detail = fn()
            checks.append({"check": name, "ok": True, **({"detail": detail} if detail else {})})
        except Exception as error:  # noqa: BLE001 - every check reports its own failure
            checks.append({"check": name, "ok": False,
                           "detail": error.message if isinstance(error, Blocked) else redact(message_of(error))[:300]})

    def fixed_specs() -> str:
        root = skill_root(ctx.env)
        return "，".join(f"{p.id}: {'占位（只用动态规范）' if fixed_spec(root, p).placeholder else '有'}" for p in PROJECTS.values())

    def ai_gateway() -> str:
        model = _model_of(ctx)
        reply = model.json(JsonRequest(system='Answer with this JSON: {"ok": true}', parts=[{"text": "ping"}], max_output_tokens=2048))
        need(isinstance(reply.data, dict) and reply.data.get("ok") is True, "AI 网关的回答不对")
        return f"{model.name}，{reply.ms} ms"

    def feishu() -> str:
        lark = _lark_of(ctx)
        lark.verify()
        return lark.app_id

    def shared_folder() -> str:
        shared = shared_dir(ctx.env, _home(ctx))
        os.makedirs(shared, exist_ok=True)
        need(os.access(shared, os.W_OK), f"共享目录不可写：{shared}")
        return shared

    def ffmpeg() -> str:
        return "有（大视频先压到 720p 再发）" if _ffmpeg(ctx) else "没有（视频按原文件发，最大 150 MB）"

    def groups() -> str:
        s = status(shared_dir(ctx.env, _home(ctx)), turn_of({}))
        failing = [g for g in s["groups"] if g.get("error")]
        need(not failing, f"有 {len(failing)} 个群上次没读成：{failing[0]['error'] if failing else ''}")
        return f"在监听 {len(s['groups'])} 个群"

    check("fixed_specs", fixed_specs)
    check("ai_gateway", ai_gateway)
    check("feishu_app", feishu)
    check("shared_folder", shared_folder)
    check("ffmpeg", ffmpeg)
    check("groups", groups)
    return {"status": "ok" if all(c["ok"] for c in checks) else "problems", "checks": checks}


def _run(args: list[str], ctx: Context) -> Result:
    command, rest = (args[0], args[1:]) if args else (None, [])
    if command == "review":
        return _review_command(rest, ctx)
    if command == "rules":
        return _rules_command(rest[0] if rest else None, rest[1:], ctx)
    if command == "info":
        _parse(rest, {})
        return _info(ctx)
    if command == "doctor":
        _parse(rest, {})
        return _doctor(ctx)
    raise Blocked("用法：review | rules <watch|unwatch|sync|status|show|remove> | doctor | info")


def main(args: list[str], ctx: Context | None = None) -> tuple[Result, int]:
    """The result and the exit code: 2 when refused, failed, or a check failed."""
    ctx = ctx or Context()
    try:
        result = _run(args, ctx)
        return result, 2 if result.get("status") in ("blocked", "failed", "problems") else 0
    except Blocked as error:
        return {"status": "blocked", "reason": error.message}, 2
    except Exception as error:  # noqa: BLE001 - anything else is a refusal too, told without credentials
        reason = explain_lark(error) if isinstance(error, LarkError) else f"出错了：{redact(message_of(error))[:300]}"
        return {"status": "blocked", "reason": reason}, 2
