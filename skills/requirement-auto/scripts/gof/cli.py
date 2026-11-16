"""The one entry point of the skill's scripts (cli.ts): `gof.py batch …`, `gof.py feishu …`, `gof.py cls …`, `gof.py creative …`, `gof.py login check`.

Every command prints one line of canonical JSON on stdout; a refusal is {"status":"blocked","reason":…} and exit code 2.
"""

from __future__ import annotations

import errno
import os
import re
from typing import Any, Optional

from . import js
from .util import Blocked, WriteSent, check_target, is_inside, need, read_json, write_json_new

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _bundled_receipt() -> str:
    """The overwrite-protection receipt that ships with the skill (its assets)."""
    return os.path.join(SKILL_DIR, "assets", "overwrite-receipt.json")


class _ArgError(Exception):
    pass


def _parse(args: list[str], options: dict[str, str], positionals: bool = False) -> tuple[dict[str, Any], list[str]]:
    """Node's util.parseArgs in strict mode: `--name value`, `--name=value`, boolean flags, `--` ends the options."""
    values: dict[str, Any] = {}
    rest: list[str] = []
    i = 0
    try:
        while i < len(args):
            arg = args[i]
            if arg == "--":
                rest.extend(args[i + 1:])
                break
            if arg.startswith("--"):
                name, has_value, inline = arg[2:].partition("=")
                if name not in options:
                    raise _ArgError(f"Unknown option '--{name}'. To specify a positional argument starting with a '-', place it at the end of the command after '--', as in '-- \"--{name}\"")
                if options[name] == "boolean":
                    if has_value:
                        raise _ArgError(f"Option '--{name}' does not take an argument")
                    values[name] = True
                else:
                    if has_value:
                        values[name] = inline
                    else:
                        if i + 1 >= len(args):
                            raise _ArgError(f"Option '--{name} <value>' argument missing")
                        following = args[i + 1]
                        if following.startswith("-"):
                            raise _ArgError(f"Option '--{name}' argument is ambiguous.\nDid you forget to specify the option argument for '--{name}'?\n"
                                            f"To specify an option argument starting with a dash use '--{name}=-XYZ'.")
                        values[name] = following
                        i += 1
            elif arg.startswith("-") and arg != "-":
                raise _ArgError(f"Unknown option '{arg[:2]}'. To specify a positional argument starting with a '-', place it at the end of the command after '--', as in '-- \"{arg}\"")
            else:
                if not positionals:
                    raise _ArgError(f"Unexpected argument '{arg}'. This command does not take positional arguments")
                rest.append(arg)
            i += 1
    except _ArgError as error:
        raise Blocked(str(error)) from None
    if rest and not positionals:
        raise Blocked(f"Unexpected argument '{rest[0]}'. This command does not take positional arguments")
    return values, rest


def _str(v: Any) -> Optional[str]:
    return v if isinstance(v, str) else None


def _must(v: Any, flag: str) -> str:
    need(isinstance(v, str) and v != "", f"缺少 --{flag}")
    return v


def _int_of(v: Any, flag: str) -> int:
    need(isinstance(v, str) and re.fullmatch(r"[1-9][0-9]*", v), f"--{flag} 需要正整数")
    return int(v)


def _open_snapshot(values: dict) -> Any:
    from .snapshot import Snapshot

    file = _str(values.get("snapshot"))
    if not file:
        return None
    check_target(_str(values.get("observed-url")) or "")
    return Snapshot(file)


def _batch_command(command: str, args: list[str]) -> dict:
    from . import batch
    from .creative import parse_numbers
    from .plan import make_plan
    from .snapshot import Snapshot

    common = {"snapshot": "string", "observed-url": "string", "batch": "string"}
    if command == "plan":
        values, _ = _parse(args, {"snapshot": "string", "observed-url": "string", "request": "string", "current-sheet": "string"})
        snap = _open_snapshot(values)
        need(snap, "缺少 --snapshot")
        return make_plan(snap, read_json(_must(values.get("request"), "request")), _str(values.get("current-sheet")))
    if command == "prepare":
        values, _ = _parse(args, {"snapshot": "string", "observed-url": "string", "plan": "string", "output-root": "string", "workspace": "string"})
        snap = _open_snapshot(values)
        need(snap, "缺少 --snapshot")
        return batch.prepare(read_json(_must(values.get("plan"), "plan")), snap, workspace=_str(values.get("workspace")), output_root=_str(values.get("output-root")))
    if command == "preflight":
        values, _ = _parse(args, common)
        snap = _open_snapshot(values)
        need(snap, "缺少 --snapshot")
        return batch.preflight(_must(values.get("batch"), "batch"), snap)
    if command == "verify":
        values, _ = _parse(args, {**common, "validation-evidence": "string"})
        snap = _open_snapshot(values)
        need(snap, "缺少 --snapshot")
        evidence = _str(values.get("validation-evidence"))
        return batch.verify(_must(values.get("batch"), "batch"), snap, read_json(evidence) if evidence else None)
    if command == "record":
        values, _ = _parse(args, {**common, "step": "string", "group": "string", "evidence": "string"})
        snap = _open_snapshot(values)
        evidence = _str(values.get("evidence"))
        return batch.record(_must(values.get("batch"), "batch"), _must(values.get("step"), "step"), _str(values.get("group")),
                            read_json(evidence) if evidence else None, snap)
    if command == "status":
        return batch.status(_must(_parse(args, {"batch": "string"})[0].get("batch"), "batch"))
    if command == "list":
        number = _str(_parse(args, {"number": "string"})[0].get("number"))
        return batch.list_batches(None, None if number is None else _int_of(re.sub(r"^GOFCN", "", number, flags=re.IGNORECASE), "number"))
    if command == "adopt":
        values, _ = _parse(args, {"numbers": "string", "snapshot": "string", "supplier": "string", "reviewer": "string", "output-root": "string"})
        numbers = parse_numbers(_must(values.get("numbers"), "numbers"))
        # The export from `gof feishu snapshot`, as for `gof feishu plan`: nothing is written, the rows are only read from it.
        snap = Snapshot(_must(values.get("snapshot"), "snapshot"))
        return batch.adopt(snap, numbers, supplier=_str(values.get("supplier")), reviewer=_str(values.get("reviewer")), output_root=_str(values.get("output-root")))
    if command == "ticket":
        values, _ = _parse(args, {"batch": "string", "group": "string", "ticket": "string", "catalog": "string", "preview": "boolean"})
        return batch.attach_ticket(_must(values.get("batch"), "batch"), _must(values.get("group"), "group"), read_json(_must(values.get("ticket"), "ticket")),
                                   read_json(_must(values.get("catalog"), "catalog")), values.get("preview") is True)
    if command == "message":
        return batch.message(_must(_parse(args, {"batch": "string"})[0].get("batch"), "batch"))
    if command == "cls-check":
        values, _ = _parse(args, {"batch": "string", "group": "string", "action": "string", "ui": "string", "observations": "string"})
        need(("ui" in values) != ("observations" in values), "需要且只能给出 --ui 或 --observations 之一")
        return batch.cls_check(_must(values.get("batch"), "batch"), _must(values.get("group"), "group"), _must(values.get("action"), "action"),
                               read_json(_must(values.get("ui") if "ui" in values else values.get("observations"), "observations")))
    if command == "report":
        values, _ = _parse(args, {"batch": "string", "output": "string"})
        directory = _must(values.get("batch"), "batch")
        result = batch.report(directory)
        output = _str(values.get("output"))
        if not output:
            return result
        file = os.path.abspath(output)
        need(is_inside(directory, file), "报告必须位于本批目录")
        write_json_new(file, result)
        return {"status": "report_saved", "report": file}
    raise Blocked(f"未知的 batch 子命令：{command}")


class _Forwarded(Exception):
    """Results of the feishu skill's own commands (login, authorize) come back with its exit code."""

    def __init__(self, answer: dict) -> None:
        super().__init__("forwarded")
        self.answer = answer


def _feishu_command(command: str, args: list[str], bridge: Any, via: Any) -> dict:
    from . import feishu
    from .brief import read_brief
    from .context import context
    from .doc import edit_doc, outline
    from .shots_online import doc_picture, sheet_picture
    from .snapshot import Snapshot

    if command == "screenshot":
        values, _ = _parse(args, {"batch": "string", "output": "string"})
        return sheet_picture(via, _must(values.get("batch"), "batch"), _must(values.get("output"), "output"))
    if command == "doctor":
        return bridge.doctor()
    if command == "login":
        values, positionals = _parse(args, {"wait": "string", "probe-scope": "boolean"}, True)
        need(len(positionals) == 1 and positionals[0] in ("start", "poll", "status", "logout"), "login 需要 start、poll、status 或 logout")
        wait = _str(values.get("wait"))
        raise _Forwarded(feishu.login(positionals[0], wait=js.number(wait) if wait is not None else None, probe_scope=values.get("probe-scope") is True))
    if command == "authorize":
        values, _ = _parse(args, {"qr": "string"})
        raise _Forwarded(feishu.authorize_feishu(_str(values.get("qr"))))
    if command == "probe":
        return feishu.probe(bridge, _must(_parse(args, {"receipt": "string"})[0].get("receipt"), "receipt"))
    if command == "append":
        values, _ = _parse(args, {"batch": "string", "receipt": "string"})
        return feishu.execute(bridge, _must(values.get("batch"), "batch"), _str(values.get("receipt")) or _bundled_receipt())
    if command == "reconcile":
        return feishu.reconcile(bridge, _must(_parse(args, {"batch": "string"})[0].get("batch"), "batch"))
    if command == "document":
        values, _ = _parse(args, {"url": "string", "sheet": "string", "full": "boolean", "outline": "boolean", "block": "string"})
        if values.get("outline") or "block" in values:
            need("sheet" not in values, "--outline 只用于新版文档（docx）；表格直接读，行号和列字母就是位置")
            block = _str(values.get("block"))
            need(block is None or re.fullmatch(r"[0-9]+", block), "--block 需要顶层块的序号（从 0 数）")
            return outline(bridge, _must(values.get("url"), "url"), full=values.get("full") is True, block=int(block) if block is not None else None)
        return read_brief(bridge, _must(values.get("url"), "url"), sheet=_str(values.get("sheet")), full=values.get("full") is True)
    if command == "doc-screenshot":
        values, _ = _parse(args, {"url": "string", "output": "string", "block": "string", "sheet": "string", "range": "string"})
        focus: dict[str, Any] = {}
        if _str(values.get("block")):
            focus["block"] = values["block"]
        if _str(values.get("sheet")):
            focus["sheet_id"] = values["sheet"]
        if _str(values.get("range")):
            focus["range"] = values["range"].upper()
        return doc_picture(via, _must(values.get("url"), "url"), focus, _must(values.get("output"), "output"))
    if command == "doc-edit":
        values, _ = _parse(args, {"url": "string", "edit": "string", "check": "boolean"})
        return edit_doc(bridge, _must(values.get("url"), "url"), read_json(_must(values.get("edit"), "edit")), values.get("check") is True)
    if command == "context":
        values, _ = _parse(args, {"snapshot": "string", "worksheet": "string", "supplier": "string"})
        snap = Snapshot(_must(values.get("snapshot"), "snapshot"))
        return context(bridge, snap, worksheet=_str(values.get("worksheet")))
    if command == "dropdown-add":
        values, _ = _parse(args, {"field": "string", "option": "string", "sheet": "string", "color": "string"})
        return feishu.add_option(bridge, _must(values.get("field"), "field"), _must(values.get("option"), "option"), _must(values.get("sheet"), "sheet"),
                                 _str(values.get("color")))
    if command == "snapshot":
        return feishu.take_snapshot(bridge, _must(_parse(args, {"output": "string"})[0].get("output"), "output"))
    if command == "plan":
        values, _ = _parse(args, {"snapshot": "string", "request": "string", "output": "string"})
        snap = Snapshot(_must(values.get("snapshot"), "snapshot"))
        output = _str(values.get("output"))
        file = None if output is None else os.path.abspath(output)
        need(file is None or not os.path.exists(file), "计划文件已存在，不覆盖")
        result = feishu.plan_with_api(bridge, snap, read_json(_must(values.get("request"), "request")))
        if file is None:
            return result
        write_json_new(file, result)
        return feishu.plan_summary(result, file)
    if command == "prepare":
        from .batch import prepare

        values, _ = _parse(args, {"plan": "string", "snapshot": "string", "output-root": "string", "workspace": "string"})
        snap = Snapshot(_must(values.get("snapshot"), "snapshot"))
        approved = read_json(_must(values.get("plan"), "plan"))
        fresh = feishu.recompute_plan(bridge, snap, approved)
        return prepare(fresh["batch_plan"], snap, workspace=_str(values.get("workspace")), api_plan=fresh["api_plan"], output_root=_str(values.get("output-root")))
    raise Blocked(f"未知的 feishu 子命令：{command}")


CLS_ONLINE = ["login", "session", "catalog", "observe", "run", "read-draft", "verify", "screenshot"]


def _cls_online_command(command: str, args: list[str], via: Any) -> dict:
    """The commands that talk to CLS: they log in through the browser on this user's profile and work over HTTP (see cls_online.py)."""
    from . import cls_online as online
    from .shots_online import draft_picture

    if command == "login":
        values, _ = _parse(args, {"qr": "string"})
        qr = _str(values.get("qr"))
        need(qr, "cls login 需要 --qr <png文件>：登录用二维码，由用户用飞书手机端扫码，不弹出浏览器窗口")
        return online.login_with_qr(via, qr)
    if command == "session":
        _parse(args, {})
        return online.session_status(via)
    if command == "catalog":
        return online.catalog(via, _must(_parse(args, {"output": "string"})[0].get("output"), "output"))
    if command == "observe":
        values, _ = _parse(args, {"action": "string", "draft": "string", "output": "string"})
        return online.observe(via, _must(values.get("action"), "action"), _must(values.get("output"), "output"),
                              None if "draft" not in values else _int_of(values["draft"], "draft"))
    if command == "run":
        values, _ = _parse(args, {"batch": "string", "operation": "string", "output": "string"})
        return online.run(via, _must(values.get("batch"), "batch"), _must(values.get("operation"), "operation"), _must(values.get("output"), "output"))
    if command == "screenshot":
        values, _ = _parse(args, {"batch": "string", "group": "string", "output": "string"})
        return draft_picture(via, _must(values.get("batch"), "batch"), _str(values.get("group")), _must(values.get("output"), "output"))
    if command == "read-draft":
        return online.read_draft(via, _int_of(_parse(args, {"id": "string"})[0].get("id"), "id"))
    if command == "verify":
        values, _ = _parse(args, {"batch": "string", "operation": "string", "draft": "string", "output": "string"})
        return online.verify_draft(via, _must(values.get("batch"), "batch"), _must(values.get("operation"), "operation"), _int_of(values.get("draft"), "draft"),
                                   _must(values.get("output"), "output"))
    raise Blocked(f"未知的 cls 子命令：{command}")


def _cls_command(args: list[str]) -> dict:
    from .cls import cls_preview, operation

    values, _ = _parse(args, {"preview": "boolean", "request": "string", "batch": "string", "group": "string", "action": "string", "catalog": "string",
                              "observations": "string", "output": "string"})
    catalog = read_json(_must(values.get("catalog"), "catalog"))
    if values.get("preview"):
        need(values.get("request") and not any(values.get(k) for k in ("batch", "group", "action", "output", "observations")), "preview 只接受 request/catalog")
        return cls_preview(read_json(values["request"]), catalog)
    need(all(values.get(k) for k in ("batch", "group", "action", "observations", "output")), "需本批、组、动作、观察和新输出路径")
    need(values["action"] in ("save_draft", "import_scripts"), "只允许草稿创建和脚本导入")
    directory = os.path.abspath(values["batch"])
    output = os.path.abspath(values["output"])
    need(is_inside(directory, output) and not os.path.exists(output), "操作文件须新建于本批目录内")
    op = operation(directory, values["group"], values["action"], catalog, read_json(values["observations"]))
    write_json_new(output, op)
    return {"status": "operation_ready", "operation": output, "operation_id": op["operation_id"], "native_defaults": op["native_defaults"]}


def _creative_command(command: str, args: list[str], bridge: Any, via: Any) -> dict:
    """The material upload (see creative_online.py): options, rows and plan only read; run uploads what an approved plan says."""
    from . import creative_online as creative
    from .creative import parse_numbers

    if command == "options":
        values, _ = _parse(args, {"find": "string", "creator": "string", "producers": "string"})
        producers_text = _str(values.get("producers"))
        producers = [js.trim(t) for t in re.split(r"[,，、]+", producers_text) if js.trim(t)] if producers_text is not None else None
        need(producers is None or len(producers) > 0, "--producers 需要至少一个名字（逗号分开）")
        creator = _str(values.get("creator"))
        return creative.upload_options(via, find=_str(values.get("find")) or None, creator=js.trim(creator) if creator and js.trim(creator) else None,
                                       producers=producers)
    if command == "files":
        values, positionals = _parse(args, {"check": "boolean", "progress": "string"}, True)
        need(len(positionals) > 0, "creative files 需要文件、文件夹或 zip 的路径")
        need(values.get("check") or "progress" not in values, "--progress 只和 --check 一起用")
        progress = _str(values.get("progress"))
        return creative.list_files(positionals, None, {"bridge": bridge, "progress": progress if progress is not None else True} if values.get("check") else None)
    if command == "rows":
        values, _ = _parse(args, {"numbers": "string", "find": "string", "progress": "string"})
        numbers = parse_numbers(values["numbers"]) if _str(values.get("numbers")) else []
        texts = [js.trim(t) for t in re.split(r"[,，、]+", _str(values.get("find")) or "") if js.trim(t)]
        need(len(texts) <= 20 and all(len(t) <= 50 for t in texts), "--find 最多 20 段文字（逗号分开），每段 50 字以内")
        progress = _str(values.get("progress"))
        return creative.find_rows(bridge, numbers, texts, progress if progress else True)
    if command == "plan":
        values, _ = _parse(args, {"request": "string", "output": "string"})
        return creative.plan_upload(via, bridge, _must(values.get("request"), "request"), _must(values.get("output"), "output"))
    if command == "run":
        return creative.run_upload(via, _must(_parse(args, {"plan": "string"})[0].get("plan"), "plan"))
    if command == "status":
        return creative.upload_status(_must(_parse(args, {"plan": "string"})[0].get("plan"), "plan"))
    if command == "screenshot":
        values, _ = _parse(args, {"output": "string", "rows": "string"})
        return creative.list_picture(via, _must(values.get("output"), "output"), None if "rows" not in values else _int_of(values["rows"], "rows"))
    raise Blocked(f"未知的 creative 子命令：{command}")


def _login_check(bridge: Any, via: Any) -> dict:
    """
    The two logins, checked for real: Feishu by asking Feishu, CLS by looking in the browser that holds its session. Each task starts with
    this, and so does each stage that follows a wait (see SKILL.md), so that a login that has lapsed is found before any work depends on it.
    It changes nothing and takes nothing from the user; what to do about a failure is the caller's business.

    Feishu comes first and the user is asked for one login at a time: while the Feishu login is missing, CLS is not looked at (no browser is
    started) and the only thing to do is the Feishu login. The CLS QR is asked for by the next check, once Feishu is ok.
    """
    from .cls_online import check

    feishu = bridge.check_login()
    if feishu["status"] in ("login_required", "scopes_missing"):
        return {
            "status": "login_required", "feishu": feishu,
            "cls": {"status": "not_checked", "reason": "Feishu login comes first; CLS is checked after it"},
            "todo": ["Feishu: run gof feishu login start (keep its link to yourself), then gof feishu authorize --qr feishu-login.png and send the QR image "
                     "to the user only, to scan with the Feishu phone app. That one scan also covers CLS. Ask for nothing else until Feishu is ok"],
        }
    cls = check(via)
    sides = [feishu["status"], cls["status"]]
    # A block comes first: logging in again does not fix it, so nobody should be asked to scan or authorize before it is reported.
    if all(s == "ok" for s in sides):
        state = "ok"
    elif any(s in ("blocked", "identity_mismatch") for s in sides):
        state = "blocked"
    elif cls["status"] == "login_required" and feishu["status"] == "ok":
        state = "login_required"
    else:
        state = "unknown"
    result: dict[str, Any] = {"status": state, "feishu": feishu, "cls": cls}
    if state == "login_required":
        result["todo"] = ["CLS: run gof cls login --qr cls-login.png and send the QR image to the user only, to scan with the Feishu phone app"]
    return result


def _node_style(error: OSError) -> str:
    """An operating-system error worded the way Node words it (ENOENT: no such file or directory, open '…')."""
    code = errno.errorcode.get(error.errno or 0, "")
    text = {"ENOENT": "no such file or directory", "EEXIST": "file already exists", "EACCES": "permission denied", "EISDIR": "illegal operation on a directory",
            "ENOTDIR": "not a directory", "ENOTEMPTY": "directory not empty", "EPERM": "operation not permitted"}.get(code)
    if text and error.filename is not None and not str(error).startswith(code):
        return f"{code}: {text}, open '{error.filename}'"
    return str(error)


def main(argv: list[str], bridge: Any = None, via: Any = None) -> dict:
    """Runs one command; returns {"result": …, "code": 0|2}."""
    from .cls_adapter import ClsError

    try:
        if bridge is None:
            from .feishu import Bridge

            bridge = Bridge()
        if via is None:
            from .cls_online import real_online

            via = real_online
        group = argv[0] if argv else ""
        command = argv[1] if len(argv) > 1 else ""
        rest = argv[2:]
        if group == "batch":
            result = _batch_command(command, rest)
        elif group == "feishu":
            try:
                result = _feishu_command(command, rest, bridge, via)
            except _Forwarded as forwarded:
                return forwarded.answer
        elif group == "login" and command == "check":
            result = _login_check(bridge, via)
        elif group == "cls":
            result = _cls_online_command(command, rest, via) if command in CLS_ONLINE else _cls_command(argv[1:])
        elif group == "creative":
            result = _creative_command(command, rest, bridge, via)
        else:
            raise Blocked("用法：gof.py <batch|feishu|cls|creative|login> <命令> [参数]")
        status = result.get("status") if isinstance(result, dict) else None
        blocked = (status in ("conflict", "not_applied") or (status == "partial" and group != "creative")
                   or (group == "feishu" and status == "blocked") or (group == "login" and status != "ok")
                   or (group == "creative" and status in ("blocked", "unknown")))
        return {"result": result, "code": 2 if blocked else 0}
    except ClsError as error:
        # After a write of unknown outcome the caller needs to know which draft and operation to read back.
        answer: dict[str, Any] = {"status": "blocked", "reason": error.message}
        if error.kind == "unknown":
            answer["unknown_result"] = True
        if error.draft_id:
            answer["draft_id"] = error.draft_id
        if error.operation_id:
            answer["operation_id"] = error.operation_id
        return {"result": answer, "code": 2}
    except WriteSent as error:
        # After a write went out the caller must read back, never plan again: said in a field, not only in the wording of the reason.
        return {"result": {"status": "blocked", "reason": error.message, "write_sent": True, "batch": error.batch}, "code": 2}
    except Blocked as error:
        return {"result": {"status": "blocked", "reason": error.message}, "code": 2}
    except OSError as error:
        return {"result": {"status": "blocked", "reason": _node_style(error)}, "code": 2}
    except Exception as error:  # noqa: BLE001 - every failure is a refusal with its reason, never a traceback
        return {"result": {"status": "blocked", "reason": str(error) or type(error).__name__}, "code": 2}
