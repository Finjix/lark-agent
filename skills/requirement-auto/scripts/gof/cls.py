"""Offline CLS operation builder and receipt validator. Never stores credentials or sends HTTP (cls.ts)."""

from __future__ import annotations

import base64
import math
import os
import re
from typing import Any, Optional

from . import js
from .request import normalize_request
from .seal import check_seal, reject_secrets, seal
from .util import Blocked, CSV_HEADERS, FIXED_TAG, canonical, is_inside, need, read_json, sha256

TAG_NAMES = [FIXED_TAG, "玩法卖点（单选必选）/展示向/展示向"]


def _unique(items: list[dict], predicate, label: str) -> dict:
    found = [x for x in (items or []) if predicate(x)]
    need(len(found) == 1, f"{label} 不存在或匹配不唯一，需在计划前澄清")
    return found[0]


_BRACKETED = re.compile(r"[（(]([^（）()]+)[）)]")
_DASHED = re.compile(r"[-－—]\s*([^-－—（）()]+)\Z")


def aliases(name: str) -> list[str]:
    """The short names a CLS name carries: in brackets ("…（丁杰）") or after a final dash ("…（个体工商户）-杨燃")."""
    found = [m.group(1) for m in _BRACKETED.finditer(name)]
    dash = _DASHED.search(name)
    if dash:
        found.append(js.trim(dash.group(1)))
    return found


def match_user(users: list[dict], wanted: str, exact: bool, label: str) -> dict:
    """
    The one CLS user a name from the progress sheet means. The progress sheet spells people and companies short, CLS in full, so the exact
    name is tried first, then a short name the CLS name carries, then a CLS name containing it (冯彦蓉 → 冯彦蓉-实习生, 达治 →
    达治（武汉）文化传媒有限公司). A name the request gives the CLS way (`cls.supplier` / `cls.creator`) must match exactly. The first level
    that finds anyone decides, and it must find exactly one.
    """
    levels = [lambda user: user.get("name") == wanted]
    if not exact:
        levels.append(lambda user: wanted in aliases(js.to_str(user.get("name"))))
        levels.append(lambda user: len(wanted) >= 2 and wanted in js.to_str(user.get("name")))
    for level in levels:
        found = [u for u in users if level(u)]
        if found:
            return _unique(found, lambda _: True, label)
    raise Blocked(f"{label} 不存在或匹配不唯一，需在计划前澄清")


def _date_value(value: Any) -> str:
    need(value == "" or (isinstance(value, str) and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value)), "日期需先明确为 YYYY-MM-DD；不猜测日期")
    return value


_AMOUNT = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")


def amount(value: Any) -> Optional[float]:
    """A decimal amount as a number, or None when the text is not a plain decimal number."""
    text = js.trim(js.to_str(value))
    if not _AMOUNT.fullmatch(text):
        return None
    n = float(text)
    return js.as_int(n) if math.isfinite(n) else None


#: CLS's supplier for a requirement whose 设计师供应商 is still open (and the 制作人 of its materials).
PENDING_SUPPLIER = "待定供应商"


def catalog_choices(catalog: dict) -> dict:
    """What a request can choose from in CLS, by the names it uses: GOFCN directions, creative task types, active suppliers and people."""
    game = next((g for g in catalog["games"] if g.get("code") == "GOFCN"), None)
    active = [u for u in catalog["users"] if u.get("status") == 1]
    return {
        "directions": [d.get("name") for d in catalog["directions"] if game and d.get("game_id") == game.get("id")],
        "task_types": [t.get("key") for t in catalog["types"] if t.get("type") == "creative"],
        "suppliers": [u.get("name") for u in active if "outsource" in u.get("roles", [])],
        "people": [u.get("name") for u in active if "outsource" not in u.get("roles", [])],
    }


def requirement_name(names: list[str]) -> str:
    """CLS requirement name: the group's first and last material number, e.g. GOFCN67294-GOFCN67299 (one item: GOFCN67294)."""
    numbers = []
    for n in names:
        m = re.match(r"GOFCN([0-9]+)_", n)
        numbers.append(m.group(1) if m else None)
    need(len(numbers) > 0 and all(n is not None for n in numbers), "素材名称缺少 GOFCN 编号，无法生成需求名称")
    first = f"GOFCN{numbers[0]}"
    last = f"GOFCN{numbers[-1]}"
    return first if first == last else f"{first}-{last}"


def _present(value: Any) -> bool:
    """A value JavaScript's `||` / `if` would take as given (not '', 0, null or undefined)."""
    return not (value is None or value is js.UNDEFINED or value == "" or value is False or (js.is_num(value) and (value == 0 or math.isnan(value))))


def build_payload(group: dict, catalog: dict, names: Optional[list[str]] = None) -> tuple[dict, int, dict, dict]:
    """The payload, the creator's ID, the native defaults taken, and the CLS names everything was matched to (for the plan)."""
    cls = group["cls"]
    need(len(cls) > 0, "全流程 CLS 字段未收齐")
    game = _unique(catalog.get("games"), lambda x: x.get("code") == "GOFCN", "GOFCN 项目")
    user_games = (catalog.get("account") or {}).get("game_ids")
    need(isinstance(user_games, list) and game.get("id") in user_games, "当前账号未证明有 GOFCN 权限")
    direction = _unique(catalog.get("directions"), lambda x: x.get("game_id") == game.get("id") and x.get("name") == cls.get("direction"), "创意方向")
    suppliers = [x for x in catalog["users"] if x.get("status") == 1 and "outsource" in x.get("roles", [])]
    # A requirement without a 设计师供应商 goes to CLS's own placeholder supplier.
    supplier_name = cls["supplier"] if "supplier" in cls and cls["supplier"] is not None else (group.get("supplier") or PENDING_SUPPLIER)
    supplier = match_user(suppliers, js.to_str(supplier_name), "supplier" in cls or not group.get("supplier"), "供应商")
    creator_name = cls["creator"] if "creator" in cls and cls["creator"] is not None else group.get("reviewer")
    creator = match_user([x for x in catalog["users"] if x.get("status") == 1], js.to_str(creator_name), "creator" in cls, "创意人")
    task = _unique(catalog.get("types"), lambda x: x.get("type") == "creative" and x.get("key") == cls.get("task_type"), "任务类型")
    payload: dict[str, Any] = {
        "game_id": game.get("id"), "direct_id": direction.get("id"), "asset_data": "", "name": requirement_name(names) if names else "GOFCN<首号>-GOFCN<末号>",
        "type": task.get("value"), "company": "", "departments": [], "marketing_user_id": "", "third_user_id": supplier.get("id"), "currency": "cny",
        "price": 0, "price_reason": "", "expect_end_date": "", "storyboard_end_date": "", "storyboard_attaches": [], "draft_end_date": "",
        "draft_attaches": [], "film_end_date": "", "film_attaches": [], "status": -2,
    }
    if _present(cls.get("currency")):
        payload["currency"] = _unique(catalog.get("currencies"), lambda x: cls["currency"] == x.get("key") or cls["currency"] == x.get("value"), "货币").get("value")
    quotation = cls.get("quotation")
    if (quotation if quotation is not None else "") != "":
        text = js.trim(js.to_str(quotation))
        if re.fullmatch(r"[+-]?(?:nan|snan|inf|infinity)", text, re.IGNORECASE):
            raise Blocked("报价无效")
        price = amount(quotation)
        need(price is not None, "报价必须是已确认的金额")
        need(price >= 0, "报价无效")
        payload["price"] = price
    for key, options, field in (("subject", "companies", "company"), ("department", "departments", "departments")):
        if _present(cls.get(key)):
            value = _unique(catalog.get(options), lambda x: cls[key] == x.get("key") or cls[key] == x.get("value"), key).get("value")
            payload[field] = [value] if key == "department" else value
    if _present(cls.get("market_contact")):
        payload["marketing_user_id"] = _unique(catalog.get("users"), lambda x: x.get("status") == 1 and x.get("name") == cls["market_contact"], "市场媒介").get("id")
    for key, field in (("expected_completion", "expect_end_date"), ("storyboard_time", "storyboard_end_date"),
                       ("first_draft_time", "draft_end_date"), ("final_cut_time", "film_end_date")):
        payload[field] = _date_value(cls[key] if cls.get(key) is not None else "")
    defaults: dict[str, Any] = {}
    for key, source in (("currency", "currency"), ("price", "quotation")):
        if (cls[source] if cls.get(source) is not None else "") == "":
            defaults[key] = payload[key]
    return payload, creator.get("id"), defaults, {"direction": direction.get("name"), "task_type": task.get("key"), "supplier": supplier.get("name"),
                                                  "creator": creator.get("name")}


def parse_csv(text: str) -> list[list[str]]:
    """RFC 4180 reading of the generated CSV."""
    rows: list[list[str]] = []
    row: list[str] = []
    field = ""
    quoted = False
    i = 0
    length = len(text)
    while i < length:
        ch = text[i]
        if quoted:
            if ch == '"':
                if i + 1 < length and text[i + 1] == '"':
                    field += '"'
                    i += 1
                else:
                    quoted = False
            else:
                field += ch
        elif ch == '"':
            quoted = True
        elif ch == ",":
            row.append(field)
            field = ""
        elif ch in ("\r", "\n"):
            if ch == "\r" and i + 1 < length and text[i + 1] == "\n":
                i += 1
            row.append(field)
            rows.append(row)
            row = []
            field = ""
        else:
            field += ch
        i += 1
    if field != "" or row:
        row.append(field)
        rows.append(row)
    return rows


def _csv_data(directory: str, group: dict) -> dict:
    with open(os.path.join(directory, group["csv"]), "rb") as handle:
        raw = handle.read()
    need(raw[:3] == b"\xef\xbb\xbf" and b"\r\n" in raw, "CSV 编码或换行不符")
    rows = parse_csv(raw.decode("utf-8", "replace")[1:])
    expected = [CSV_HEADERS, *[[name, group["request"]["script_url"] if i == 0 else "同上", FIXED_TAG] for i, name in enumerate(group["names"])]]
    need(canonical(rows) == canonical(expected), "CSV 与获批组不一致，禁止导入")
    return {"filename": group["csv"], "sha256": sha256(raw), "base64": base64.b64encode(raw).decode("ascii")}


def operation(directory: str, group_id: str, action: str, catalog: dict, observations: dict) -> dict:
    from . import batch

    batch_path, manifest = batch.load_batch(directory)
    reject_secrets(catalog)
    need(action in ("save_draft", "import_scripts"), "只允许草稿创建和脚本导入")
    # A ticket attached after the append (numbers reserved first, requirement written later) counts as the group's own.
    group = batch.ticket_group(manifest["plan"], batch.events(batch_path), group_id)
    need(group.get("csv"), batch.NO_TICKET)
    need(catalog.get("can_create") is True and (catalog.get("account") or {}).get("status") == 1, "未证明当前 CLS 登录及建单权限")
    batch.cls_check(batch_path, group_id, action, observations)
    payload, creator_id, defaults, _ = build_payload(group["request"], catalog, group["names"])
    expected = {"names": group["names"], "quantity": group["request"]["quantity"], "creator_id": creator_id, "link": group["request"]["script_url"],
                "tag_names": sorted(TAG_NAMES, key=js.utf16_key)}
    result: dict[str, Any] = {"schema": 1, "action": action, "plan_id": manifest["plan"]["plan_id"], "group": group_id,
                              "account_id": catalog["account"].get("id"), "payload": payload, "native_defaults": defaults, "expected": expected}
    if action == "import_scripts":
        saved = [e for e in batch.events(batch_path) if e["group"] == group_id and e["step"] == "draft_saved"]
        need(len(saved) == 1 and saved[0]["evidence"].get("draft_id"), "原批次没有可核对的草稿 ID；先核对旧工单")
        result["draft_id"] = saved[0]["evidence"]["draft_id"]
        result["csv"] = _csv_data(batch_path, group)
    return seal(result, "operation_id")


ALLOWED_POST = ["/api/workflow/script/save", "/api/workflow/sub-script/multiple-add"]


def validate_receipt(evidence: dict) -> dict:
    receipt_file = evidence.get("receipt")
    receipt = read_json(js.to_str(receipt_file) if receipt_file is not None else "")
    reject_secrets(receipt)
    need(receipt.get("schema") == 1 and receipt.get("transport") == "cls_api" and receipt.get("status") == "verified" and receipt.get("unpublished") is True,
         "缺少已回读验证的 CLS 接口记录")
    parent = receipt.get("parent") or {}
    need(js.is_int(parent.get("id")) and parent["id"] > 0 and parent.get("status") == -2, "CLS 需求未证明为草稿")
    need(evidence.get("draft_id") == parent["id"] and evidence.get("url") == f"https://creative.centurygames.cn/requirement/{js.to_str(parent['id'])}/edit-outer",
         "草稿入口与接口 ID 不匹配")
    if evidence.get("requirement_number") is not None:
        need(evidence["requirement_number"] == parent.get("number"), "需求编号不是实际接口返回值")
    calls = receipt.get("calls") or []
    read_path = f"/api/workflow/script/{js.to_str(parent['id'])}"
    for call in calls:
        allowed = (call.get("method") == "POST" and call.get("path") in ALLOWED_POST) or (call.get("method") == "GET" and call.get("path") == read_path)
        status = call.get("http_status")
        need(allowed and call.get("business_code") == 0 and js.is_num(status) and 200 <= status < 300, "接口调用记录包含失败或未许可动作")
    need(len(calls) > 0, "接口记录没有回读证据")
    last = calls[-1]
    need(last.get("method") == "GET" and last.get("path") == read_path, "接口记录必须以完整草稿回读结束")
    need(len([c for c in calls if c.get("method") == "POST"]) <= 1, "接口记录包含重复写请求")
    return receipt


def _validate_parent(parent: dict, payload: dict) -> None:
    for key, expected in payload.items():
        if key == "asset_data":
            continue  # This field belongs to the separate asset object; always blank in this skill.
        actual = parent.get(key, js.UNDEFINED)
        if key == "price":
            a = amount(actual)
            b = amount(expected)
            equal = a is not None and b is not None and a == b
        elif expected == "" and (actual is None or actual is js.UNDEFINED):
            equal = True  # Backend persists unfilled dates/optional IDs as null.
        else:
            equal = canonical(actual) == canonical(expected)
        need(equal, f"CLS 回读字段不符：{key}")


def validate_batch_receipt(directory: str, group_id: str, evidence: dict, step: str) -> None:
    from . import batch

    batch_path, manifest = batch.load_batch(directory)
    receipt_path = os.path.abspath(js.to_str(evidence.get("receipt")) if evidence.get("receipt") is not None else "")
    operation_path = os.path.abspath(js.to_str(evidence.get("operation")) if evidence.get("operation") is not None else "")
    need(is_inside(batch_path, receipt_path) and is_inside(batch_path, operation_path), "CLS 记录必须位于本批目录")
    op = read_json(operation_path)
    check_seal(op, "operation_id")
    need(op.get("plan_id") == manifest["plan"]["plan_id"] and op.get("group") == group_id, "接口记录不属于本批/本组")
    receipt = validate_receipt(evidence)
    need(receipt["parent"].get("user_id") == op.get("account_id") and receipt["parent"].get("for_internal") in (0, False)
         and not (receipt["parent"].get("for_internal") is True), "草稿账号/外包归属不符")
    need(receipt.get("operation_id") == op.get("operation_id") and receipt.get("action") == op.get("action"), "接口请求摘要不匹配")
    records = batch.events(batch_path)
    attempts = [e for e in records if e["group"] == group_id and e["step"] == "cls_api_attempt" and e["evidence"].get("operation_id") == op.get("operation_id")]
    need(len(attempts) == 1, "必须先记录本次接口尝试；不可重复请求")
    group = batch.ticket_group(manifest["plan"], records, group_id)
    need(op["payload"].get("name") == requirement_name(group["names"]), "需求名称与获批需求不符")
    need(canonical(op["expected"].get("names")) == canonical(group["names"]) and js.is_num(op["expected"].get("creator_id")) and op["expected"]["creator_id"] > 0
         and op["expected"].get("link") == group["request"]["script_url"], "请求与获批需求不匹配")
    _validate_parent(receipt["parent"], op["payload"])
    scripts = receipt["parent"].get("sub_scripts") or []
    if step == "draft_saved":
        need(op.get("action") == "save_draft" and len(scripts) == 0, "新建草稿存在计划外脚本")
    else:
        need(op.get("action") == "import_scripts" and receipt["parent"].get("id") == op.get("draft_id") and len(scripts) == len(group["names"]), "导入数量/草稿不符")
        need(len({canonical(s.get("id")) for s in scripts}) == len(scripts), "返回脚本 ID 重复")
        for index, script in enumerate(scripts):
            need(script.get("name") == group["names"][index] and script.get("user_id") == op["expected"]["creator_id"]
                 and script.get("data") == (op["expected"]["link"] if index == 0 else "同上")
                 and any(t.get("full_name") in TAG_NAMES for t in (script.get("tags") or [])), "脚本名称/创意人/链接/标签不符")


def cls_preview(request_input: Any, catalog: dict) -> dict:
    reject_secrets(catalog)
    need(catalog.get("can_create") is True, "没有建单权限")
    request = normalize_request(request_input)
    groups = []
    for offset, group in enumerate(request["groups"]):
        payload, creator, defaults, matched = build_payload(group, catalog)
        groups.append({"group": str(offset + 1).zfill(2), "payload": payload, "creator_id": creator, "native_defaults": defaults, "cls_names": matched})
    return {"status": "cls_plan", "groups": groups}
