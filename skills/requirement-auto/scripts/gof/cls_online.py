"""The commands that go online: `gof cls login | session | catalog | observe | run | read-draft | verify` (cls-online.ts).

Each one gets a session by itself (the login kept in this user's browser profile), does its work over HTTP with the token it saw, and
forgets the token when it ends. The batch rules are unchanged: a write needs its recorded attempt first, and the result of a write is only
ever read back, never repeated.
"""

from __future__ import annotations

import os
import re
from typing import Any, Optional

from . import js
from .cls_adapter import ClsAdapter, ClsError
from .cls_guard import CORRUPT_PIN, Online, guard_online, pin_state, write_qr
from .seal import check_seal, reject_secrets
from .util import Blocked, is_inside, need, read_json, write_json_new

#: The one consent page that is approved after a scan: the CLS application's own, for the account this login is pinned to.
CONSENT_APP = "dsc_admin"


def _consent_options() -> Any:
    from .cls_session import ConsentOptions, cls_account_file

    pin = pin_state(cls_account_file())
    if pin["state"] == "corrupt":
        raise Blocked(CORRUPT_PIN)
    if pin["state"] == "none":
        return ConsentOptions(app=CONSENT_APP)
    return ConsentOptions(app=CONSENT_APP, account=pin["pin"]["name"]) if pin["pin"]["name"] else ConsentOptions(app=CONSENT_APP, unnamed_pin=True)


class _RawOnline(Online):
    def session(self) -> dict:
        from .cls_session import open_session

        return open_session(consent=_consent_options())

    def capture(self, wait_ms: int) -> dict:
        from .cls_session import capture_authorization, cls_profile_dir

        return capture_authorization(cls_profile_dir(), wait_ms, consent=_consent_options())

    def login_qr(self, file: str) -> dict:  # type: ignore[override]
        from .chromium import attach_browser
        from .cls_session import capture_login_qr, cls_profile_dir, cls_qr_file

        result = capture_login_qr(cls_profile_dir())
        if result["state"] != "qr":
            return result
        try:
            return {"state": "qr", "file": write_qr(cls_qr_file(), file, result["png"])}
        except BaseException:
            # No picture, no record: nobody would ever close the browser that is now waiting for a scan nobody can make.
            try:
                browser = attach_browser(cls_profile_dir())
                if browser:
                    browser.close()
            except Exception:
                pass
            raise


def _guarded() -> Online:
    """Built per call: the user's directories come from the environment of the process that runs the command."""
    from .cls_session import cls_account_file, cls_profile_dir, cls_qr_file

    return guard_online(_RawOnline(), cls_account_file(), qr_file=cls_qr_file(), profile_dir=cls_profile_dir())


class _RealOnline(Online):
    def session(self) -> dict:
        return _guarded().session()

    def capture(self, wait_ms: int) -> dict:
        return _guarded().capture(wait_ms)

    def expected_account(self) -> Optional[dict]:
        return _guarded().expected_account()

    def login_qr(self, file: str) -> dict:  # type: ignore[override]
        return _guarded().login_qr(file)  # type: ignore[misc]


real_online: Online = _RealOnline()


def _adapter_for(session: dict, before_write: Any = None) -> ClsAdapter:
    return ClsAdapter(session["origin"], session["authorization"], before_write=before_write)


def _account(a: dict) -> dict:
    return {"id": a["id"], "name": a["name"]}


def login_with_qr(online: Online, file: str) -> dict:
    """
    The login: the QR code of the sign-in page goes to a picture file for the user to scan with their phone, and the hidden browser that
    shows it keeps running. After the scan, `cls session` (or `cls login`) takes the login from that browser.
    """
    from .cls_session import unreachable_reason

    need(re.search(r"\.png\Z", file, re.IGNORECASE), "二维码文件需要是 .png")
    quiet = online.capture(12_000)
    if quiet["state"] == "ok":
        return {"status": "logged_in", "account": _account(quiet["account"])}
    if quiet["state"] == "consent_refused":
        raise Blocked(quiet["reason"])
    # A code scanned while CLS cannot be reached would not log anybody in.
    if quiet["state"] == "unreachable":
        raise Blocked(unreachable_reason(quiet["reason"]))
    need(online.login_qr is not None, "这个环境不能用二维码登录")
    outcome = online.login_qr(os.path.abspath(file))  # type: ignore[misc]
    if outcome["state"] == "not_login_page":
        return {"status": "no_qr", "where": outcome["where"],
                "message": "页面没有停在登录二维码上（可能已经登录，或正在跳转）；先运行 gof cls session 看一眼，仍未登录再重新运行本命令"}
    expected = online.expected_account()
    result: dict[str, Any] = {"status": "qr_ready", "qr_file": outcome["file"]}
    if expected:
        result["expected_account"] = expected["name"]
    hint = f"（账号应为“{expected['name']}”）" if expected else ""
    result["message"] = f"二维码已生成。把它发给用户，请其用飞书手机端扫码并在手机上确认{hint}；二维码几分钟后失效，失效就重新运行本命令。用户说已确认后运行 gof cls session"
    return result


def check(online: Online) -> dict:
    """
    Is the CLS login good right now? One look in a hidden browser, with the real session behind it: ok | login_required | blocked, or
    unknown when CLS could not be asked (the login is not lost, and no code is to be scanned for it).
    """
    from .cls_session import unreachable_reason

    try:
        seen = online.capture(25_000)
        if seen["state"] == "ok":
            return {"status": "ok", "account": _account(seen["account"])}
        if seen["state"] == "consent_refused":
            return {"status": "blocked", "reason": seen["reason"]}
        if seen["state"] == "unreachable":
            return {"status": "unknown", "reason": unreachable_reason(seen["reason"])}
        return {"status": "login_required"}
    except Blocked as error:
        return {"status": "blocked", "reason": error.message}


def session_status(online: Online) -> dict:
    session = online.session()
    return {"status": "logged_in", "account": _account(session["account"])}


def catalog(online: Online, output: str) -> dict:
    from .cls import catalog_choices

    file = os.path.abspath(output)
    need(not os.path.exists(file), "目录文件已存在，不覆盖")
    session = online.session()
    result = _adapter_for(session).read_catalog()
    reject_secrets(result)
    write_json_new(file, result)
    return {"status": "catalog_saved", "catalog": file, "account": _account(session["account"]), "can_create": result["can_create"],
            "can_import": result["can_import"], "choices": catalog_choices(result)}


def observe(online: Online, action: str, output: str, draft_id: Optional[int] = None) -> dict:
    """
    The conditions `cls-check` asks for, taken from what CLS says right now instead of from a person's report. `required_complete` is true
    because the operation file that follows is only built from a request with every required field; `import_would_publish` is false because
    the import endpoint only adds scripts to the draft (publishing is a different, forbidden call) and the read-back after the import fails
    if the draft is no longer unpublished.
    """
    need(action in ("save_draft", "import_scripts"), "只允许草稿创建和脚本导入")
    file = os.path.abspath(output)
    need(not os.path.exists(file), "观察文件已存在，不覆盖")
    session = online.session()
    api = _adapter_for(session)
    current = api.read_catalog()
    if action == "save_draft":
        observations = {"logged_in": True, "can_create": current["can_create"], "unpublished": True, "save_result": "none", "required_complete": True}
    else:
        need(js.is_safe_int(draft_id) and draft_id > 0, "导入需要 --draft 草稿 ID")
        parent = api.read_draft(draft_id)
        observations = {
            "logged_in": True, "can_create": current["can_create"], "unpublished": parent.get("status") == -2, "draft_saved": True, "save_result": "ok",
            "import_enabled": current["can_import"] is True and (parent.get("is_edit") is True or parent.get("is_edit") == 1) and len(parent["sub_scripts"]) == 0
            and parent.get("user_id") == session["account"]["id"],
            "import_would_publish": False,
        }
    write_json_new(file, observations)
    return {"status": "observations_saved", "observations": file, **observations}


def _load_operation(batch_dir: str, operation_file: str) -> tuple[str, dict, dict, str]:
    from .batch import load_batch

    batch_path, manifest = load_batch(batch_dir)
    operation_path = os.path.abspath(operation_file)
    need(is_inside(batch_path, operation_path), "操作文件必须位于本批目录")
    op = read_json(operation_path)
    check_seal(op, "operation_id")
    need(op.get("plan_id") == manifest["plan"]["plan_id"], "操作文件不属于本批")
    return batch_path, manifest, op, operation_path


def run(online: Online, batch_dir: str, operation_file: str, output: str) -> dict:
    """
    Sends one sealed operation. The attempt has to be recorded in the batch first, and a marker file is created just before the write goes
    out, so a second run of the same operation (in this session or any other) is refused: what a write did is found out by reading back.
    """
    from .batch import events

    batch_path, _, op, _ = _load_operation(batch_dir, operation_file)
    receipt_path = os.path.abspath(output)
    need(is_inside(batch_path, receipt_path) and not os.path.exists(receipt_path), "接口记录须新建于本批目录内")
    os.makedirs(os.path.dirname(receipt_path), exist_ok=True)  # before the write: a failure to save the receipt must not come after it
    attempts = [e for e in events(batch_path) if e.get("group") == op.get("group") and e["step"] == "cls_api_attempt"
                and e["evidence"].get("operation_id") == op.get("operation_id")]
    need(len(attempts) == 1, "先用 batch record --step cls_api_attempt 记录本次尝试")
    marker = os.path.join(batch_path, f"cls-sent-{js.to_str(op.get('operation_id'))[:16]}.json")
    session = online.session()
    need(session["account"]["id"] == op.get("account_id"), "当前登录的 CLS 账号与操作文件不是同一个")

    def before_write(operation: dict) -> None:
        try:
            write_json_new(marker, {"operation_id": operation["operation_id"], "action": operation["action"], "at": js.iso()})
        except Exception:
            raise ClsError("本操作已经发出过写请求；先用 read-draft 回读核对，不重发", "unknown") from None

    api = _adapter_for(session, before_write)
    receipt = api.create_draft(op) if op.get("action") == "save_draft" else api.import_scripts(op)
    try:
        write_json_new(receipt_path, receipt)
    except Exception as error:
        lost = ClsError(f"写请求已成功，但接口记录没能保存：{error}；先用 cls read-draft / cls verify 回读核对，不重发", "unknown")
        lost.draft_id = receipt["parent"]["id"]
        lost.operation_id = op.get("operation_id")
        raise lost from None
    parent = receipt["parent"]
    return {"status": "receipt_saved", "receipt": receipt_path, "action": op["action"], "draft_id": parent["id"],
            "requirement_number": parent.get("number"), "url": f"{session['origin']}/requirement/{js.to_str(parent['id'])}/edit-outer",
            "scripts": len(parent["sub_scripts"])}


def read_draft(online: Online, draft_id: int) -> dict:
    need(js.is_safe_int(draft_id) and draft_id > 0, "需要 --id 草稿 ID")
    return {"status": "draft", "draft": _adapter_for(online.session()).read_draft(draft_id)}


def verify_draft(online: Online, batch_dir: str, operation_file: str, draft_id: int, output: str) -> dict:
    """A read-only receipt for a draft that is already there, for recovering after a write whose outcome was not known."""
    batch_path, _, op, _ = _load_operation(batch_dir, operation_file)
    receipt_path = os.path.abspath(output)
    need(is_inside(batch_path, receipt_path) and not os.path.exists(receipt_path), "接口记录须新建于本批目录内")
    need(js.is_safe_int(draft_id) and draft_id > 0, "需要 --draft 草稿 ID")
    session = online.session()
    need(session["account"]["id"] == op.get("account_id"), "当前登录的 CLS 账号与操作文件不是同一个")
    receipt = _adapter_for(session).verify(op, draft_id)
    write_json_new(receipt_path, receipt)
    return {"status": "receipt_saved", "receipt": receipt_path, "action": op["action"], "draft_id": receipt["parent"]["id"],
            "requirement_number": receipt["parent"].get("number")}
