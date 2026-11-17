"""Plans, batches, verification, the event chain and the group message.

The fixtures and golden.json were produced by the Python implementation the TypeScript code replaced; digests and plan ids are not
comparable (they hash openpyxl's view of the workbook), so everything else is: row ranges, numbers, names, CSV text, verdicts, reasons.
"""

from __future__ import annotations

import os
import re
import unittest

from tests.helpers import fixture, make_xlsx, temp_dir, write_file

from gof import js
from gof.batch import cls_check, compose_message, load_batch, message, NO_TICKET, preflight, prepare, record, report, status, verify
from gof.cls import operation
from gof.feishu import same_name_rows, variant_rows
from gof.plan import make_plan
from gof.request import normalize_request
from gof.seal import reject_secrets
from gof.snapshot import Snapshot
from gof.util import Blocked, canonical, digest, read_json, write_json_new

GOLDEN = read_json(fixture("golden.json"))


def request(name: str) -> dict:
    return read_json(os.path.join(fixture("requests"), f"{name}.json"))


def strip(plan: dict) -> dict:
    """`naming` (how the sheet names a row, shown in the plan) came after the port."""
    return {k: v for k, v in plan.items() if k not in ("plan_id", "baseline_digest", "naming")}


def tmp() -> str:
    return temp_dir("gof-batch-")


def open_(name: str) -> Snapshot:
    return Snapshot(fixture(name))


#: JSON.parse cannot tell 2.0 from 2, so the original check for an integral float is not reproducible.
KNOWN_DIVERGENT = {"base.xlsx|float-quantity|"}
#: Refusals whose rule changed after the port: 设计师供应商 may be left open, but not in one group of a batch and not the other.
CHANGED_REFUSALS = {"base.xlsx|missing-field|": "some groups of this request fill the 设计师供应商 cell and some do not (a column is written in one go, without gaps; the cell stays empty when the supplier is the reviewer): append in two requests"}
#: No longer refused: a sheet without a naming formula to copy gets the sheet's naming written by the plan.
NOW_PLANNED = {"plan-no-formula.xlsx|basic|"}


def prepared() -> tuple[str, dict]:
    snapshot = open_("base.xlsx")
    plan = make_plan(snapshot, request("basic"))
    return prepare(plan, snapshot, output_root=tmp())["batch"], plan


def derived(**over: object) -> dict:
    """Requests the agent builds from a requirement document: 提炼词 by default, names built from the tags, rows of several kinds."""
    return {"supplier": "供应商甲", "reviewer": "张三", "script_url": "https://centurygames.feishu.cn/wiki/XYZ789?sheet=abc123",
            "cls": {"direction": "方向一", "task_type": "creative_a"}, **over}


class GoldenTests(unittest.TestCase):
    def test_plans_match_golden(self) -> None:
        """Plans match what the original implementation produced, including the refusals."""
        keys = [*GOLDEN["plan"].keys(), *GOLDEN["plan_errors"].keys()]
        self.assertGreaterEqual(len(keys), 25)
        for key in keys:
            if key in KNOWN_DIVERGENT:
                continue
            fx, req, current = key.split("|")
            snapshot = open_(fx)
            if key in NOW_PLANNED:
                rows = make_plan(snapshot, request(req), current or None)["rows"]
                self.assertEqual([rows[0]["name"], rows[0]["values"]["C6"]],
                                 ["GOFCN1011_玩法_卖点_视频_新爆点", '=SUBSTITUTE(CONCATENATE("GOFCN",A6,"_",D6,"_",E6,"_",F6,"_",B6),"__","_")'], key)
                continue
            if key in GOLDEN["plan"]:
                plan = make_plan(snapshot, request(req), current or None)
                self.assertEqual(canonical(strip(plan)), canonical(GOLDEN["plan"][key]), key)
                self.assertRegex(plan["plan_id"], r"^[0-9a-f]{24}$")
            else:
                with self.assertRaises(Blocked, msg=key) as caught:
                    make_plan(snapshot, request(req), current or None)
                self.assertEqual(caught.exception.message, CHANGED_REFUSALS.get(key, GOLDEN["plan_errors"][key]), key)

    def test_fingerprint(self) -> None:
        """The fingerprint is stable for the same file and changes with any content, style, rule or layout change."""
        a = open_("base.xlsx").fingerprint
        self.assertEqual(open_("base.xlsx").fingerprint, a)
        for name in ("plan-protected.xlsx", "plan-merged.xlsx", "plan-no-formula.xlsx", "applied-outside-style.xlsx", "applied-rule-changed.xlsx",
                     "applied-outside-comment.xlsx", "applied-hidden-row.xlsx", "applied-filter-changed.xlsx", "applied-complete.xlsx"):
            self.assertNotEqual(open_(name).fingerprint, a, name)

    def test_prepare(self) -> None:
        """Prepare writes the same CSV and clipboard text as before, starts a batch of its own when run again, and refuses a stale plan."""
        snapshot = open_("base.xlsx")
        plan = make_plan(snapshot, request("basic"))
        root = tmp()
        result = prepare(plan, snapshot, output_root=root)
        self.assertEqual(result["range"], GOLDEN["prepare"]["range"])
        self.assertEqual(result["csv"], GOLDEN["prepare"]["csv"])
        directory = result["batch"]
        self.assertEqual(os.path.basename(directory), plan["plan_id"])
        for name, text in GOLDEN["prepare"]["files"].items():
            with open(os.path.join(directory, name), encoding="utf-8", newline="") as handle:
                self.assertEqual(handle.read(), text, name)
        with open(os.path.join(directory, GOLDEN["prepare"]["csv"][0]), encoding="utf-8", newline="") as handle:
            self.assertTrue(handle.read().startswith("\ufeff脚本名称,脚本内容,创意标签\r\n"))
        # The same plan again (only possible on an unchanged sheet: nothing of the first batch landed) never reuses the first batch's directory.
        second = prepare(plan, snapshot, output_root=root)["batch"]
        self.assertEqual(os.path.basename(second), f"{plan['plan_id']}-2")
        self.assertEqual(len(status(second)["events"]), 0)
        self.assertEqual(load_batch(directory)[0], directory, "the first one is left as it was")
        stale = {**plan, "first_number": plan["first_number"] + 1}
        with self.assertRaises(Blocked) as caught:
            prepare(stale, snapshot, output_root=tmp())
        self.assertEqual(caught.exception.message, GOLDEN["prepare"]["stale_plan"])
        # Any change to a generated file is noticed.
        with open(os.path.join(directory, "append.tsv"), "a", encoding="utf-8") as handle:
            handle.write("x")
        self.assertRaisesRegex(Blocked, "a batch file has changed: append.tsv", status, directory)

    def test_verify_golden(self) -> None:
        """Verification gives the same verdicts as the original implementation for every appended variant."""
        directory, _ = prepared()
        for fx, expected in GOLDEN["verify"].items():
            result = verify(directory, open_(fx))
            self.assertEqual(result["status"], expected["status"], fx)
            self.assertEqual(result["matched_cells"], expected["matched_cells"], fx)
            self.assertEqual(result["expected_cells"], expected["expected_cells"], fx)
            if fx == "applied-renamed-sheet.xlsx":
                # The original reported an arbitrary 20 of many; the first message is the stable part.
                self.assertEqual(result["issues"][0], next(i for i in expected["issues"] if i.startswith("the list or order of tabs")))
                continue
            self.assertEqual(sorted(result["issues"]), sorted(expected["issues"]), fx)

    def test_preflight_golden(self) -> None:
        """Preflight only lets an untouched snapshot through."""
        directory, _ = prepared()
        for fx, expected in GOLDEN["preflight"].items():
            if expected["status"] == "blocked":
                with self.assertRaises(Blocked, msg=fx) as caught:
                    preflight(directory, open_(fx))
                self.assertEqual(caught.exception.message, expected["reason"], fx)
            else:
                self.assertEqual(preflight(directory, open_(fx)), expected, fx)

    def test_state_machine(self) -> None:
        """The batch state machine accepts and refuses the same steps in the same words."""
        root = tmp()
        snapshot = open_("base.xlsx")
        plan = make_plan(snapshot, request("basic"))
        directory = prepare(plan, snapshot, output_root=root)["batch"]

        def run(name: str, step: str, group: object = None, ev: object = None, fx: object = None) -> dict:
            try:
                return record(directory, step, group, ev, open_(fx) if fx else None)
            except Blocked as error:
                return {"status": "blocked", "reason": error.message}

        steps = {
            "cls before append": ("cls_started", "01"),
            "secret in evidence": ("append_started", None, {"access_token": "x"}),
            "append_started": ("append_started",),
            "append_started twice": ("append_started",),
            "verified with partial": ("append_verified", None, {}, "applied-partial.xlsx"),
            "verified without snapshot": ("append_verified",),
            "verified": ("append_verified", None, {}, "applied-complete.xlsx"),
            "append_verified twice": ("append_verified", None, {}, "applied-complete.xlsx"),
            "bad group": ("cls_started", "09"),
            "cls_started": ("cls_started", "01"),
            "cls_started twice": ("cls_started", "01"),
            "draft before attempt": ("draft_saved", "01", {"confirmed": True, "unpublished": True, "url": "https://creative.centurygames.cn/requirement/1/edit-outer"}),
            "draft bad url": ("draft_saved", "01", {"confirmed": True, "unpublished": True, "url": "https://evil.example.com/x"}),
            "handoff no reason": ("handoff", "01", {}),
            "bad requirement number": ("requirement_number", "01", {"confirmed": True, "requirement_number": "GOFCN123"}),
            "blocked no reason": ("blocked", None, {}),
            "unknown step": ("frobnicate", "01", {}),
            "cls_started group 2": ("cls_started", "02"),
            "handoff group 2": ("handoff", "02", {"reason": "人工接手"}),
            "handoff group 2 twice": ("handoff", "02", {"reason": "再次"}),
            "number group 2": ("requirement_number", "02", {"confirmed": True, "requirement_number": "GOFCN20260100001"}),
            "blocked": ("blocked", None, {"reason": "测试阻塞"}),
            "record while blocked": ("handoff", "01", {"reason": "x"}),
            "resumed without confirm": ("resumed", None, {"reason": "x"}, "applied-complete.xlsx"),
            "resumed": ("resumed", None, {"confirmed": True, "reason": "已核对", "cls_reconciled": True}, "applied-complete.xlsx"),
        }
        expected = {f["name"]: f["result"] for f in GOLDEN["flow"] if "step" in f}
        states = {f["name"]: f["status"] for f in GOLDEN["flow"] if f.get("status")}

        def check_state(name: str) -> None:
            s = status(directory)
            self.assertEqual({"append_verified": s["append_verified"], "blocked": s["blocked"], "groups": s["groups"]}, states[name], name)

        for name, spec in steps.items():
            got = run(name, *spec)
            self.assertIn(name, expected, f"no golden for {name}")
            self.assertEqual(got, expected[name], name)
            if name == "verified":
                check_state("after append_verified")
            if name == "blocked":
                check_state("after blocked")
            if name == "resumed":
                check_state("after resumed")

        observations = {
            "fill": {"logged_in": True, "can_create": True, "unpublished": True, "save_result": "ok"},
            "publish": {},
            "save_ok": {"logged_in": True, "can_create": True, "unpublished": True, "save_result": "ok", "required_complete": True},
            "save_incomplete": {"logged_in": True, "can_create": True, "unpublished": True, "save_result": "ok"},
            "save_loggedout": {"logged_in": False, "can_create": True, "unpublished": True, "required_complete": True},
            "save_unknown": {"logged_in": True, "can_create": True, "unpublished": True, "save_result": "unknown", "required_complete": True},
            "import": {"logged_in": True, "can_create": True, "unpublished": True, "save_result": "ok"},
        }
        checks = [f for f in GOLDEN["flow"] if f["name"].startswith("cls-check") and f["name"] != "cls-check bad group"]
        wanted = [("fill", "fill"), ("publish", "publish"), ("save_draft", "save_ok"), ("save_draft", "save_incomplete"), ("save_draft", "save_loggedout"),
                  ("save_draft", "save_unknown"), ("import_scripts", "import")]
        for i, (action, key) in enumerate(wanted):
            want = checks[i]["result"]
            try:
                self.assertEqual(cls_check(directory, "01", action, observations[key]), want, f"{action}/{key}")
            except Blocked as error:
                self.assertEqual({"status": "blocked", "reason": error.message}, want, f"{action}/{key}")
        with self.assertRaises(Blocked) as caught:
            cls_check(directory, "09", "save_draft", {})
        self.assertEqual(caught.exception.message, "no such group")

        self.assertEqual(message(directory), next(f for f in GOLDEN["flow"] if f["name"] == "message")["result"])
        full = report(directory)
        self.assertEqual(full["status"], "report_ready")
        body = next(f for f in GOLDEN["flow"] if f["name"] == "report body")["result"]
        for key in ("status", "worksheet", "range", "target", "message"):
            self.assertEqual(full[key], body[key], key)
        self.assertEqual([{"group": g["group"], "quantity": g["quantity"], "steps": g["steps"]} for g in full["groups"]],
                         next(f for f in GOLDEN["flow"] if f["name"] == "report groups")["result"])
        self.assertEqual(load_batch(directory)[1]["plan"]["plan_id"], plan["plan_id"])

    def test_event_chain(self) -> None:
        """The event chain notices a changed or missing event."""
        directory, _ = prepared()
        record(directory, "append_started", None)
        record(directory, "blocked", None, {"reason": "测试"})
        file = os.path.join(directory, "events", "000001.json")
        with open(file, encoding="utf-8") as handle:
            original = handle.read()
        write_file(file, original.replace("append_started", "append_verified"))
        self.assertRaisesRegex(Blocked, "a batch event was modified", status, directory)
        write_file(file, original)
        os.rename(file, os.path.join(directory, "events", "000009.json"))
        self.assertRaisesRegex(Blocked, "a batch event is missing or out of order", status, directory)

    def test_canonical(self) -> None:
        """Canonical JSON and digests agree byte for byte with what the batches on disk were sealed with."""
        for sample in GOLDEN["canonical"]:
            self.assertEqual(canonical(sample["value"]), sample["canonical"])
            self.assertEqual(digest(sample["value"]), sample["sha256"])
        # JavaScript's number formatting, which the digests depend on.
        self.assertEqual(canonical([1.0, 0.5, 1e21, 1e-7, 123456789012345680000, -0.0, 1.5e-6, 2 ** 53]), "[1,0.5,1e+21,1e-7,123456789012345680000,0,0.0000015,9007199254740992]")

    def test_secrets(self) -> None:
        """Records that carry credentials are refused."""
        values = {"key": {"access_token": "x"}, "bearer": {"a": "Bearer abc"}, "nested": {"a": [{"Cookie": "c"}]}, "ok": {"a": "bearer"},
                  "session-id-token": {"id_token": 1}, "authz": {"Authorization": "x"}}
        for name, reason in GOLDEN["seal"].items():
            if reason == "ok":
                reject_secrets(values[name])
            else:
                with self.assertRaises(Blocked, msg=name) as caught:
                    reject_secrets(values[name])
                self.assertEqual(caught.exception.message, reason, name)

    def test_exclusive_json(self) -> None:
        """JSON files are written exclusively and never over an existing file."""
        file = os.path.join(tmp(), "x.json")
        write_json_new(file, {"a": 1})
        self.assertRaisesRegex(FileExistsError, "EEXIST", write_json_new, file, {"a": 2})
        self.assertEqual(read_json(file)["a"], 1)

    def test_unusable_header(self) -> None:
        """A GOFCN sheet with an unusable header is never skipped silently: it stops the plan when it may be the target or hold higher numbers."""
        for fx in ("plan-header-dup.xlsx", "plan-no-header.xlsx"):
            self.assertRaisesRegex(Blocked, "not skipped silently", make_plan, open_(fx), request("basic"), None)


class DerivedRequestTests(unittest.TestCase):
    def test_default_keyword(self) -> None:
        """提炼词 defaults to 关键词 and the material name is what the sheet's naming formula makes of the tags, form and keyword."""
        plan = make_plan(open_("base.xlsx"), {"groups": [derived(quantity=2, primary_tag="玩法", secondary_tag="卖点", creative_form="视频")]})
        self.assertEqual([r["name"] for r in plan["rows"]], ["GOFCN1011_玩法_卖点_视频_关键词", "GOFCN1012_玩法_卖点_视频_关键词"])
        self.assertEqual(plan["naming"], "GOFCN<素材编号>_<一级标签>_<二级标签>_<创意形式>_<提炼词>")
        g = plan["groups"][0]["request"]
        self.assertEqual([g["keyword"], g.get("material_name"), g["topic"], "items" in g], ["关键词", None, "玩法_卖点", False])
        # Written out in full, the same request gives the same rows; the name it gives is kept, to be checked against the formula.
        same = make_plan(open_("base.xlsx"), {"groups": [derived(quantity=2, material_name="GOFCN1_玩法_卖点_视频_关键词", creative_form="视频", keyword="关键词")]})
        self.assertEqual([canonical(same["rows"]), same["groups"][0]["request"]["material_name"]], [canonical(plan["rows"]), "玩法_卖点_视频_关键词"])
        # A one-item list is the same group too.
        listed = make_plan(open_("base.xlsx"), {"groups": [derived(items=[{"quantity": 2, "primary_tag": "玩法", "secondary_tag": "卖点", "creative_form": "视频"}])]})
        self.assertEqual(canonical(listed), canonical(plan))
        # The 素材名称 is the creative's name in CLS too, which cannot hold / or \: such a row could never be uploaded.
        for keyword in ("横版/竖版", "A\\B"):
            self.assertRaisesRegex(Blocked, r"cannot contain / or \\", make_plan, open_("base.xlsx"),
                                   {"groups": [derived(quantity=1, primary_tag="玩法", secondary_tag="卖点", creative_form="视频", keyword=keyword)]})

    def test_items(self) -> None:
        """A group can hold rows of several kinds: they keep their order, share one CSV and one CLS ticket, and are counted by kind."""
        req = {"groups": [derived(primary_tag="玩法", creative_form="视频", items=[
            {"quantity": 2, "secondary_tag": "卖点"}, {"quantity": 1, "secondary_tag": "体验", "keyword": "新词"}, {"quantity": 1, "secondary_tag": "卖点"},
            {"quantity": 1, "primary_tag": "画面", "secondary_tag": "体验", "creative_form": "图文"}])]}
        snapshot = open_("base.xlsx")
        plan = make_plan(snapshot, req)
        self.assertEqual(plan["range"], "A6:I10")
        self.assertEqual([r["name"] for r in plan["rows"]], ["GOFCN1011_玩法_卖点_视频_关键词", "GOFCN1012_玩法_卖点_视频_关键词",
                                                            "GOFCN1013_玩法_体验_视频_新词", "GOFCN1014_玩法_卖点_视频_关键词", "GOFCN1015_画面_体验_图文_关键词"])
        self.assertEqual([[r["values"][f"B{r['row']}"], r["values"][f"E{r['row']}"], r["values"][f"H{r['row']}"], r["values"][f"I{r['row']}"]] for r in plan["rows"]],
                         [["关键词", "卖点", "供应商甲", "张三"], ["关键词", "卖点", "供应商甲", "张三"], ["新词", "体验", "供应商甲", "张三"],
                          ["关键词", "卖点", "供应商甲", "张三"], ["关键词", "体验", "供应商甲", "张三"]])
        self.assertEqual(len(plan["groups"]), 1)
        self.assertEqual(plan["groups"][0]["request"]["quantity"], 5)
        # prepare makes the plan again from the normalized request, which must come out the same.
        self.assertEqual(canonical(normalize_request(plan["request"])), canonical(plan["request"]))
        self.assertEqual(next(d for d in plan["dropdowns"] if d["field"] == "secondary_tag")["missing"], [])
        directory = prepare(plan, snapshot, output_root=tmp())["batch"]
        with open(os.path.join(directory, plan["groups"][0]["csv"]), encoding="utf-8", newline="") as handle:
            csv = handle.read()
        self.assertEqual(len(csv.split("\r\n")), 7)
        self.assertIn("GOFCN1011_玩法_卖点_视频_关键词,https://centurygames.feishu.cn/wiki/XYZ789?sheet=abc123,", csv)
        self.assertIn("GOFCN1015_画面_体验_图文_关键词,同上,", csv)
        # Without a label the message counts the rows by 一级标签, in the order they first appear.
        self.assertEqual(compose_message(plan["groups"], [{"group": "01", "step": "handoff", "evidence": {}}])["message"].split("\n")[1],
                         "需求编号：【发布后补填需求编号】（玩法*4，画面*1）")


def kind(label: object, quantity: int, supplier: str, reviewer: str, form: str = "视频") -> dict:
    extra = {"label": label} if label else {}
    return derived(supplier=supplier, reviewer=reviewer, quantity=quantity, primary_tag="玩法", secondary_tag="卖点", creative_form=form, **extra)


def numbered(*entries: tuple[str, str]) -> list[dict]:
    return [{"group": group, "step": "requirement_number", "evidence": {"requirement_number": number}} for group, number in entries]


class MessageTests(unittest.TestCase):
    def test_several_documents(self) -> None:
        """Several requirement documents give one message: every requirement with its supplier, then each creator once."""
        plan = make_plan(open_("base.xlsx"), {"groups": [kind("迭代", 22, "潘越", "张天伟"), kind("素人剧情", 25, "丁杰", "张天伟"), kind("明星", 25, "丁杰", "雷佳威"),
                                                         kind("解说", 25, "王晨龙", "雷佳威", "2D")]})
        got = compose_message(plan["groups"], numbered(("01", "GOFCN20250919001"), ("02", "GOFCN20250919002"), ("03", "GOFCN20250919003"), ("04", "GOFCN20250919004")))
        self.assertEqual(got, {"status": "message_ready", "skipped_groups": [], "missing_numbers": [], "message": "\n".join([
            "老师好 刚刚发了新的工单", "需求编号：GOFCN20250919001（迭代*22）", "供应商：潘越", "需求编号：GOFCN20250919002（素人剧情*25）", "供应商：丁杰",
            "需求编号：GOFCN20250919003（明星*25）", "供应商：丁杰", "需求编号：GOFCN20250919004（解说*25）", "供应商：王晨龙", "创意人：张天伟/雷佳威", "辛苦安排一下~"])})

    def test_one_requirement(self) -> None:
        """One requirement gets the same wording, and a missing official number keeps the placeholder."""
        plan = make_plan(open_("base.xlsx"), {"groups": [kind("素人剧情", 25, "丁杰", "张天伟")]})
        got = compose_message(plan["groups"], [{"group": "01", "step": "draft_saved", "evidence": {}}])
        self.assertEqual(got["message"], "\n".join(["老师好 刚刚发了新的工单", "需求编号：【发布后补填需求编号】（素人剧情*25）", "供应商：丁杰", "创意人：张天伟", "辛苦安排一下~"]))
        self.assertEqual([got["status"], got["missing_numbers"], got["skipped_groups"]], ["message_ready", ["01"], []])

    def test_without_ticket(self) -> None:
        """A requirement without a ticket is left out of the message and named; with no ticket at all there is no message."""
        plan = make_plan(open_("base.xlsx"), {"groups": [kind("明星", 3, "丁杰", "张天伟"), kind("解说", 2, "王晨龙", "雷佳威")]})
        some = compose_message(plan["groups"], numbered(("02", "GOFCN20250919002")))
        self.assertEqual([some["skipped_groups"], some["missing_numbers"]], [["01"], []])
        self.assertTrue("明星" not in some["message"] and "需求编号：GOFCN20250919002（解说*2）" in some["message"] and "创意人：雷佳威" in some["message"])
        # Only a saved draft, a hand-over or an official number counts; starting the CLS stage does not.
        none = compose_message(plan["groups"], [{"group": "01", "step": "cls_started", "evidence": {}}, {"group": None, "step": "append_verified", "evidence": {}}])
        self.assertEqual(none, {"status": "message_not_ready", "message": None, "skipped_groups": ["01", "02"], "missing_numbers": []})

    def test_label(self) -> None:
        """The label of a group is a short plain name; it is optional and survives normalizing again."""
        plan = make_plan(open_("base.xlsx"), {"groups": [kind("素人剧情", 2, "丁杰", "张三")]})
        self.assertEqual(plan["groups"][0]["request"]["label"], "素人剧情")
        self.assertEqual(canonical(normalize_request(plan["request"])), canonical(plan["request"]))
        self.assertNotIn("label", make_plan(open_("base.xlsx"), {"groups": [kind(None, 2, "丁杰", "张三")]})["groups"][0]["request"])
        for bad in ("", "  ", "素人（剧情）", "明星*2", "a\nb", "x" * 21):
            self.assertRaisesRegex(Blocked, "label", normalize_request, {"groups": [{**kind(None, 2, "丁杰", "张三"), "label": bad}]})

    def test_refused_items(self) -> None:
        """A request with rows of several kinds is refused when an item is incomplete or the group also counts its own rows."""
        def refused(**group: object) -> str:
            try:
                make_plan(open_("base.xlsx"), {"groups": [derived(**group)]})
            except Blocked as error:
                return error.message
            return "no error"

        item = {"quantity": 1, "primary_tag": "玩法", "secondary_tag": "卖点", "creative_form": "视频"}
        self.assertEqual(refused(quantity=2, items=[item]), "group 1: quantity is not the sum of its items' quantities")
        self.assertEqual(refused(material_name="玩法_卖点_视频_关键词", items=[item]), "group 1 has items: give material_name in the items")
        self.assertEqual(refused(items=[]), "group 1: items needs at least one entry")
        self.assertEqual(refused(items=[item, {**item, "extra": 1}]), "group 1 item 2 has unknown fields")
        self.assertEqual(refused(items=[item, {"quantity": 1, "primary_tag": "玩法", "creative_form": "视频"}]), "group 1 item 2: primary_tag / secondary_tag is missing")
        self.assertEqual(refused(items=[item, {"quantity": 1, "primary_tag": "玩法", "secondary_tag": "卖点"}]),
                         "some rows of this request have a creative_form and some do not (a column is written in one go, without gaps): leave creative_form out for all and put it in front of the keyword (the name stays the same), or append in two requests")
        # No 创意形式 at all is fine: the formula then gets SUBSTITUTE(…,"__","_") around it.
        self.assertEqual(refused(items=[{"quantity": 1, "primary_tag": "玩法", "secondary_tag": "卖点"}]), "no error")
        self.assertEqual(refused(items=[{**item, "secondary_tag": "卖点_体验"}]), 'group 1 item 1: the 二级标签 "卖点_体验" cannot contain _: a 素材名称 is cut into parts at _, a tag or creative_form is one part and the rest goes into the keyword')
        self.assertEqual(refused(items=[{**item, "creative_form": "3D_AI"}]), 'group 1 item 1: the 创意形式 "3D_AI" cannot contain _: a 素材名称 is cut into parts at _, a tag or creative_form is one part and the rest goes into the keyword')
        for keyword in ("_企鹅", "企鹅_", "企鹅__竖版"):
            self.assertEqual(refused(items=[{**item, "keyword": keyword}]), f"group 1 item 1: the keyword \"{keyword}\" cannot start or end with _ or hold __")
        self.assertEqual(refused(items=[{**item, "quantity": 0}]), "quantity must be a positive whole number")
        self.assertEqual(refused(items=[{**item, "material_name": "玩法_卖点_图文_关键词"}]),
                         'by the sheet\'s naming (GOFCN<素材编号>_<一级标签>_<二级标签>_<创意形式>_<提炼词>) this row would be named "GOFCN1011_玩法_卖点_视频_关键词", not the requested "GOFCN1011_玩法_卖点_图文_关键词": check primary_tag, secondary_tag, creative_form and keyword')
        self.assertEqual(refused(items=[{"quantity": 1, "material_name": "玩法_卖点_图文_关键词", "creative_form": "视频"}]), "material_name does not agree with creative_form / keyword: settle it")
        self.assertEqual(refused(quantity=1, creative_form="视频"), "group 1: primary_tag / secondary_tag is missing")
        self.assertEqual(refused(**{**item, "script_url": "https://centurygames.feishu.cn/sheets/shtABC123?sheet=x"}), "no error")


# ---- names are GOFCN<编号>_<一级>_<二级>_<创意形式>_<提炼词>; from row 23282 (2026-10-22) the production sheet's formula left out 创意形式
# and 客户端 for a while, and a new row is not to copy that

LIVE_HEADER = ["", "创意批次", "素材编号", "提炼词", "片头引用（初版编号）", "创意引用（海外汉化解说/迭代）（初版编号）", "素材名称", "一级标签（方向）",
               "二级标签（卖点）", "创意表达（可多选）", "创意形式（制作形式）", "客户端", "设计师供应商", "创意者脚本审核人"]


def OLD(r: int) -> str:
    return f'CONCATENATE("GOFCN",C{r},"_",H{r},"_",I{r},"_",K{r},"_",D{r},L{r})'


def OWN(r: int) -> str:
    return f'=SUBSTITUTE(CONCATENATE("GOFCN",C{r},"_",H{r},"_",I{r},"_",K{r},"_",D{r}),"__","_")'


def live_sheet(last=lambda r: f'CONCATENATE("GOFCN",C{r},"_",H{r},"_",I{r},"_",D{r})') -> Snapshot:
    """A progress sheet like the production one: the last rows named by the formula given (by default without 创意形式, as from row 23282)."""
    def row(r: int, number: int, keyword: str, tags: tuple[str, str], formula: str, form: object = None) -> list:
        return [None, None, number, keyword, None, None, {"f": formula}, tags[0], tags[1], None, form, None, "达治", "靳筱萌"]

    file = os.path.join(tmp(), "live.xlsx")
    write_file(file, make_xlsx([{"name": "26年1月-GOFCN", "rows": [LIVE_HEADER,
                                                                   row(2, 67679, "关键词", ("AI迭代", "题材片头"), OLD(2), "2D"),
                                                                   row(3, 67736, "3D_BJAI片头矿工冰层版CN60141", ("AI片头衔接", "模拟经营"), last(3)),
                                                                   row(4, 67737, "割资源冰块对抗_竖版", ("AI玩法优化", "搂冰块抢资源"), last(4))]}]))
    return Snapshot(file)


def from_files(names: list[str]) -> dict:
    """Rows from files named 一级_二级_提炼词: no 创意形式, everything after the tags is the 提炼词 (竖版 too)."""
    items = []
    for name in names:
        primary, secondary, *rest = re.sub(r"^GOFCNx+_", "", name).split("_")
        items.append({"quantity": 1, "primary_tag": primary, "secondary_tag": secondary, "keyword": "_".join(rest), "material_name": name})
    return {"groups": [derived(label="AI玩法优化", items=items)]}


def finished(names: list[str]) -> dict:
    """Rows registered for finished materials (the upload skill): no script link and no CLS fields, so no CSV and no ticket."""
    group = from_files(names)["groups"][0]
    return {"groups": [{k: v for k, v in group.items() if k not in ("script_url", "cls", "label")}]}


class NamingTests(unittest.TestCase):
    def test_new_row_naming(self) -> None:
        """A new row is named GOFCN<编号>_<一级>_<二级>_<创意形式>_<提炼词> whatever the row above does; 创意形式 may stay empty."""
        snapshot = live_sheet()
        plan = make_plan(snapshot, {"groups": [derived(quantity=2, primary_tag="AI生成", secondary_tag="设定3.0", keyword="企鹅保龄球")]})
        self.assertEqual(plan["naming"], "GOFCN<素材编号>_<一级标签>_<二级标签>_<创意形式>_<提炼词>")
        self.assertEqual([r["name"] for r in plan["rows"]], ["GOFCN67738_AI生成_设定3.0_企鹅保龄球", "GOFCN67739_AI生成_设定3.0_企鹅保龄球"])
        self.assertEqual([plan["rows"][0]["values"]["K5"], plan["rows"][0]["values"]["G5"], plan["rows"][1]["values"]["G6"]], [None, OWN(5), OWN(6)])
        formed = make_plan(snapshot, {"groups": [derived(quantity=1, primary_tag="AI生成", secondary_tag="设定3.0", creative_form="3D")]})
        self.assertEqual([formed["rows"][0]["values"]["K5"], formed["rows"][0]["name"]], ["3D", "GOFCN67738_AI生成_设定3.0_3D_关键词"])
        # A row above that names rows that way keeps its own spelling (客户端 at the end stays empty here); without a 创意形式 it gets SUBSTITUTE.
        usual = live_sheet(OLD)
        kept = make_plan(usual, {"groups": [derived(quantity=1, primary_tag="AI生成", secondary_tag="设定3.0", creative_form="3D")]})
        self.assertEqual([kept["rows"][0]["values"]["G5"], kept["rows"][0]["name"], kept["naming"]],
                         [f"={OLD(5)}", "GOFCN67738_AI生成_设定3.0_3D_关键词", "GOFCN<素材编号>_<一级标签>_<二级标签>_<创意形式>_<提炼词><客户端>"])
        bare = make_plan(usual, {"groups": [derived(quantity=1, primary_tag="AI生成", secondary_tag="设定3.0", keyword="冻鱼飞盘")]})
        self.assertEqual([bare["rows"][0]["values"]["G5"], bare["rows"][0]["name"]], [f'=SUBSTITUTE({OLD(5)},"__","_")', "GOFCN67738_AI生成_设定3.0_冻鱼飞盘"])
        wrapped = live_sheet(lambda r: f'SUBSTITUTE({OLD(r)},"__","_")')
        self.assertEqual(make_plan(wrapped, {"groups": [derived(quantity=1, primary_tag="AI生成", secondary_tag="设定3.0", keyword="冻鱼飞盘")]})["rows"][0]["values"]["G5"],
                         f'=SUBSTITUTE({OLD(5)},"__","_")')

    def test_formula_outside_columns(self) -> None:
        """A formula above that also takes a cell left of the appended columns is followed too: in a new row that cell is empty."""
        with_batch = live_sheet(lambda r: f'CONCATENATE("GOFCN",C{r},"_",H{r},"_",I{r},"_",K{r},"_",D{r},B{r})')
        plan = make_plan(with_batch, {"groups": [derived(quantity=1, primary_tag="AI生成", secondary_tag="设定3.0", creative_form="3D")]})
        self.assertEqual(plan["range"], "C5:N5")
        self.assertEqual([plan["rows"][0]["values"]["G5"], plan["rows"][0]["name"]], ['=CONCATENATE("GOFCN",C5,"_",H5,"_",I5,"_",K5,"_",D5,B5)', "GOFCN67738_AI生成_设定3.0_3D_关键词"])
        self.assertEqual(plan["naming"], "GOFCN<素材编号>_<一级标签>_<二级标签>_<创意形式>_<提炼词><创意批次>")
        # A reference to another row is no naming of this one: the sheet's own naming is written instead.
        cross = live_sheet(lambda r: f'CONCATENATE("GOFCN",C{r},"_",H{r},"_",I{r},"_",K{r},"_",D{r - 1})')
        self.assertEqual(make_plan(cross, {"groups": [derived(quantity=1, primary_tag="AI生成", secondary_tag="设定3.0", creative_form="3D")]})["rows"][0]["values"]["G5"], OWN(5))

    def test_pending_names(self) -> None:
        """Files named for rows not appended yet (GOFCNxxxxx_…) become rows of exactly those names, checked against the formula."""
        snapshot = live_sheet()
        # Two versions of one piece: 兵临城下塔防 is no 创意形式 here but the start of each 提炼词.
        names = ["GOFCNxxxxx_AI玩法优化_火车运输御敌_兵临城下塔防_冻鱼_竖版", "GOFCNxxxxx_AI玩法优化_火车运输御敌_兵临城下塔防_企鹅_竖版"]
        plan = make_plan(snapshot, from_files(names))
        self.assertEqual([[r["name"], r["values"][f"K{r['row']}"], r["values"][f"D{r['row']}"]] for r in plan["rows"]], [
            ["GOFCN67738_AI玩法优化_火车运输御敌_兵临城下塔防_冻鱼_竖版", None, "兵临城下塔防_冻鱼_竖版"],
            ["GOFCN67739_AI玩法优化_火车运输御敌_兵临城下塔防_企鹅_竖版", None, "兵临城下塔防_企鹅_竖版"]])
        self.assertEqual([i["material_name"] for i in plan["groups"][0]["request"]["items"]], [n.replace("GOFCNxxxxx_", "") for n in names])
        self.assertEqual(next(d for d in plan["dropdowns"] if d["field"] == "secondary_tag")["missing"], ["火车运输御敌"])
        # Prepared again on the same sheet it is the same plan, and its CSV names the rows as the files are named.
        result = prepare(plan, snapshot, output_root=tmp())
        self.assertEqual(result["csv"], ["创意脚本_01_AI玩法优化_火车运输御敌_供应商甲.csv"])
        with open(os.path.join(result["batch"], result["csv"][0]), encoding="utf-8", newline="") as handle:
            self.assertIn("\r\nGOFCN67739_AI玩法优化_火车运输御敌_兵临城下塔防_企鹅_竖版,同上,", handle.read())
        # A file whose name does hold a 创意形式 can have it in its column: the name is the same.
        formed = make_plan(snapshot, {"groups": [derived(items=[{"quantity": 1, "primary_tag": "AI生成", "secondary_tag": "设定3.0", "creative_form": "3D",
                                                                 "keyword": "围炉_横版", "material_name": "GOFCNxxxxx_AI生成_设定3.0_3D_围炉_横版"}])]})
        self.assertEqual([formed["rows"][0]["name"], formed["rows"][0]["values"]["K5"]], ["GOFCN67738_AI生成_设定3.0_3D_围炉_横版", "3D"])
        # A name the formula cannot make is refused with what the row would be called.
        wrong = from_files(names)
        wrong["groups"][0]["items"][0]["keyword"] = "兵临城下塔防_冻鱼"
        with self.assertRaises(Blocked) as caught:
            make_plan(snapshot, wrong)
        self.assertEqual(caught.exception.message, 'by the sheet\'s naming (GOFCN<素材编号>_<一级标签>_<二级标签>_<创意形式>_<提炼词>) this row would be named "GOFCN67738_AI玩法优化_火车运输御敌_兵临城下塔防_冻鱼", not the requested "GOFCN67738_AI玩法优化_火车运输御敌_兵临城下塔防_冻鱼_竖版": check primary_tag, secondary_tag, creative_form and keyword')
        # A row of that name already in the sheet is said: the file has most likely been registered before.
        again = make_plan(snapshot, from_files(["GOFCNxxxxx_AI玩法优化_搂冰块抢资源_割资源冰块对抗_竖版", *names]))
        self.assertEqual(same_name_rows(snapshot, again), [{"name": "AI玩法优化_搂冰块抢资源_割资源冰块对抗_竖版",
                                                            "rows": [{"number": 67737, "name": "GOFCN67737_AI玩法优化_搂冰块抢资源_割资源冰块对抗_竖版", "where": "26年1月-GOFCN row 4"}]}])
        self.assertEqual(same_name_rows(snapshot, plan), [])
        # A name starting with a row's (cut at a _, or within the first word) may be a variant of it: said, for 合并变体名称.
        variants = make_plan(snapshot, from_files(["GOFCNxxxxx_AI玩法优化_搂冰块抢资源_割资源冰块对抗_竖版_加长版", "GOFCNxxxxx_AI玩法优化_搂冰块抢资源_割资源冰块对抗_竖版加长", *names]))
        row67737 = [{"number": 67737, "name": "GOFCN67737_AI玩法优化_搂冰块抢资源_割资源冰块对抗_竖版", "where": "26年1月-GOFCN row 4"}]
        self.assertEqual(variant_rows(snapshot, variants), [{"name": "AI玩法优化_搂冰块抢资源_割资源冰块对抗_竖版_加长版", "rows": row67737},
                                                            {"name": "AI玩法优化_搂冰块抢资源_割资源冰块对抗_竖版加长", "rows": row67737}])
        self.assertEqual(variant_rows(snapshot, plan), [])

    def test_finished_materials(self) -> None:
        """Rows registered for finished materials need no script link: they get no CSV and no CLS ticket, the rest of the plan is the same."""
        snapshot = live_sheet()
        names = ["GOFCNxxxxx_AI玩法优化_火车运输御敌_兵临城下塔防_冻鱼_竖版", "GOFCNxxxxx_AI玩法优化_火车运输御敌_兵临城下塔防_企鹅_竖版"]
        plan = make_plan(snapshot, finished(names))
        ticketed = make_plan(snapshot, from_files(names))
        self.assertEqual(canonical([plan["rows"], plan["range"]]), canonical([ticketed["rows"], ticketed["range"]]))
        self.assertEqual([plan["groups"][0]["csv"], plan["groups"][0]["request"]["script_url"], plan["groups"][0]["request"]["cls"]], [None, "", {}])
        self.assertEqual(canonical(normalize_request(plan["request"])), canonical(plan["request"]))
        result = prepare(plan, snapshot, output_root=tmp())
        self.assertEqual(result["csv"], [])
        self.assertEqual([f for f in os.listdir(result["batch"]) if f.endswith(".csv")], [])
        self.assertIsNone(report(result["batch"])["groups"][0]["csv"])
        with self.assertRaises(Blocked) as caught:
            operation(result["batch"], "01", "save_draft", {}, {})
        self.assertEqual(caught.exception.message, NO_TICKET)
        # A ticket still needs its script link.
        unlinked = {k: v for k, v in from_files(names)["groups"][0].items() if k != "script_url"}
        with self.assertRaises(Blocked) as caught:
            normalize_request({"groups": [unlinked]})
        self.assertEqual(caught.exception.message, "group 1 lacks script_url")
        with self.assertRaises(Blocked) as caught:
            normalize_request({"groups": [{**unlinked, "script_url": " "}]})
        self.assertEqual(caught.exception.message, "script_url is missing")
        self.assertRaisesRegex(Blocked, "script_url must be a company Feishu document link", normalize_request, {"groups": [{**finished(names)["groups"][0], "script_url": "https://example.com/x"}]})
        # Finished materials were made by someone: their 设计师供应商 (the uploads' 制作人) is never left open.
        unmade = {k: v for k, v in finished(names)["groups"][0].items() if k != "supplier"}
        for group in (unmade, {**unmade, "supplier": " "}):
            with self.assertRaises(Blocked) as caught:
                normalize_request({"groups": [group]})
            self.assertEqual(caught.exception.message, "group 1 lacks a supplier: finished materials need who made them (also the uploads' 制作人)")
        # Files named with their number (GOFCN67680_…) have most likely been registered: their rows are verified, not appended a second time.
        with self.assertRaises(Blocked) as caught:
            normalize_request(finished(["GOFCN67739_AI玩法优化_火车运输御敌_兵临城下塔防_企鹅_竖版"]))
        self.assertTrue(caught.exception.message.startswith("group 1: material_name carries a number (GOFCN67739_AI玩法优化_火车运输御敌_兵临城下塔防_企鹅_竖版): a numbered material is most likely registered already, so check it with gof creative files --check"))
        # A requirement's sample name keeps its old number out of the new rows, as before.
        normalize_request(from_files(["GOFCN67739_AI玩法优化_火车运输御敌_兵临城下塔防_企鹅_竖版"]))

    def test_landed_unverified(self) -> None:
        """Rows for finished materials whose write landed unverified are not appended again; other files are."""
        names = ["GOFCNxxxxx_AI玩法优化_火车运输御敌_兵临城下塔防_冻鱼_竖版", "GOFCNxxxxx_AI玩法优化_火车运输御敌_兵临城下塔防_企鹅_竖版"]
        base = tmp()
        before = live_sheet()
        directory = prepare(make_plan(before, finished(names)), before, output_root=base)["batch"]
        record(directory, "append_started")
        # The sheet as it is now: the two rows went in, but the answer was lost and nothing was verified.
        file = os.path.join(tmp(), "landed.xlsx")

        def landed_row(r: int, number: int, keyword: str) -> list:
            return [None, None, number, keyword, None, None, {"f": OWN(r)[1:]}, "AI玩法优化", "火车运输御敌", None, None, None, None, "张三"]

        write_file(file, make_xlsx([{"name": "26年1月-GOFCN", "rows": [LIVE_HEADER,
            [None, None, 67679, "关键词", None, None, {"f": OLD(2)}, "AI迭代", "题材片头", None, "2D", None, "达治", "靳筱萌"],
            [None, None, 67736, "3D_BJAI片头矿工冰层版CN60141", None, None, {"f": OLD(3)}, "AI片头衔接", "模拟经营", None, None, None, "达治", "靳筱萌"],
            [None, None, 67737, "割资源冰块对抗_竖版", None, None, {"f": OLD(4)}, "AI玩法优化", "搂冰块抢资源", None, None, None, "达治", "靳筱萌"],
            landed_row(5, 67738, "兵临城下塔防_冻鱼_竖版"), landed_row(6, 67739, "兵临城下塔防_企鹅_竖版")]}]))
        now = Snapshot(file)
        # The same files planned again would be appended after themselves.
        again = make_plan(now, finished(names))
        self.assertEqual(again["first_number"], 67740)
        self.assertRaisesRegex(Blocked, "an earlier batch of this request was sent and never verified", prepare, again, now, output_root=base)
        # Files of other names are another batch of materials.
        other = make_plan(now, finished(["GOFCNxxxxx_AI玩法优化_搂冰块抢资源_割资源冰块对抗_横版"]))
        self.assertEqual(prepare(other, now, output_root=base)["status"], "prepared")

    def test_supplier_is_reviewer(self) -> None:
        """A supplier who is also the 创意者脚本审核人 leaves the 设计师供应商 cell empty; the request, CLS and the message keep the person."""
        snapshot = live_sheet()
        own = {"groups": [{**finished(["GOFCNxxxxx_AI玩法优化_火车运输御敌_兵临城下塔防_冻鱼_竖版"])["groups"][0], "supplier": "张三"}]}
        plan = make_plan(snapshot, own)
        self.assertEqual([plan["rows"][0]["values"]["M5"], plan["rows"][0]["values"]["N5"], plan["groups"][0]["request"]["supplier"]], [None, "张三", "张三"])
        self.assertFalse(any(d["field"] == "supplier" for d in plan["dropdowns"]), "no supplier option is added for a cell left empty")
        ticketed = make_plan(snapshot, {"groups": [derived(quantity=1, primary_tag="AI生成", secondary_tag="设定3.0", supplier="张三")]})
        self.assertIsNone(ticketed["rows"][0]["values"]["M5"])
        self.assertRegex(compose_message(ticketed["groups"], [{"group": "01", "step": "draft_saved", "evidence": {}}])["message"], r"\n供应商：张三\n")
        # The column is written in one go: one group's cell empty and another's filled is two appends.
        self.assertRaisesRegex(Blocked, "the cell stays empty when the supplier is the reviewer", make_plan, snapshot, {"groups": [
            derived(quantity=1, primary_tag="AI生成", secondary_tag="设定3.0", supplier="张三"),
            derived(quantity=1, primary_tag="AI生成", secondary_tag="设定3.0", script_url="https://centurygames.feishu.cn/docx/Other123")]})

    def test_pending_supplier(self) -> None:
        """A requirement without a 设计师供应商 leaves its cell empty; the group message says 待定供应商."""
        plan = make_plan(live_sheet(), {"groups": [derived(quantity=1, primary_tag="AI生成", secondary_tag="设定3.0", supplier="")]})
        self.assertIsNone(plan["rows"][0]["values"]["M5"])
        self.assertFalse(any(d["field"] == "supplier" for d in plan["dropdowns"]))
        rest = {k: v for k, v in derived(quantity=1, primary_tag="AI生成", secondary_tag="设定3.0").items() if k != "supplier"}
        self.assertEqual(normalize_request({"groups": [rest]})["groups"][0]["supplier"], "")
        sent = compose_message(plan["groups"], [{"group": "01", "step": "draft_saved", "evidence": {}}])
        self.assertRegex(sent["message"], r"\n供应商：待定供应商\n")


if __name__ == "__main__":
    unittest.main()
