"""The request the agent builds from the requirement documents: groups of rows, checked and normalized (request.ts)."""

from __future__ import annotations

import re
from typing import Any

from . import js
from .util import cell_text, feishu_doc_path, is_object, need

REQUIRED_TEXT = ["material_name", "keyword", "creative_form", "supplier", "reviewer", "script_url"]
OPTIONAL_FIELDS = ["creative_expression", "creative_batch", "head_reference", "creative_reference"]
CLS_REQUIRED = ["direction", "task_type"]
# `supplier` and `creator` name the same supplier and reviewer the way CLS spells them, when the program cannot match them by itself.
CLS_OPTIONAL = ["subject", "department", "market_contact", "currency", "quotation",
                "expected_completion", "storyboard_time", "first_draft_time", "final_cut_time", "supplier", "creator"]
#: 提炼词 unless the request names one.
DEFAULT_KEYWORD = "关键词"
#: The 脚本内容 of a requirement that has no document (the user said there is none): the CSV says so instead of linking one.
NO_SCRIPT = "无"
#: The progress fields that make up a material name and may differ from row to row within one group.
ITEM_FIELDS = ["primary_tag", "secondary_tag", "creative_form", "keyword"]
ITEM_KEYS = ["quantity", "material_name", "topic", *ITEM_FIELDS]

#: The name a request gives without its number: a number in front is an old one (a sample, never a new ID), and so is a number not given
#: yet (`GOFCNxxxxx_…`, how a file is named before its row exists).
PENDING_NUMBER = re.compile("^GOFCN(?:[0-9]+|[xX×*＊?？#]*)_")

_MISSING = object()


def _pick(obj: dict, key: str, fallback: Any) -> Any:
    return obj[key] if key in obj else fallback


def _subset(keys: list[str], allowed: list[str]) -> bool:
    allowed_set = set(allowed)
    return all(k in allowed_set for k in keys)


def items_of(group: dict) -> list[dict]:
    """The rows of a group in order: its items, or the group itself when all its rows are alike."""
    if isinstance(group.get("items"), list):
        return group["items"]
    item = {"quantity": group.get("quantity", js.UNDEFINED), "material_name": group.get("material_name", js.UNDEFINED),
            "topic": group.get("topic", js.UNDEFINED), "primary_tag": group.get("primary_tag", js.UNDEFINED),
            "secondary_tag": group.get("secondary_tag", js.UNDEFINED), "creative_form": group.get("creative_form", js.UNDEFINED),
            "keyword": group.get("keyword", js.UNDEFINED)}
    return [item]


def field_values(groups: list[dict], field: str) -> list[str]:
    """Every value a field takes in the request, in order of first use."""
    values: list[Any] = []
    for g in groups:
        if field in ITEM_FIELDS:
            values.extend(item.get(field) for item in items_of(g))
        else:
            values.append(g.get(field))
    out: list[str] = []
    for v in values:
        if isinstance(v, str) and v != "" and v not in out:
            out.append(v)
    return out


def _item(raw: dict, inherited: dict, where: str, adopted: bool) -> dict:
    """
    One run of rows. `raw` holds the item's own keys and `inherited` the group's defaults for the tags, form and keyword. The tags are
    given; an older request that gives only a name (`material_name`, 一级_二级_创意形式_提炼词) has them read from it. A given name is the
    one the rows must get: the plan checks it against what the naming formula makes of the row. Rows taken over from the sheet (`adopted`)
    have their names already, so the rules for making a new name are not theirs to meet.
    """
    q = raw.get("quantity", js.UNDEFINED)
    need(js.is_int(q) and q > 0, "quantity 必须是正整数")
    q = js.as_int(q)

    def value(key: str, fallback: Any) -> Any:
        return _pick(raw, key, _pick(inherited, key, fallback))

    # 创意形式 is filled only when the request states it; an empty one stays empty and drops out of the name.
    form = js.trim(cell_text(value("creative_form", ""), "creative_form"))
    keyword = cell_text(value("keyword", DEFAULT_KEYWORD), "keyword")
    need(js.trim(keyword) != "", "缺少 keyword")
    # The name is cut into its parts at _ and the formula folds "__" into one: a 提炼词 may hold _, but not at its ends or twice in a row.
    need(adopted or not re.search(r"^_|_$|__", keyword), f"{where}的提炼词“{keyword}”不能以 _ 开头或结尾，也不能有连续的 _")
    # It ends up in the 素材名称, which is also the creative's name in CLS, where / and \ cannot be: such a row could never be uploaded.
    need(adopted or not re.search(r"[\\/]", keyword), f"提炼词“{keyword}”不能含 / 或 \\（素材名称也是 CLS 里的创意名称，那里不能有）：换成别的写法")
    result: dict[str, Any] = {"quantity": q, "creative_form": form, "keyword": keyword}
    given = value("material_name", _MISSING)
    tagged = all(isinstance(value(key, ""), str) and js.trim(value(key, "")) != "" for key in ("primary_tag", "secondary_tag"))
    if given is not _MISSING and given is not js.UNDEFINED:
        need(isinstance(given, str) and js.trim(given) != "", "缺少 material_name")
        result["material_name"] = PENDING_NUMBER.sub("", js.trim(cell_text(given, "material_name")), count=1)
        need(result["material_name"] != "" and not re.search(r"[\\/]", result["material_name"]), f"素材名称“{given}”不能为空，也不能含 / 或 \\")
    else:
        given = _MISSING
    if given is not _MISSING and not tagged:
        suffix = f"_{form}_{keyword}" if form != "" else f"_{keyword}"
        name = result["material_name"]
        need(name.endswith(suffix), "素材名称与创意形式/提炼词不一致，请澄清")
        topic = name[:len(name) - len(suffix)]
        result["topic"] = cell_text(_pick(raw, "topic", topic), "topic")
        need(result["topic"] == topic, "题材与素材名称不一致")
        cut = topic.find("_")
        inferred = [topic] if cut < 0 else [topic[:cut], topic[cut + 1:]]
        for i, key in enumerate(("primary_tag", "secondary_tag")):
            result[key] = cell_text(value(key, inferred[i] if len(inferred) == 2 else ""), key)
            need(js.trim(result[key]) != "", "无法从素材名称确定一二级标签，需先根据名称推理")
    else:
        for key in ("primary_tag", "secondary_tag"):
            tag = value(key, "")
            need(isinstance(tag, str) and js.trim(tag) != "", f"{where}缺少一级/二级标签")
            result[key] = cell_text(tag, key)
        result["topic"] = f"{result['primary_tag']}_{result['secondary_tag']}"
        if "topic" in raw:
            need(raw["topic"] == result["topic"], "题材与素材名称不一致")
    # A 素材名称 is cut into its parts at _: a tag or form with one of its own would make a name that can be read two ways (no option has one).
    for key, label in (("primary_tag", "一级标签"), ("secondary_tag", "二级标签"), ("creative_form", "创意形式")):
        need(adopted or "_" not in result[key], f"{where}的{label}“{result[key]}”不能含 _：素材名称按 _ 分段，标签和创意形式各占一段，其余写进提炼词")
    return result


def normalize_request(request: Any, adopted: bool = False) -> dict:
    """`adopted`: rows taken over as the sheet has them (`gof batch adopt`): named already, they need not meet the rules for a new name."""
    need(is_object(request) and _subset(list(request.keys()), ["worksheet", "groups"]), "请求有未知字段/目标覆盖")
    groups = request.get("groups")
    need(isinstance(groups, list) and len(groups) > 0, "需要至少一个供应商/题材组")
    allowed = [*REQUIRED_TEXT, *OPTIONAL_FIELDS, "quantity", "topic", "primary_tag", "secondary_tag", "creator", "client", "brief", "cls", "items", "label"]
    normalized: list[dict] = []
    for offset, raw in enumerate(groups):
        index = offset + 1
        need(is_object(raw) and _subset(list(raw.keys()), allowed), f"第 {index} 组有未知字段")
        g: dict[str, Any] = {}
        if "items" in raw:
            entries = raw["items"]
            need(isinstance(entries, list) and len(entries) > 0, f"第 {index} 组的 items 需要至少一项")
            need(not any(k in raw for k in ("material_name", "topic")), f"第 {index} 组已按 items 分行，素材名称写在各项里")
            items = []
            for i, entry in enumerate(entries):
                need(is_object(entry) and _subset(list(entry.keys()), ITEM_KEYS), f"第 {index} 组第 {i + 1} 项有未知字段")
                items.append(_item(entry, raw, f"第 {index} 组第 {i + 1} 项", adopted))
            total = sum(it["quantity"] for it in items)
            # A normalized request is normalized again when a batch is prepared: its total is the items' sum.
            need("quantity" not in raw or raw["quantity"] == total, f"第 {index} 组的 quantity 与各项数量之和不一致")
            # A group of one kind of row looks the same however it was written.
            if len(items) == 1:
                g.update(items[0])
            else:
                g.update({"quantity": total, "items": items})
        else:
            g.update(_item(raw, {}, f"第 {index} 组", adopted))
        # The script link goes into the CSV and the CLS ticket. Rows registered for finished materials (the upload skill) get neither:
        # without `cls` they may go without it ('' = none).
        ticket = is_object(raw.get("cls")) and len(raw["cls"]) > 0
        missing = [label for key, label, needed in (("reviewer", "审核人", True), ("script_url", "脚本内容链接", ticket)) if needed and key not in raw]
        need(len(missing) == 0, f"第 {index} 组缺少需求字段：{'、'.join(missing)}")
        need(isinstance(raw.get("reviewer"), str) and js.trim(raw["reviewer"]) != "", "缺少 reviewer")
        g["reviewer"] = cell_text(raw["reviewer"], "reviewer")
        link = _pick(raw, "script_url", "")
        need(isinstance(link, str) and (js.trim(link) != "" or not ticket), "缺少 script_url")
        g["script_url"] = NO_SCRIPT if js.trim(link) == NO_SCRIPT else cell_text(link, "script_url")
        # Finished materials named with their number (GOFCN67680_…) have most likely been registered already: their rows are verified
        # (gof creative files --check), never appended a second time. A new piece is named GOFCNxxxxx_…, whatever its file carries.
        if not ticket and g["script_url"] == "":
            sources = [raw, *(raw["items"] if isinstance(raw.get("items"), list) else [])]
            numbered = [x.get("material_name") for x in sources if isinstance(x, dict)]
            numbered = [n for n in numbered if isinstance(n, str) and re.match(r"GOFCN[0-9]+_", js.trim(n), re.IGNORECASE)]
            need(len(numbered) == 0, f"第 {index} 组的素材名称已带编号（{'、'.join(numbered)}）：带编号的素材多半已在进度表登记，先用 gof creative files --check 核验，已登记的只上传、不追加；确是新素材的，素材名称写成 GOFCNxxxxx_…")
        # 设计师供应商 may be left open only for a requirement whose supplier is not decided yet: the sheet's cell stays empty and CLS takes
        # its 待定供应商 (cls.py). Finished materials (no script link) were made by someone, who is also their 制作人.
        g["supplier"] = js.trim(cell_text(_pick(raw, "supplier", ""), "supplier"))
        need(g["supplier"] != "" or g["script_url"] != "", f"第 {index} 组缺少设计师供应商：已做好的素材要写明是谁做的（也是上传的制作人）")
        g["creator"] = g["reviewer"]  # Same person for progress reviewer, CLS creator and message.
        need(_pick(raw, "creator", g["reviewer"]) == g["reviewer"], "审核人与进度表创意者字段不一致")
        for key in ["client", *OPTIONAL_FIELDS]:
            need(_pick(raw, key, "") == "", f"进度表阶段 {key} 应留空")
            g[key] = ""
        if "label" in raw:
            # Only in the group message, where it stands in front of "*数量" inside brackets: no brackets, stars or line breaks of its own.
            label = js.trim(cell_text(raw["label"], "label"))
            need(label != "" and js.u16len(label) <= 20 and not re.search("[()（）*＊]", label), f"第 {index} 组的 label 需要 1-20 个字，且不含括号和星号")
            g["label"] = label
        g["brief"] = _pick(raw, "brief", "")  # Only still accepted so older requests load; it is not used anywhere.
        need(isinstance(g["brief"], str), "brief 必须是文本")
        need(g["script_url"] == "" or g["script_url"] == NO_SCRIPT or feishu_doc_path(g["script_url"], "wiki|docx|sheets", True) is not None,
             f"脚本链接必须是当前确认的飞书文档；没有文档时写「{NO_SCRIPT}」")
        cls = _pick(raw, "cls", {})
        need(not (is_object(cls) and "requirement_name" in cls), "CLS 需求名称由该组素材编号生成（GOFCN首号-GOFCN末号），不接受输入")
        need(is_object(cls) and _subset(list(cls.keys()), [*CLS_REQUIRED, *CLS_OPTIONAL]), "CLS 字段未知")
        if len(cls) > 0:
            for key in CLS_REQUIRED:
                need(isinstance(cls.get(key), str) and js.trim(cls[key]) != "", f"缺少 CLS {key}")
        for v in cls.values():
            need(isinstance(v, str) or js.is_num(v), "CLS 字段必须是明确的文本/数值")
        g["cls"] = dict(cls)
        normalized.append(g)
    result: dict[str, Any] = {"groups": normalized}
    if "worksheet" in request:
        need(isinstance(request["worksheet"], str) and js.trim(request["worksheet"]) != "", "工作表名称不能为空")
        result["worksheet"] = request["worksheet"]
    return result
