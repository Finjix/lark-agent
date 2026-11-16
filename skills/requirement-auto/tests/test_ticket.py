"""Tickets put on rows appended without one, rows taken over from the sheet, the batch list (gof-ticket.test.ts)."""

from __future__ import annotations

import base64
import copy
import json
import os
import re
import shutil
import unittest

from tests.helpers import fixture, make_xlsx, temp_dir, write_file

from gof.batch import ADOPTED, adopt, attach_ticket, csv_text, list_batches, message, NO_TICKET, prepare, record, report, status, verify
from gof.cli import main
from gof.cls import operation, parse_csv
from gof.plan import make_plan
from gof.request import normalize_request
from gof.snapshot import Snapshot
from gof.util import Blocked, TARGET, digest, read_json, sha256, write_json_new

CATALOG = read_json(fixture("catalog.json"))
REQUEST = read_json(os.path.join(fixture("requests"), "basic.json"))
OBSERVATIONS = {"logged_in": True, "can_create": True, "unpublished": True, "save_result": "ok", "required_complete": True}
LINK = "https://centurygames.feishu.cn/docx/ABC123def"
TICKET = {"script_url": LINK, "cls": {"direction": "方向一", "task_type": "creative_a"}}


def tmp() -> str:
    return temp_dir("gof-ticket-")


def reason(fn) -> str:
    try:
        fn()
    except Blocked as error:
        return error.message
    return "no error"


def reserved() -> dict:
    """The basic request with numbers reserved only: no script link and no CLS fields (金莉's "帮我占 15 条编号")."""
    rows = copy.deepcopy(REQUEST)
    for g in rows["groups"]:
        g.pop("cls", None)
        g.pop("script_url", None)
        g["material_name"] = re.sub(r"^GOFCN[0-9]+_", "", g["material_name"])
    return rows


def appended(test: unittest.TestCase, root: str | None = None) -> tuple[str, object]:
    base = Snapshot(fixture("base.xlsx"))
    plan = make_plan(base, reserved())
    # The same rows as the full requirement: what is appended does not depend on the ticket.
    test.assertEqual(json.dumps(plan["rows"]), json.dumps(make_plan(base, REQUEST)["rows"]))
    result = prepare(plan, base, output_root=root or tmp())
    test.assertEqual(result["csv"], [])
    directory = result["batch"]

    def verify_now() -> None:
        record(directory, "append_started", None)
        record(directory, "append_verified", None, {}, Snapshot(fixture("applied-complete.xlsx")))

    return directory, verify_now


def read_text(file: str) -> str:
    with open(file, encoding="utf-8", newline="") as handle:
        return handle.read()


class TicketTests(unittest.TestCase):
    def test_no_document(self) -> None:
        """A requirement without a document says 无 as its 脚本内容, in the CSV and in the CLS check."""
        none = copy.deepcopy(REQUEST)
        none["groups"][0]["script_url"] = " 无 "
        self.assertEqual(normalize_request(none)["groups"][0]["script_url"], "无")
        base = Snapshot(fixture("base.xlsx"))
        directory = prepare(make_plan(base, none), base, output_root=tmp())["batch"]
        csv = parse_csv(read_text(os.path.join(directory, read_json(os.path.join(directory, "manifest.json"))["plan"]["groups"][0]["csv"]))[1:])
        self.assertEqual([r[1] for r in csv[1:3]], ["无", "同上"])
        record(directory, "append_started", None)
        record(directory, "append_verified", None, {}, Snapshot(fixture("applied-complete.xlsx")))
        record(directory, "cls_started", "01")
        self.assertEqual(operation(directory, "01", "save_draft", CATALOG, OBSERVATIONS)["expected"]["link"], "无")
        # Anything else still has to be a Feishu document.
        none["groups"][0]["script_url"] = "没有"
        self.assertEqual(reason(lambda: normalize_request(none)), "脚本链接必须是当前确认的飞书文档；没有文档时写「无」")

    def test_ticket_afterwards(self) -> None:
        """Rows appended without a ticket get one afterwards, and then go through the CLS stage like any group."""
        directory, verify_now = appended(self)
        self.assertEqual(reason(lambda: attach_ticket(directory, "01", TICKET, CATALOG, True)), "这一批的行还没有追加并验收，不能补需求单")
        verify_now()
        self.assertEqual(reason(lambda: record(directory, "cls_started", "01")), NO_TICKET)

        # The preview is what the user approves; it writes nothing.
        preview = attach_ticket(directory, "01", TICKET, CATALOG, True)
        names = read_json(os.path.join(directory, "manifest.json"))["plan"]["groups"][0]["names"]
        self.assertEqual(preview, {
            "status": "ticket_preview", "group": "01", "requirement_name": f"{names[0].split('_')[0]}-{names[1].split('_')[0]}", "quantity": 2,
            "first": names[0], "last": names[1], "script_url": LINK, "supplier": "供应商丙", "reviewer": "张三",
            "cls_names": {"direction": "方向一", "task_type": "creative_a", "supplier": "丙公司（供应商丙）", "creator": "张三"},
            "native_defaults": {"currency": "cny", "price": 0}, "message_kind": "玩法*2"})
        self.assertEqual([f for f in os.listdir(directory) if f.endswith(".csv")], [])
        self.assertEqual(status(directory)["groups"]["01"], [])

        # Only the link and the CLS fields can be added; the rows stay as they were appended.
        self.assertEqual(reason(lambda: attach_ticket(directory, "01", {**TICKET, "supplier": "供应商甲"}, CATALOG)), "需求单只接受 script_url、cls、label")
        self.assertEqual(reason(lambda: attach_ticket(directory, "01", {**TICKET, "cls": {}}, CATALOG)), "缺少 CLS 字段（创意方向、任务类型）")
        self.assertEqual(reason(lambda: attach_ticket(directory, "01", {"cls": TICKET["cls"]}, CATALOG)), "缺少脚本内容：需求文档链接，没有文档时写「无」")
        self.assertEqual(reason(lambda: attach_ticket(directory, "01", {**TICKET, "script_url": "https://example.com/x"}, CATALOG)),
                         "脚本链接必须是当前确认的飞书文档；没有文档时写「无」")
        self.assertEqual(reason(lambda: attach_ticket(directory, "01", {**TICKET, "cls": {"direction": "方向二", "task_type": "creative_a"}}, CATALOG)),
                         "创意方向 不存在或匹配不唯一，需在计划前澄清")

        # Attached, and corrected once more before the CLS stage: the last one counts.
        first = attach_ticket(directory, "01", {**TICKET, "label": "迭代"}, CATALOG)
        self.assertEqual(first["status"], "ticket_attached")
        second = attach_ticket(directory, "01", {**TICKET, "script_url": "无", "label": "片头"}, CATALOG)
        self.assertNotEqual(second["csv"], first["csv"])
        self.assertEqual(read_text(second["csv"]), csv_text(names, "无"))
        record(directory, "cls_started", "01")
        self.assertEqual(reason(lambda: attach_ticket(directory, "01", TICKET, CATALOG)), "这一组已开始建单，需求单不能再改")

        # The CLS operations take the attached ticket: its link (here 无) and its CSV.
        save = operation(directory, "01", "save_draft", CATALOG, OBSERVATIONS)
        self.assertEqual([save["payload"]["name"], save["expected"]["link"], save["expected"]["names"]], [preview["requirement_name"], "无", names])
        op_path = os.path.join(directory, "save-operation.json")
        write_file(op_path, json.dumps(save, ensure_ascii=False))
        record(directory, "cls_api_attempt", "01", {"action": "save_draft", "operation_id": save["operation_id"], "observations": OBSERVATIONS})
        parent = {"id": 5001, "status": -2, "user_id": save["account_id"], "for_internal": 0, "sub_scripts": []}
        for key, value in save["payload"].items():
            if key != "asset_data":
                parent[key] = None if value == "" and key.endswith("_date") else value
        receipt = os.path.join(directory, "save-receipt.json")
        write_file(receipt, json.dumps({"schema": 1, "transport": "cls_api", "status": "verified", "unpublished": True, "parent": parent,
                                        "operation_id": save["operation_id"], "action": "save_draft",
                                        "calls": [{"method": "POST", "path": "/api/workflow/script/save", "http_status": 200, "business_code": 0},
                                                  {"method": "GET", "path": "/api/workflow/script/5001", "http_status": 200, "business_code": 0}]}, ensure_ascii=False))
        record(directory, "draft_saved", "01", {"confirmed": True, "unpublished": True, "transport": "cls_api", "draft_id": 5001, "operation": op_path,
                                                "receipt": receipt, "url": "https://creative.centurygames.cn/requirement/5001/edit-outer"})
        imported = operation(directory, "01", "import_scripts", CATALOG, {**OBSERVATIONS, "draft_saved": True, "import_enabled": True, "import_would_publish": False})
        self.assertEqual(base64.b64decode(imported["csv"]["base64"]).decode("utf-8"), csv_text(names, "无"))
        self.assertEqual(imported["csv"]["filename"], os.path.basename(second["csv"]))

        # The group message and the report know the group has a ticket now, with the label it was given.
        self.assertIn("（片头*2）\n供应商：供应商丙", message(directory)["message"])
        self.assertEqual(report(directory)["groups"][0]["csv"], second["csv"])
        # The other group still has none.
        self.assertEqual(message(directory)["skipped_groups"], ["02"])

    def test_refused_ticket(self) -> None:
        """A ticket is refused for a group approved with one, and a CSV changed afterwards is not taken."""
        base = Snapshot(fixture("base.xlsx"))
        full = prepare(make_plan(base, REQUEST), base, output_root=tmp())["batch"]
        record(full, "append_started", None)
        record(full, "append_verified", None, {}, Snapshot(fixture("applied-complete.xlsx")))
        self.assertEqual(reason(lambda: attach_ticket(full, "01", TICKET, CATALOG, True)), "这一组获批时已带需求单，按原计划建单")

        directory, verify_now = appended(self)
        verify_now()
        given = {**TICKET, "script_url": "无"}
        attached = attach_ticket(directory, "02", given, CATALOG)
        recorded = {**given, "csv": os.path.basename(attached["csv"]), "csv_sha256": sha256(read_text(attached["csv"]))}
        # The ticket's CSV is taken while the file is what the ticket recorded …
        self.assertEqual(record(directory, "ticket_attached", "02", recorded)["status"], "recorded")
        # … and no longer once it has changed: the same record is refused.
        with open(attached["csv"], "a", encoding="utf-8") as handle:
            handle.write("x")
        self.assertRegex(reason(lambda: record(directory, "ticket_attached", "02", recorded)), "需求单的 CSV 与本组的行不符")

    def test_list(self) -> None:
        """The batches of the user are listed newest first, and found by a material number."""
        root = tmp()
        directory, verify_now = appended(self, root)
        verify_now()
        attach_ticket(directory, "02", TICKET, CATALOG)
        listed = list_batches(root)
        self.assertEqual(len(listed["batches"]), 1)
        only = listed["batches"][0]
        self.assertEqual([[g["group"], g["quantity"], g["ticket"], g["steps"]] for g in only["groups"]], [["01", 2, False, []], ["02", 1, True, ["ticket_attached"]]])
        self.assertTrue(only["append_verified"])
        self.assertEqual(len(list_batches(root, only["first_number"])["batches"]), 1)
        self.assertEqual(list_batches(root, only["last_number"] + 1)["batches"], [])
        # From the command line, and a ticket preview through it.
        ticket_file = write_file(os.path.join(root, "ticket.json"), json.dumps(TICKET))
        answer = main(["batch", "ticket", "--batch", directory, "--group", "01", "--ticket", ticket_file, "--catalog", fixture("catalog.json"), "--preview"])
        self.assertEqual(answer["code"], 0, answer["result"])
        self.assertEqual(answer["result"]["status"], "ticket_preview")

    def test_adopt(self) -> None:
        """Rows already in the sheet but in no batch are taken over as they are, and then get their ticket like reserved rows."""
        root = tmp()
        sheet = Snapshot(fixture("applied-complete.xlsx"))
        self.assertEqual(reason(lambda: adopt(sheet, [1011, 1014], output_root=root)), "进度表里没有这些编号的行：GOFCN1014")
        self.assertEqual(reason(lambda: adopt(sheet, [1011, 1012, 1013], output_root=root)),
                         "这些行的设计师供应商不一致（供应商丙、供应商甲）：用 --supplier 指明这张需求单的设计师供应商")
        self.assertEqual(os.listdir(root), [])

        # In any order; the sheet decides the names, the supplier and the reviewer.
        adopted = adopt(sheet, [1012, 1011], output_root=root)
        directory = adopted["batch"]
        self.assertEqual(adopted, {"status": "rows_adopted", "batch": directory, "worksheet": "GOFCN创意进度 26年3月", "quantity": 2, "first_number": 1011,
                                   "last_number": 1012, "first": "GOFCN1011_玩法_卖点_视频_新爆点", "last": "GOFCN1012_玩法_卖点_视频_新爆点",
                                   "supplier": "供应商丙", "reviewer": "张三"})
        self.assertTrue(status(directory)["append_verified"])
        self.assertEqual(reason(lambda: adopt(sheet, [1012, 1013], output_root=root)), f"GOFCN1012 已在批次 {directory} 里：用这一批继续（gof batch list --number 1012）")

        # Nothing to append or read back.
        self.assertEqual(reason(lambda: record(directory, "append_started", None)), ADOPTED)
        self.assertEqual(reason(lambda: verify(directory, sheet)), ADOPTED)

        # The ticket, the CLS operations and the message as for rows reserved first.
        preview = attach_ticket(directory, "01", TICKET, CATALOG, True)
        self.assertEqual([preview["requirement_name"], preview["quantity"], preview["message_kind"], preview["cls_names"]["supplier"]],
                         ["GOFCN1011-GOFCN1012", 2, "玩法*2", "丙公司（供应商丙）"])
        attached = attach_ticket(directory, "01", TICKET, CATALOG)
        self.assertEqual(read_text(attached["csv"]), csv_text([adopted["first"], adopted["last"]], LINK))
        record(directory, "cls_started", "01")
        save = operation(directory, "01", "save_draft", CATALOG, OBSERVATIONS)
        self.assertEqual([save["payload"]["name"], save["expected"]["names"]], ["GOFCN1011-GOFCN1012", [adopted["first"], adopted["last"]]])
        record(directory, "handoff", "01", {"reason": "用户在 CLS 手动建单"})
        self.assertIn("（玩法*2）\n供应商：供应商丙", message(directory)["message"])
        listed = list_batches(root, 1011)["batches"][0]
        self.assertEqual([listed["adopted"], listed["groups"][0]["ticket"]], [True, True])

        # A stop is resumed by the user's word, with nothing to read back.
        record(directory, "blocked", None, {"reason": "暂停"})
        record(directory, "resumed", None, {"confirmed": True, "reason": "继续", "cls_reconciled": True})
        self.assertFalse(status(directory)["blocked"])

        # Rows of different people: the request names who the ticket is for. From the command line.
        other = tmp()
        answer = main(["batch", "adopt", "--numbers", "GOFCN1011-GOFCN1013", "--snapshot", sheet.path, "--supplier", "供应商甲", "--reviewer", "李四", "--output-root", other])
        self.assertEqual(answer["code"], 0, answer["result"])
        result = answer["result"]
        self.assertEqual([result["quantity"], result["supplier"], result["reviewer"]], [3, "供应商甲", "李四"])
        self.assertEqual(attach_ticket(result["batch"], "01", TICKET, CATALOG, True)["message_kind"], "玩法*2，画面*1")

    def test_prepared_rows_adoptable(self) -> None:
        """Rows of a batch only prepared can still be taken over; once its rows are verified, the batch keeps them."""
        sheet = Snapshot(fixture("applied-complete.xlsx"))
        # The batch prepared for the rows is in the folder adopt looks in: it sent nothing, so it holds nothing.
        prepared_root = tmp()
        appended(self, prepared_root)
        self.assertEqual(adopt(sheet, [1011], output_root=prepared_root)["status"], "rows_adopted")
        verified_root = tmp()
        _, appended_now = appended(self, verified_root)
        appended_now()
        self.assertRegex(reason(lambda: adopt(sheet, [1011], output_root=verified_root)), r"^GOFCN1011 已在批次 .+ 里：用这一批继续")

    def test_take_over_unverifiable(self) -> None:
        """A batch whose write went out but can never be verified gives its rows to adopt once every cell it wrote is in place, and goes no further."""
        root = tmp()
        directory, _ = appended(self, root)
        # The write went out, its verification failed (a colleague's edit moved the revision during the export), and the batch was blocked.
        record(directory, "append_started", None)
        record(directory, "blocked", None, {"reason": "导出期间出现并发变化"})
        # Then a colleague filled in a cell the plugin leaves empty in a new row: every verification is a conflict from now on.
        now = Snapshot(fixture("applied-occupied-optional.xlsx"))
        verdict = verify(directory, now)
        self.assertEqual([verdict["status"], verdict["matched_cells"], verdict["expected_cells"]], ["conflict", 24, 24])
        # A cell it wrote that is not as written keeps the rows where they are.
        self.assertRegex(reason(lambda: adopt(Snapshot(fixture("applied-bad-value.xlsx")), [1011, 1012], output_root=root)),
                         r"^GOFCN1011 在批次 .+ 里，那一批的写入发出后没有核对通过，表里 D6 和它写的不一样")
        self.assertNotIn("taken_over", status(directory))
        taken = adopt(now, [1011, 1012], output_root=root)
        self.assertEqual([taken["status"], taken["taken_over"], taken["first"]], ["rows_adopted", [directory], "GOFCN1011_玩法_卖点_视频_新爆点"])
        # The old batch goes no further, so the rows cannot get a ticket there as well; the new batch gets theirs.
        self.assertEqual(status(directory)["taken_over"], taken["batch"])
        self.assertRegex(reason(lambda: record(directory, "resumed", None, {"confirmed": True, "reason": "继续"}, now)), r"^这一批的行已由批次 .+ 接管")
        self.assertRegex(reason(lambda: attach_ticket(directory, "01", TICKET, CATALOG, True)), r"^这一批的行已由批次 .+ 接管")
        self.assertEqual(attach_ticket(taken["batch"], "01", TICKET, CATALOG, True)["requirement_name"], "GOFCN1011-GOFCN1012")
        self.assertEqual(next(b for b in list_batches(root, 1011)["batches"] if b["batch"] == directory)["taken_over"], taken["batch"])
        # Its other rows are free to be taken over as well, and appending the same rows again is still refused.
        self.assertEqual(adopt(now, [1013], output_root=root)["status"], "rows_adopted")
        self.assertRaisesRegex(Blocked, "已有一批写入发出、尚未核对", prepare, make_plan(now, reserved()), now, output_root=root)

    def test_gapped_batch(self) -> None:
        """A batch is found by a number it holds, not by one between its first and last; a ticket is for one run of numbers."""
        root = tmp()
        sheet = Snapshot(fixture("applied-complete.xlsx"))
        # A batch an earlier version took over with a gap between its numbers (adopt takes one run of numbers now).
        gapped_request = normalize_request({"groups": [{"supplier": "供应商甲", "reviewer": "李四", "items": [
            {"quantity": 1, "primary_tag": "玩法", "secondary_tag": "卖点", "creative_form": "视频", "keyword": "新爆点"},
            {"quantity": 1, "primary_tag": "画面", "secondary_tag": "体验", "creative_form": "图文", "keyword": "另一个"}]}]})
        names = ["GOFCN1011_玩法_卖点_视频_新爆点", "GOFCN1013_画面_体验_图文_另一个"]
        sheet_name = "GOFCN创意进度 26年3月"
        plan = {"schema": 1, "target": TARGET, "adopted": True, "worksheet": sheet_name, "current_sheet": None, "request": gapped_request,
                "baseline_digest": sheet.fingerprint, "range": None, "quantity": 2, "first_number": 1011, "last_number": 1013,
                "rows": [{"sheet": sheet_name, "row": 6, "number": 1011, "name": names[0], "group": "01"}, {"sheet": sheet_name, "row": 8, "number": 1013, "name": names[1], "group": "01"}],
                "groups": [{"id": "01", "csv": None, "names": names, "rows": [6, 8], "request": gapped_request["groups"][0]}]}
        plan["plan_id"] = digest(plan)[:24]
        gapped = os.path.join(root, plan["plan_id"])
        os.makedirs(os.path.join(gapped, "events"))
        shutil.copyfile(sheet.path, os.path.join(gapped, "baseline.xlsx"))
        with open(os.path.join(gapped, "baseline.xlsx"), "rb") as handle:
            write_json_new(os.path.join(gapped, "manifest.json"), {"plan": plan, "baseline_sha256": sha256(handle.read()), "artifacts": {}})
        record(gapped, "rows_adopted", None, {"snapshot_digest": sheet.fingerprint})
        self.assertEqual([b["batch"] for b in list_batches(root, 1013)["batches"]], [gapped])
        self.assertEqual(list_batches(root, 1012)["batches"], [])
        # 1012 is in no batch: it can be taken over. A run with a gap cannot be one ticket (its name would be GOFCN1011-GOFCN1013).
        self.assertEqual(adopt(sheet, [1012], output_root=root)["status"], "rows_adopted")
        self.assertEqual(reason(lambda: adopt(sheet, [1011, 1013], output_root=tmp(), supplier="供应商甲", reviewer="李四")),
                         "编号要连成一段：GOFCN1011 之后是 GOFCN1013。一张需求单的名称是 GOFCN首号-GOFCN末号，不连续的编号分段各接管一次")

    def test_rows_by_hand(self) -> None:
        """Rows typed by hand are taken over as they are, though a new row could not be named like them."""
        header = ["素材编号", "提炼词", "素材名称", "一级标签", "二级标签", "创意形式", "客户端", "设计师供应商", "创意者脚本审核人"]
        file = write_file(os.path.join(tmp(), "by-hand.xlsx"), make_xlsx([{"name": "26年1月-GOFCN", "rows": [header,
            [67679, "横版/竖版", "GOFCN67679_玩法_卖点_2D_横版/竖版", "玩法", "卖点", "2D", None, "供应商丙", "张三"],
            [67680, "_开头", "GOFCN67680_玩法_卖点_3D_AI__开头", "玩法", "卖点", "3D_AI", None, "供应商丙", "张三"]]}]))
        taken = adopt(Snapshot(file), [67679, 67680], output_root=tmp())
        self.assertEqual([taken["status"], taken["first"], taken["last"]], ["rows_adopted", "GOFCN67679_玩法_卖点_2D_横版/竖版", "GOFCN67680_玩法_卖点_3D_AI__开头"])
        preview = attach_ticket(taken["batch"], "01", TICKET, CATALOG, True)
        self.assertEqual([preview["requirement_name"], preview["message_kind"]], ["GOFCN67679-GOFCN67680", "玩法*2"])
        attached = attach_ticket(taken["batch"], "01", TICKET, CATALOG)
        self.assertEqual(read_text(attached["csv"]), csv_text([taken["first"], taken["last"]], LINK))
        # A new row still has to be one a name can be made of.
        self.assertRaisesRegex(Blocked, r"不能含 / 或", normalize_request,
                               {"groups": [{"supplier": "供应商丙", "reviewer": "张三", "primary_tag": "玩法", "secondary_tag": "卖点", "quantity": 1, "keyword": "横版/竖版"}]})


if __name__ == "__main__":
    unittest.main()
