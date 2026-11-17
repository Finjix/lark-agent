"""CLS: operations and receipts, the online commands, the login guards, `login check` and the profile lock
(gof-cls.test.ts, cls-online.test.ts, cls-guard.test.ts, login-check.test.ts, browser-lock.test.ts)."""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import threading
import time
import unittest
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from tests.fakes import FakeCls, ScriptedOnline, session_of
from tests.helpers import fixture, py_script, temp_dir, write_file

from gof import js
from gof.batch import message, prepare, record, report, status
from gof.chromium import lock_profile, open_browser
from gof.cli import main
from gof.cls import catalog_choices, cls_preview, operation, requirement_name
from gof.cls_adapter import ClsAdapter, ClsError
from gof.cls_guard import check_pin, guard_online, read_pin, write_qr
from gof.cls_session import cls_account_file, cls_profile_dir
from gof.feishu import Bridge
from gof.paths import resolve_data_paths
from gof.plan import make_plan
from gof.shots_online import draft_of
from gof.snapshot import Snapshot
from gof.util import Blocked, read_json, write_json_new
from gof.web import FetchError, Response, fetch as real_fetch

GOLDEN = read_json(fixture("golden.json"))["cls"]
CATALOG = read_json(fixture("catalog.json"))
REQUEST = read_json(os.path.join(fixture("requests"), "basic.json"))
OBSERVATIONS = {"logged_in": True, "can_create": True, "unpublished": True, "save_result": "ok", "required_complete": True}


def tmp() -> str:
    return temp_dir("gof-cls-")


def reason(fn) -> str:
    try:
        fn()
    except Blocked as error:
        return error.message
    return "no error"


def group_ready() -> str:
    base = Snapshot(fixture("base.xlsx"))
    directory = prepare(make_plan(base, REQUEST), base, output_root=tmp())["batch"]
    record(directory, "append_started", None)
    record(directory, "append_verified", None, {}, Snapshot(fixture("applied-complete.xlsx")))
    record(directory, "cls_started", "01")
    return directory


def receipt_for(op: dict, **over: object) -> dict:
    parent = {"id": 5001, "status": -2, "user_id": op["account_id"], "for_internal": 0, "sub_scripts": [], "number": "GOFCN20260100009"}
    for key, value in op["payload"].items():
        if key != "asset_data":
            parent[key] = None if value == "" and key.endswith("_date") else value
    return {"schema": 1, "transport": "cls_api", "status": "verified", "unpublished": True, "parent": parent, "operation_id": op["operation_id"], "action": op["action"],
            "calls": [{"method": "POST", "path": "/api/workflow/script/save", "http_status": 200, "business_code": 0},
                      {"method": "GET", "path": "/api/workflow/script/5001", "http_status": 200, "business_code": 0}], **over}


class OperationTests(unittest.TestCase):
    def test_preview(self) -> None:
        """The CLS preview builds the payloads the original built, and refuses what it refused."""
        preview = cls_preview(REQUEST, CATALOG)
        # The CLS names everything was matched to are new since the port; the rest is unchanged.
        self.assertEqual([g["cls_names"] for g in preview["groups"]], [
            {"direction": "方向一", "task_type": "creative_a", "supplier": "丙公司（供应商丙）", "creator": "张三"},
            {"direction": "方向一", "task_type": "creative_a", "supplier": "供应商甲", "creator": "李四"}])
        self.assertEqual(js.canonical({**preview, "groups": [{k: v for k, v in g.items() if k != "cls_names"} for g in preview["groups"]]}), js.canonical(GOLDEN["preview"]))
        variants = {
            "no-create": lambda c: c.update(can_create=False),
            "no-game-permission": lambda c: c["account"].update(game_ids=[4]),
            "unknown-supplier": lambda c: c["users"].pop(1),
            "dup-direction": lambda c: c["directions"].append({"id": 23, "name": "方向一", "game_id": 3}),
            "secret": lambda c: c.update(authorization="x"),
        }
        for name, change in variants.items():
            broken = copy.deepcopy(CATALOG)
            change(broken)
            self.assertEqual(reason(lambda: cls_preview(REQUEST, broken)), GOLDEN[f"preview-{name}"]["reason"], name)
        quote = copy.deepcopy(REQUEST)
        quote["groups"][1]["cls"]["quotation"] = "abc"
        self.assertEqual(reason(lambda: cls_preview(quote, CATALOG)), GOLDEN["preview-bad-quote"]["reason"])

    def test_receipts(self) -> None:
        """An operation and its receipts are checked as strictly as before."""
        directory = group_ready()
        op_path = os.path.join(directory, "save-operation.json")
        op = operation(directory, "01", "save_draft", CATALOG, OBSERVATIONS)
        write_json_new(op_path, op)
        self.assertRegex(op["operation_id"], r"^[0-9a-f]{64}$")
        body = {k: op[k] for k in ("schema", "action", "group", "account_id", "payload", "native_defaults", "expected")}
        self.assertEqual(js.canonical(body), js.canonical(GOLDEN["operation_save_body"]))
        self.assertEqual(reason(lambda: operation(directory, "01", "import_scripts", CATALOG, OBSERVATIONS)), GOLDEN["operation_import_before_draft"]["reason"])

        def draft(name: str, receipt: dict, evidence: dict | None = None) -> str:
            file = write_file(os.path.join(directory, f"receipt-{name}.json"), json.dumps(receipt, ensure_ascii=False))
            return reason(lambda: record(directory, "draft_saved", "01", {"confirmed": True, "unpublished": True, "transport": "cls_api", "draft_id": 5001,
                                                                        "operation": op_path, "receipt": file,
                                                                        "url": "https://creative.centurygames.cn/requirement/5001/edit-outer", **(evidence or {})}))

        self.assertEqual(draft("no-attempt", receipt_for(op)), GOLDEN["draft-no-attempt"]["reason"])
        record(directory, "cls_api_attempt", "01", {"action": "save_draft", "operation_id": op["operation_id"], "observations": OBSERVATIONS})
        parent = receipt_for(op)["parent"]
        cases = [
            ("wrong-operation", receipt_for(op, operation_id="0" * 64), None),
            ("wrong-name", receipt_for({**op, "payload": {**op["payload"], "name": "别的"}}), None),
            ("scripts-in-draft", receipt_for(op, parent={**parent, "sub_scripts": [{"id": 1}]}), None),
            ("bad-call", receipt_for(op, calls=[{"method": "POST", "path": "/api/workflow/script/publish", "http_status": 200, "business_code": 0}]), None),
            ("not-draft", receipt_for(op, parent={**parent, "status": 1}), None),
            ("wrong-url", receipt_for(op), {"url": "https://creative.centurygames.cn/requirement/9/edit-outer"}),
            ("wrong-number", receipt_for(op), {"requirement_number": "GOFCN20260100001"}),
            ("wrong-price", receipt_for(op, parent={**parent, "price": 3}), None),
        ]
        for name, receipt, evidence in cases:
            self.assertEqual(draft(name, receipt, evidence), GOLDEN[f"draft-{name}"]["reason"], name)
        with self.assertRaises(FileNotFoundError):
            record(directory, "draft_saved", "01", {"confirmed": True, "unpublished": True, "transport": "cls_api", "draft_id": 5001, "operation": op_path,
                                                    "receipt": os.path.join(directory, "receipt-none.json"), "url": "https://creative.centurygames.cn/requirement/5001/edit-outer"})
        self.assertEqual(draft("ok", receipt_for(op), {"requirement_number": "GOFCN20260100009"}), "no error")
        self.assertEqual(status(directory)["groups"]["01"], GOLDEN["steps_group_01"])
        # The group with the saved draft is in the message with its official number; the other one has no ticket yet and is left out.
        sent = message(directory)
        self.assertIn("需求编号：GOFCN20260100009（玩法*2）\n供应商：供应商丙\n创意人：张三\n辛苦安排一下~", sent["message"])
        self.assertEqual([sent["skipped_groups"], sent["missing_numbers"]], [["02"], []])
        full = report(directory)
        self.assertEqual(full["groups"][0]["draft_saved"]["draft_id"], 5001)
        # The user is handed the progress sheet and each saved draft to look at.
        self.assertEqual(full["links"], {"progress_sheet": "https://centurygames.feishu.cn/wiki/GH7cwIxzZi3gVdk2s3tc1dDbnQc",
                                         "cls": [{"group": "01", "draft": "https://creative.centurygames.cn/requirement/5001/edit-outer"}, {"group": "02", "draft": None}]})
        # The operation file is never replaced.
        self.assertRaisesRegex(FileExistsError, "EEXIST", write_json_new, op_path, op)

    def test_adapter_checks_before_sending(self) -> None:
        """The adapter checks an operation built here (same canonical form and digest) before it sends anything."""
        op = operation(group_ready(), "01", "save_draft", CATALOG, OBSERVATIONS)

        def no_network(*args, **kwargs):
            raise FetchError("no network")

        adapter = ClsAdapter("https://creative.centurygames.cn", "Bearer test", fetch=no_network)
        self.assertRaisesRegex(ClsError, "操作文件摘要错误", adapter.create_draft, {**op, "operation_id": "0" * 64})
        self.assertRaisesRegex(ClsError, "CLS 读取失败", adapter.create_draft, op)

    def test_gateway_page(self) -> None:
        """A write answered 2xx without a business code (a gateway page) is an unknown result, not a refusal."""
        cls = FakeCls(CATALOG)
        try:
            op = operation(group_ready(), "01", "save_draft", CATALOG, OBSERVATIONS)

            def fetcher(url, method="GET", **kwargs):
                if method == "POST":
                    return Response(200, {}, b"<html>ok</html>")
                return real_fetch(url, method=method, **kwargs)

            adapter = ClsAdapter(cls.origin, "Bearer good", fetch=fetcher)
            with self.assertRaises(ClsError) as caught:
                adapter.create_draft(op)
            self.assertEqual(caught.exception.kind, "unknown")
            self.assertEqual(caught.exception.operation_id, op["operation_id"])
            self.assertRegex(caught.exception.message, r"写入结果未知（HTTP 200，响应没有业务码）")
        finally:
            cls.close()

    def test_command_line_operation(self) -> None:
        """The cls command line builds and saves an operation file."""
        directory = group_ready()
        obs = write_file(os.path.join(directory, "obs.json"), json.dumps(OBSERVATIONS))
        out = os.path.join(directory, "op.json")
        args = ["cls", "--batch", directory, "--group", "01", "--action", "save_draft", "--catalog", fixture("catalog.json"), "--observations", obs, "--output", out]
        answer = main(args)
        self.assertEqual(answer["code"], 0, answer["result"])
        self.assertEqual(answer["result"]["status"], "operation_ready")
        self.assertEqual(read_json(out)["operation_id"], answer["result"]["operation_id"])
        again = main(args)
        self.assertEqual([again["code"], again["result"]["reason"]], [2, GOLDEN["operation_exists"]["reason"]])

    def test_requirement_name(self) -> None:
        """The CLS requirement name is the first and last material number of the group, and is never taken from the request."""
        self.assertEqual(requirement_name(["GOFCN67294_a", "GOFCN67295_a", "GOFCN67299_a"]), "GOFCN67294-GOFCN67299")
        self.assertEqual(requirement_name(["GOFCN67294_a"]), "GOFCN67294")
        self.assertEqual(reason(lambda: requirement_name(["无编号"])), "素材名称缺少 GOFCN 编号，无法生成需求名称")
        given = copy.deepcopy(REQUEST)
        given["groups"][0]["cls"]["requirement_name"] = "手填"
        self.assertEqual(reason(lambda: cls_preview(given, CATALOG)), "CLS 需求名称由该组素材编号生成（GOFCN首号-GOFCN末号），不接受输入")

    def test_short_names(self) -> None:
        """CLS names are found from the short names the progress sheet uses, and exactly one may match."""
        wide = copy.deepcopy(CATALOG)
        wide["users"].extend([
            {"id": 21, "name": "北京柒曦文化传媒有限公司（丁杰）", "roles": ["outsource"], "status": 1},
            {"id": 22, "name": "某某工作室（个体工商户）-杨燃", "roles": ["outsource"], "status": 1},
            {"id": 23, "name": "达治（武汉）文化传媒有限公司", "roles": ["outsource"], "status": 1},
            {"id": 24, "name": "长沙廿拾一文化传媒有限公司", "roles": ["outsource"], "status": 1},
            {"id": 25, "name": "冯彦蓉-实习生", "roles": ["creative_plan"], "status": 1},
            {"id": 26, "name": "张三丰", "roles": ["creative_plan"], "status": 1},
            {"id": 27, "name": "李达甲", "roles": ["creative_plan"], "status": 1},
            {"id": 28, "name": "李达乙", "roles": ["creative_plan"], "status": 1},
            {"id": 29, "name": "丁杰-离职", "roles": ["outsource"], "status": 0},
            {"id": 51, "name": "待定供应商", "roles": ["outsource"], "status": 1}])

        def names(supplier: str, reviewer: str, cls: dict | None = None) -> dict:
            r = copy.deepcopy(REQUEST)
            r["groups"][0].update(supplier=supplier, reviewer=reviewer)
            r["groups"][0]["cls"].update(cls or {})
            return cls_preview(r, wide)["groups"][0]["cls_names"]

        self.assertEqual(names("丁杰", "冯彦蓉"), {"direction": "方向一", "task_type": "creative_a", "supplier": "北京柒曦文化传媒有限公司（丁杰）", "creator": "冯彦蓉-实习生"})
        self.assertEqual(names("杨燃", "张三")["supplier"], "某某工作室（个体工商户）-杨燃")
        self.assertEqual(names("达治", "张三")["supplier"], "达治（武汉）文化传媒有限公司")
        # A requirement without a 设计师供应商 goes to CLS's 待定供应商.
        self.assertEqual(names("", "张三")["supplier"], "待定供应商")
        # The exact name wins over a longer one that contains it.
        self.assertEqual(names("供应商甲", "张三")["creator"], "张三")
        self.assertEqual(reason(lambda: names("供应商甲", "李达")), "创意人 不存在或匹配不唯一，需在计划前澄清")
        self.assertEqual(reason(lambda: names("廿十一", "张三")), "供应商 不存在或匹配不唯一，需在计划前澄清")
        # When the spelling differs, the request names the CLS user exactly; then nothing else is tried.
        self.assertEqual(names("廿十一", "李达", {"supplier": "长沙廿拾一文化传媒有限公司", "creator": "李达甲"})["creator"], "李达甲")
        self.assertEqual(reason(lambda: names("丁杰", "张三", {"supplier": "丁杰"})), "供应商 不存在或匹配不唯一，需在计划前澄清")
        r = copy.deepcopy(REQUEST)
        r["groups"][0]["cls"]["supplier"] = "长沙廿拾一文化传媒有限公司"
        payload = cls_preview(r, wide)["groups"][0]["payload"]
        self.assertEqual(payload["third_user_id"], 24)
        self.assertTrue("supplier" not in payload and "creator" not in payload)

    def test_catalog_choices(self) -> None:
        """The catalog choices are the GOFCN directions, creative task types, active suppliers and active people."""
        self.assertEqual(catalog_choices(CATALOG), {"directions": ["方向一"], "task_types": ["creative_a"], "suppliers": ["丙公司（供应商丙）", "供应商甲"], "people": ["张三", "李四"]})


def ready_batch() -> str:
    base = Snapshot(fixture("base.xlsx"))
    directory = prepare(make_plan(base, REQUEST), base, output_root=temp_dir("cls-online-"))["batch"]
    record(directory, "append_started", None)
    record(directory, "append_verified", None, {}, Snapshot(fixture("applied-complete.xlsx")))
    record(directory, "cls_started", "01")
    return directory


def write(directory: str, name: str, value: object) -> str:
    return write_file(os.path.join(directory, name), json.dumps(value, ensure_ascii=False))


class OnlineTests(unittest.TestCase):
    def ok(self, args: list[str], via: object) -> dict:
        answer = main(args, None, via)
        self.assertEqual(answer["code"], 0, answer["result"])
        return answer["result"]

    def built_save(self, cls: FakeCls, directory: str, via: object) -> tuple[str, str, dict]:
        live = os.path.join(directory, "live-catalog.json")
        self.ok(["cls", "catalog", "--output", live], via)
        observations = os.path.join(directory, "obs.json")
        self.ok(["cls", "observe", "--action", "save_draft", "--output", observations], via)
        save = os.path.join(directory, "save-operation.json")
        built = self.ok(["cls", "--batch", directory, "--group", "01", "--action", "save_draft", "--catalog", live, "--observations", observations, "--output", save], via)
        return live, save, {"built": built, "observations": observations}

    def test_whole_group(self) -> None:
        """A whole group goes through the commands: options, observations, draft, import, and each receipt is accepted by the batch."""
        cls = FakeCls(CATALOG)
        try:
            directory = ready_batch()
            via = session_of(cls.origin)
            live = os.path.join(directory, "live-catalog.json")
            saved = self.ok(["cls", "catalog", "--output", live], via)
            self.assertEqual([saved["status"], saved["can_create"], saved["can_import"]], ["catalog_saved", True, True])
            rest = {k: v for k, v in read_json(live).items() if k != "observed_at"}
            self.assertEqual(js.canonical(rest), js.canonical(CATALOG), "what the adapter reads from the API is the catalog the operations are built from")

            # 1. the draft
            observations = os.path.join(directory, "obs-save.json")
            self.ok(["cls", "observe", "--action", "save_draft", "--output", observations], via)
            save = os.path.join(directory, "save-operation.json")
            built = self.ok(["cls", "--batch", directory, "--group", "01", "--action", "save_draft", "--catalog", live, "--observations", observations, "--output", save], via)
            attempt = write(directory, "attempt-save.json", {"action": "save_draft", "operation_id": built["operation_id"], "observations": read_json(observations)})
            self.ok(["batch", "record", "--batch", directory, "--step", "cls_api_attempt", "--group", "01", "--evidence", attempt], via)
            receipt = os.path.join(directory, "receipt-save.json")
            sent = self.ok(["cls", "run", "--batch", directory, "--operation", save, "--output", receipt], via)
            self.assertEqual([sent["status"], sent["draft_id"], sent["requirement_number"]], ["receipt_saved", 5001, "GOFCN20260100009"])
            self.assertEqual(sent["url"], f"{cls.origin}/requirement/5001/edit-outer")
            # The origin of the fake is not CLS's, so the batch refuses the URL the real command would have produced for it: check the real shape.
            draft_evidence = write(directory, "draft-evidence.json", {"confirmed": True, "unpublished": True, "transport": "cls_api", "draft_id": 5001, "operation": save,
                                                                      "receipt": receipt, "url": "https://creative.centurygames.cn/requirement/5001/edit-outer",
                                                                      "requirement_number": "GOFCN20260100009"})
            self.ok(["batch", "record", "--batch", directory, "--step", "draft_saved", "--group", "01", "--evidence", draft_evidence], via)

            # 2. the import into that draft
            obs_import = os.path.join(directory, "obs-import.json")
            seen = self.ok(["cls", "observe", "--action", "import_scripts", "--draft", "5001", "--output", obs_import], via)
            self.assertEqual([seen["import_enabled"], seen["import_would_publish"], seen["unpublished"]], [True, False, True])
            imp = os.path.join(directory, "import-operation.json")
            built_import = self.ok(["cls", "--batch", directory, "--group", "01", "--action", "import_scripts", "--catalog", live, "--observations", obs_import, "--output", imp], via)
            attempt_import = write(directory, "attempt-import.json", {"action": "import_scripts", "operation_id": built_import["operation_id"], "observations": read_json(obs_import)})
            self.ok(["batch", "record", "--batch", directory, "--step", "cls_api_attempt", "--group", "01", "--evidence", attempt_import], via)
            receipt_import = os.path.join(directory, "receipt-import.json")
            imported = self.ok(["cls", "run", "--batch", directory, "--operation", imp, "--output", receipt_import], via)
            self.assertEqual(imported["scripts"], 2)
            names = read_json(imp)["expected"]["names"]
            import_evidence = write(directory, "import-evidence.json", {"confirmed": True, "unpublished": True, "transport": "cls_api", "draft_id": 5001, "operation": imp,
                                                                        "receipt": receipt_import, "url": "https://creative.centurygames.cn/requirement/5001/edit-outer",
                                                                        "quantity": 2, "names": names})
            self.ok(["batch", "record", "--batch", directory, "--step", "scripts_imported", "--group", "01", "--evidence", import_evidence], via)

            # The picture of the draft is taken from the batch's own record: the draft the scripts went into.
            self.assertEqual(draft_of(directory, "01"), {"group": "01", "id": 5001, "url": "https://creative.centurygames.cn/requirement/5001/edit-outer"})
            self.assertEqual(draft_of(directory, None), draft_of(directory, "01"))
            self.assertRaisesRegex(Blocked, "没有分组 99", draft_of, directory, "99")

            # Only Authorization was sent, only to the allowed paths, and nothing was ever published or deleted.
            self.assertTrue(all(r["authorization"] == "Bearer good" for r in cls.requests))
            self.assertEqual([r["path"] for r in cls.requests if r["method"] != "GET"], ["/api/workflow/script/save", "/api/workflow/sub-script/multiple-add"])
            self.assertEqual(cls.drafts[5001]["status"], -2)
        finally:
            cls.close()

    def test_write_once(self) -> None:
        """A write needs its recorded attempt, goes out once, and the same operation is refused the second time."""
        cls = FakeCls(CATALOG)
        try:
            directory = ready_batch()
            via = session_of(cls.origin)
            _, save, extra = self.built_save(cls, directory, via)
            early = main(["cls", "run", "--batch", directory, "--operation", save, "--output", os.path.join(directory, "r1.json")], None, via)
            self.assertRegex(early["result"]["reason"], "cls_api_attempt")
            self.assertEqual(len([r for r in cls.requests if r["method"] == "POST"]), 0)
            attempt = write(directory, "attempt.json", {"action": "save_draft", "operation_id": extra["built"]["operation_id"], "observations": read_json(extra["observations"])})
            self.ok(["batch", "record", "--batch", directory, "--step", "cls_api_attempt", "--group", "01", "--evidence", attempt], via)
            self.ok(["cls", "run", "--batch", directory, "--operation", save, "--output", os.path.join(directory, "r1.json")], via)
            again = main(["cls", "run", "--batch", directory, "--operation", save, "--output", os.path.join(directory, "r2.json")], None, via)
            self.assertEqual(again["code"], 2)
            self.assertRegex(again["result"]["reason"], "已经发出过写请求")
            self.assertEqual(len([r for r in cls.requests if r["method"] == "POST"]), 1)
            # A different account than the one the operation was built for is refused before anything is sent.
            other = session_of(cls.origin, account={"id": 99, "name": "别人"})
            wrong = main(["cls", "run", "--batch", directory, "--operation", save, "--output", os.path.join(directory, "r3.json")], None, other)
            self.assertRegex(wrong["result"]["reason"], "不是同一个")
        finally:
            cls.close()

    def test_lost_answer(self) -> None:
        """A write that gets no answer is reported as unknown with what is needed to read it back, and cannot be sent again."""
        cls = FakeCls(CATALOG)
        try:
            directory = ready_batch()
            via = session_of(cls.origin)
            _, save, extra = self.built_save(cls, directory, via)
            attempt = write(directory, "attempt.json", {"action": "save_draft", "operation_id": extra["built"]["operation_id"], "observations": read_json(extra["observations"])})
            self.ok(["batch", "record", "--batch", directory, "--step", "cls_api_attempt", "--group", "01", "--evidence", attempt], via)
            cls.post_mode = "hang-up"
            lost = main(["cls", "run", "--batch", directory, "--operation", save, "--output", os.path.join(directory, "r1.json")], None, via)
            self.assertEqual(lost["code"], 2)
            self.assertEqual([lost["result"].get("unknown_result"), lost["result"].get("operation_id")], [True, extra["built"]["operation_id"]])
            self.assertFalse(os.path.exists(os.path.join(directory, "r1.json")))
            cls.post_mode = "normal"
            retry = main(["cls", "run", "--batch", directory, "--operation", save, "--output", os.path.join(directory, "r2.json")], None, via)
            self.assertRegex(retry["result"]["reason"], "已经发出过写请求")
            self.assertEqual(len([r for r in cls.requests if r["method"] == "POST"]), 1, "the second run never reached the server")
        finally:
            cls.close()

    def test_read_back(self) -> None:
        """A draft that exists is read back and verified without any write."""
        cls = FakeCls(CATALOG)
        try:
            directory = ready_batch()
            via = session_of(cls.origin)
            _, save, extra = self.built_save(cls, directory, via)
            attempt = write(directory, "attempt.json", {"action": "save_draft", "operation_id": extra["built"]["operation_id"], "observations": read_json(extra["observations"])})
            self.ok(["batch", "record", "--batch", directory, "--step", "cls_api_attempt", "--group", "01", "--evidence", attempt], via)
            self.ok(["cls", "run", "--batch", directory, "--operation", save, "--output", os.path.join(directory, "r1.json")], via)
            before = len([r for r in cls.requests if r["method"] == "POST"])
            draft = self.ok(["cls", "read-draft", "--id", "5001"], via)
            self.assertEqual(draft["draft"]["status"], -2)
            verified = self.ok(["cls", "verify", "--batch", directory, "--operation", save, "--draft", "5001", "--output", os.path.join(directory, "r-verify.json")], via)
            self.assertEqual(verified["status"], "receipt_saved")
            self.assertEqual(len([r for r in cls.requests if r["method"] == "POST"]), before)
            self.assertEqual([f"{c['method']} {c['path']}" for c in read_json(os.path.join(directory, "r-verify.json"))["calls"]], ["GET /api/workflow/script/5001"])
            self.assertEqual(main(["cls", "read-draft", "--id", "abc"], None, via)["code"], 2)
        finally:
            cls.close()

    def test_without_login(self) -> None:
        """Without a login every online command stops with the instruction to log in, and nothing is sent."""
        cls = FakeCls(CATALOG)
        try:
            logged_out = ScriptedOnline(session=Blocked("CLS 未登录或登录已过期：先运行 gof cls login --qr <png文件> 生成登录二维码，交给用户用飞书手机端扫码"))
            for args in (["cls", "session"], ["cls", "read-draft", "--id", "1"], ["cls", "catalog", "--output", os.path.join(temp_dir("cls-none-"), "c.json")]):
                answer = main(args, None, logged_out)
                self.assertEqual(answer["code"], 2)
                self.assertRegex(answer["result"]["reason"], "cls login")
            self.assertEqual(len(cls.requests), 0)
        finally:
            cls.close()

    def test_qr_while_unreachable(self) -> None:
        """cls login --qr while CLS is out of reach makes no code to scan: a scan could not log anybody in."""
        made = []

        def login_qr(file: str) -> dict:
            made.append(file)
            raise RuntimeError("no code is to be made")

        online = ScriptedOnline(capture={"state": "unreachable", "reason": "HTTP 502"}, login_qr=login_qr)
        png = os.path.join(temp_dir("cls-qr-"), "q.png")
        answer = main(["cls", "login", "--qr", png], None, online)
        self.assertEqual([answer["code"], answer["result"]["status"], len(made), os.path.exists(png)], [2, "blocked", 0, False])
        self.assertRegex(answer["result"]["reason"], "HTTP 502.*不说明登录失效")

    def test_qr_already_logged_in(self) -> None:
        """cls login --qr: a login that is already there is picked up by one hidden look, and no token is ever printed."""
        seen = []

        def capture(wait_ms: int) -> dict:
            seen.append(wait_ms)
            return {"state": "ok", "authorization": "x", "account": {"id": 7, "name": "张三"}}

        here = main(["cls", "login", "--qr", os.path.join(temp_dir("cls-qr-"), "q.png")], None, ScriptedOnline(capture=capture))
        self.assertEqual([here["code"], here["result"]["status"], here["result"]["account"], seen], [0, "logged_in", {"id": 7, "name": "张三"}, [12_000]])
        text = json.dumps(here)
        self.assertTrue("authorization" not in text and "Bearer" not in text and '"x"' not in text, "the token is never in what a command prints")


ALICE = {"id": 7, "name": "张三"}
BOB = {"id": 8, "name": "李四"}


def ok_capture(account: dict) -> dict:
    return {"state": "ok", "authorization": "secret-token-1", "account": account}


def scripted(*answers: dict) -> ScriptedOnline:
    """What a hidden browser would give back, one scripted answer per call."""
    queue = list(answers)

    def session() -> dict:
        a = queue.pop(0)
        if a["state"] != "ok":
            raise RuntimeError("no login")
        return {"authorization": a["authorization"], "account": a["account"], "origin": "https://cls.test"}

    return ScriptedOnline(session=session, capture=lambda wait: queue.pop(0) if queue else {"state": "login_required"})


def fake_feishu_info(directory: str) -> str:
    """A stand-in for lark-agent's feishu.py that answers `bridge info` with the user's browser profile."""
    return py_script(directory, "feishu.py", "import json, os, sys\n"
                     "sys.stdin.read()\n"
                     "user = os.environ.get('LARK_AGENT_USER_DIR', '')\n"
                     "print(json.dumps({'ok': True, 'data': {'bridge': 'feishu-sdk-bridge/1', 'browser_profile': os.path.join(user, 'feishu', 'browser-profile')}}))\n")


PNG = bytes.fromhex("89504e470d0a1a0a")


class GuardTests(unittest.TestCase):
    def test_profiles_per_user(self) -> None:
        """Each user has their own CLS profile and account file under their own directory; the browser is the Feishu module's."""
        fake = fake_feishu_info(temp_dir("ra-feishu-"))
        a, b = temp_dir("ra-cls-a-"), temp_dir("ra-cls-b-")
        env_a = {**os.environ, "LARK_AGENT_USER_DIR": a, "LARK_AGENT_FEISHU": fake}
        env_b = {**os.environ, "LARK_AGENT_USER_DIR": b, "LARK_AGENT_FEISHU": fake}
        self.assertEqual(cls_profile_dir(env_a), os.path.join(a, "feishu", "browser-profile"))
        self.assertEqual(cls_profile_dir(env_b), os.path.join(b, "feishu", "browser-profile"))
        self.assertEqual(cls_account_file(env_a), os.path.join(a, "requirement-auto", "cls", "account.json"))
        self.assertNotEqual(cls_profile_dir(env_a), cls_profile_dir(env_b))
        for file, directory in ((cls_profile_dir(env_a), a), (cls_account_file(env_a), a), (cls_profile_dir(env_b), b), (cls_account_file(env_b), b)):
            relative = os.path.relpath(file, directory)
            self.assertTrue(relative and not relative.startswith(".."), f"{file} stays inside {directory}")

    def test_host_without_directory(self) -> None:
        """A host that names the user but not the directory is refused instead of sharing one folder."""
        self.assertRaisesRegex(Blocked, "LARK_AGENT_USER_DIR", resolve_data_paths, {"LARK_AGENT_USER_OPEN_ID": "ou_alice"}, temp_dir("ra-home-"))
        self.assertRaisesRegex(Blocked, "LARK_AGENT_USER_DIR", cls_account_file, {"LARK_AGENT_USER_DIR": "", "LARK_AGENT_USER_OPEN_ID": "ou_alice"})
        # The single-machine modes are unchanged.
        self.assertEqual(resolve_data_paths({}, temp_dir("ra-home-")).mode, "standalone")
        self.assertEqual(resolve_data_paths({"LARK_AGENT_USER_OPEN_ID": "ou_alice", "LARK_AGENT_USER_DIR": temp_dir("ra-u-")}).mode, "host")

    def test_pin(self) -> None:
        """The first CLS account is pinned, the same one passes, another is refused without naming it."""
        file = os.path.join(temp_dir("ra-pin-"), "cls", "account.json")
        self.assertIsNone(read_pin(file))
        check_pin(file, ALICE, lambda: datetime(2026, 10, 19, 8, 0, 0, tzinfo=timezone.utc))
        self.assertEqual(read_pin(file), {"id": 7, "name": "张三", "pinned_at": "2026-10-19T08:00:00.000Z"})
        check_pin(file, {"id": 7, "name": "张三（改名）"})  # the same account under a new display name
        with self.assertRaises(Blocked) as caught:
            check_pin(file, BOB)
        self.assertTrue("张三" in caught.exception.message and "李四" not in caught.exception.message and "管理员" in caught.exception.message)
        self.assertEqual(read_pin(file)["id"], 7, "a refused login does not replace the pin")
        with open(file, encoding="utf-8") as handle:
            self.assertNotRegex(handle.read().lower(), "authorization|token|secret", "the pin holds id and name only")

    def test_pin_rename(self) -> None:
        """The same CLS account under a new name keeps its pin under the new name, which the consent page is then checked against."""
        file = os.path.join(temp_dir("ra-pin-rename-"), "cls", "account.json")
        check_pin(file, {"id": 1565, "name": "钟丰骏-实习生"}, lambda: datetime(2026, 10, 20, 8, 0, 0, tzinfo=timezone.utc))
        check_pin(file, {"id": 1565, "name": "钟丰骏"})
        self.assertEqual(read_pin(file), {"id": 1565, "name": "钟丰骏", "pinned_at": "2026-10-20T08:00:00.000Z"}, "renamed, pinned as before")
        check_pin(file, {"id": 1565, "name": ""})
        self.assertEqual(read_pin(file)["name"], "钟丰骏", "a login that gives no name changes nothing")
        self.assertRaisesRegex(Blocked, "已绑定为“钟丰骏”", check_pin, file, BOB)
        self.assertEqual(os.listdir(os.path.dirname(file)), ["account.json"], "nothing is left beside the pin")

    def test_pin_through_commands(self) -> None:
        """Through the commands: login pins, a different account is refused by login and by every later command."""
        directory = temp_dir("ra-pin-cmd-")
        pin_file = os.path.join(directory, "account.json")
        png = os.path.join(directory, "cls-login.png")
        first = main(["cls", "login", "--qr", png], None, guard_online(scripted(ok_capture(ALICE)), pin_file))
        self.assertEqual([first["code"], first["result"]["status"], read_pin(pin_file)["id"]], [0, "logged_in", 7])
        wrong = main(["cls", "login", "--qr", png], None, guard_online(scripted(ok_capture(BOB)), pin_file))
        self.assertEqual(wrong["code"], 2)
        self.assertRegex(wrong["result"]["reason"], "已绑定为“张三”")
        self.assertEqual(main(["cls", "session"], None, guard_online(scripted(ok_capture(BOB)), pin_file))["code"], 2)
        self.assertEqual(main(["cls", "session"], None, guard_online(scripted(ok_capture(ALICE)), pin_file))["code"], 0)
        text = json.dumps([first, wrong], ensure_ascii=False)
        self.assertTrue("secret-token-1" not in text and "李四" not in text, "neither the token nor the other account is printed")

    def test_no_window_login(self) -> None:
        """There is no login by window: cls login without --qr is refused and nothing is opened."""
        looked = []
        online = ScriptedOnline(capture=lambda wait: looked.append(wait) or {"state": "login_required"})
        result = main(["cls", "login"], None, online)
        self.assertEqual(result["code"], 2)
        self.assertRegex(result["result"]["reason"], "--qr")
        self.assertEqual(looked, [])
        self.assertEqual(main(["cls", "login", "--wait", "30"], None, online)["code"], 2)

    def qr_online(self, directory: str, *answers: dict):
        """An Online whose QR step writes through the same functions the real one uses; `capture` answers come from the script."""
        record_file = os.path.join(directory, "login-qr.json")
        outcome = {"next": "qr", "calls": []}
        queue = list(answers)

        def login_qr(file: str) -> dict:
            outcome["calls"].append(file)
            return {"state": "qr", "file": write_qr(record_file, file, PNG)} if outcome["next"] == "qr" else {"state": "not_login_page", "where": "somewhere.test/page"}

        inner = ScriptedOnline(capture=lambda wait: queue.pop(0) if queue else {"state": "login_required"}, login_qr=login_qr)

        def guarded(now=None):
            return guard_online(inner, os.path.join(directory, "account.json"), now=now, qr_file=record_file, profile_dir=os.path.join(directory, "profile"))

        return record_file, outcome, guarded

    def test_qr_written(self) -> None:
        """cls login --qr: a login that is still good needs no picture; otherwise the picture is written and described."""
        directory = temp_dir("ra-qr-")
        png = os.path.join(directory, "workspace", "cls-login.png")
        record_file, outcome, guarded = self.qr_online(directory, ok_capture(ALICE))
        here = main(["cls", "login", "--qr", png], None, guarded())
        self.assertEqual([here["code"], here["result"]["status"], len(outcome["calls"]), os.path.exists(png)], [0, "logged_in", 0, False])
        check_pin(os.path.join(directory, "account.json"), ALICE)
        made = main(["cls", "login", "--qr", png], None, guarded())
        self.assertEqual([made["code"], made["result"]["status"], made["result"]["qr_file"], made["result"]["expected_account"]], [0, "qr_ready", os.path.abspath(png), "张三"])
        self.assertRegex(made["result"]["message"], "扫码")
        self.assertTrue(os.path.exists(png) and os.path.exists(record_file))
        self.assertNotIn("secret-token", json.dumps(made), "no token anywhere in what is printed")

    def test_qr_replaced(self) -> None:
        """cls login --qr: asking again replaces the picture; another file or a non-picture name is refused."""
        directory = temp_dir("ra-qr-more-")
        png = os.path.join(directory, "a.png")
        _, outcome, guarded = self.qr_online(directory)
        self.assertEqual(main(["cls", "login", "--qr", png], None, guarded())["result"]["status"], "qr_ready")
        write_file(png, "older picture")
        self.assertEqual(main(["cls", "login", "--qr", png], None, guarded())["result"]["status"], "qr_ready")
        with open(png, "rb") as handle:
            self.assertEqual(handle.read(), PNG, "the same file is the new picture")
        # A second file moves the picture: the old one is removed, so only one code is left lying around.
        other = os.path.join(directory, "b.png")
        self.assertEqual(main(["cls", "login", "--qr", other], None, guarded())["result"]["status"], "qr_ready")
        self.assertEqual([os.path.exists(png), os.path.exists(other)], [False, True])
        # A file that is not the picture's own is never overwritten.
        mine = write_file(os.path.join(directory, "report.png"), "something else")
        self.assertEqual(main(["cls", "login", "--qr", mine], None, guarded())["code"], 2)
        with open(mine, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "something else")
        self.assertEqual(main(["cls", "login", "--qr", os.path.join(directory, "c.txt")], None, guarded())["code"], 2)
        self.assertEqual(main(["cls", "login", "--qr", os.path.join(directory, "cpng")], None, guarded())["code"], 2, "the dot of .png is a dot")
        outcome["next"] = "not_login_page"
        none = main(["cls", "login", "--qr", os.path.join(directory, "e.png")], None, guarded())
        self.assertEqual([none["code"], none["result"]["status"], none["result"]["where"], os.path.exists(os.path.join(directory, "e.png"))], [0, "no_qr", "somewhere.test/page", False])

    def test_qr_cleaned_up(self) -> None:
        """The picture goes away when the login is done, and a code nobody scanned is cleaned up after a while."""
        directory = temp_dir("ra-qr-clean-")
        png = os.path.join(directory, "cls-login.png")
        record_file, _, guarded = self.qr_online(directory)
        main(["cls", "login", "--qr", png], None, guarded())
        self.assertTrue(os.path.exists(png))
        # Looked at too soon: still there. Looked at after the code could no longer be good: removed.
        created = read_json(record_file)["created_at"]
        guarded(lambda: datetime.fromtimestamp((created + 5 * 60_000) / 1000, tz=timezone.utc)).capture(1)
        self.assertTrue(os.path.exists(png), "five minutes: the code may still be scanned")
        guarded(lambda: datetime.fromtimestamp((created + 16 * 60_000) / 1000, tz=timezone.utc)).capture(1)
        self.assertEqual([os.path.exists(png), os.path.exists(record_file)], [False, False])
        # A login that completes removes it at once.
        main(["cls", "login", "--qr", png], None, guarded())
        self.assertTrue(os.path.exists(png))
        done_record, _, done = self.qr_online(directory, ok_capture(ALICE))
        write_file(done_record, json.dumps({"path": png, "created_at": js.now_ms()}))
        self.assertEqual(done().capture(1)["state"], "ok")
        self.assertEqual([os.path.exists(png), os.path.exists(done_record)], [False, False])


GOOD_FEISHU = {"ok": True, "data": {"logged_in": True, "open_id": "ou_alice", "name": "李小明", "missing_scopes": [], "refresh_expires_at": 1}}


def fail(kind: str, msg: str = "消息") -> dict:
    return {"ok": False, "error": {"kind": kind, "message": msg}}


def bridge_saying(reply: dict) -> Bridge:
    return Bridge(lambda name, payload: reply)


class ClsSaying(ScriptedOnline):
    def __init__(self, state: str, **extra: object) -> None:
        super().__init__()
        self.looked = 0
        self._state = state
        self._extra = extra

        def login_qr(file: str) -> dict:
            raise AssertionError("a check must never start a login")

        self.login_qr = login_qr

    def capture(self, wait_ms: int) -> dict:
        self.looked += 1
        if self._state == "pin":
            raise Blocked("这个用户的 CLS 账号已绑定为“张三”，这次登录的是另一个账号，已拒绝。")
        return {"state": self._state, "authorization": "secret-token-1", "account": {"id": 7, "name": "张三"}, **self._extra}


class LoginCheckTests(unittest.TestCase):
    def test_missing_logins(self) -> None:
        """Both good is ok; each missing login is named with what to do about it; nothing is started."""
        both = main(["login", "check"], bridge_saying(GOOD_FEISHU), ClsSaying("ok"))
        self.assertEqual([both["code"], both["result"]["status"], both["result"]["feishu"]["status"], both["result"]["cls"]["status"], both["result"].get("todo")],
                         [0, "ok", "ok", "ok", None])
        self.assertEqual(both["result"]["cls"]["account"], {"id": 7, "name": "张三"})
        not_looked = ClsSaying("ok")
        feishu_gone = main(["login", "check"], bridge_saying(fail("auth_required", "飞书登录已失效")), not_looked)
        self.assertEqual([feishu_gone["code"], feishu_gone["result"]["status"], feishu_gone["result"]["feishu"]["status"]], [2, "login_required", "login_required"])
        self.assertEqual(len(feishu_gone["result"]["todo"]), 1)
        self.assertRegex(feishu_gone["result"]["todo"][0], "feishu_login")
        self.assertEqual(feishu_gone["result"]["cls"]["status"], "not_checked", "CLS waits for Feishu")
        self.assertEqual(not_looked.looked, 0, "no browser is started while Feishu is not logged in")
        cls_gone = main(["login", "check"], bridge_saying(GOOD_FEISHU), ClsSaying("login_required"))
        self.assertEqual([cls_gone["code"], cls_gone["result"]["status"], cls_gone["result"]["cls"]["status"]], [2, "login_required", "login_required"])
        self.assertRegex(cls_gone["result"]["todo"][0], "cls login --qr")
        neither_cls = ClsSaying("login_required")
        neither = main(["login", "check"], bridge_saying(fail("auth_required")), neither_cls)
        self.assertEqual(len(neither["result"]["todo"]), 1, "one login at a time: Feishu first")
        self.assertRegex(neither["result"]["todo"][0], "feishu_login")
        self.assertEqual(neither_cls.looked, 0)
        missing = main(["login", "check"], bridge_saying({"ok": True, "data": {"logged_in": True, "name": "李小明", "missing_scopes": ["wiki:wiki:readonly"]}}), ClsSaying("ok"))
        self.assertEqual([missing["result"]["status"], missing["result"]["feishu"]["status"], missing["result"]["feishu"]["missing_scopes"]],
                         ["login_required", "scopes_missing", ["wiki:wiki:readonly"]])
        self.assertEqual(len(missing["result"]["todo"]), 1)

    def test_blocked_and_unreachable(self) -> None:
        """A problem that logging in again cannot fix is "blocked", and not being able to reach Feishu is not a lapsed login."""
        wrong = main(["login", "check"], bridge_saying(GOOD_FEISHU), ClsSaying("pin"))
        self.assertEqual([wrong["code"], wrong["result"]["status"], wrong["result"]["cls"]["status"]], [2, "blocked", "blocked"])
        self.assertRegex(wrong["result"]["cls"]["reason"], "已绑定")
        self.assertNotIn("todo", wrong["result"], "a wrong account is not fixed by asking for another scan")
        refused = main(["login", "check"], bridge_saying(GOOD_FEISHU), ClsSaying("consent_refused", reason="飞书授权页请求的不是 dsc_admin，没有点击"))
        self.assertEqual([refused["result"]["status"], refused["result"]["cls"]["status"]], ["blocked", "blocked"])
        other = main(["login", "check"], bridge_saying(fail("identity_mismatch", "不是本人")), ClsSaying("ok"))
        self.assertEqual([other["result"]["status"], other["result"]["feishu"]["status"]], ["blocked", "identity_mismatch"])
        # A block on one side outranks a login wanted on the other: nobody is asked to scan before the block is reported.
        both = main(["login", "check"], bridge_saying(fail("config", "未配置飞书应用")), ClsSaying("login_required"))
        self.assertEqual([both["code"], both["result"]["status"], both["result"]["feishu"]["status"], both["result"]["cls"]["status"]], [2, "blocked", "blocked", "login_required"])
        unreachable = main(["login", "check"], bridge_saying(fail("network")), ClsSaying("ok"))
        self.assertEqual([unreachable["code"], unreachable["result"]["status"], unreachable["result"]["feishu"]["status"]], [2, "unknown", "unreachable"])
        self.assertNotIn("todo", unreachable["result"], "no new login is asked for when Feishu merely did not answer")

    def test_cls_unreachable(self) -> None:
        """CLS out of reach is "unknown" and asks nobody to scan, as Feishu out of reach is."""
        cls = main(["login", "check"], bridge_saying(GOOD_FEISHU), ClsSaying("unreachable", reason="连接失败"))
        self.assertEqual([cls["code"], cls["result"]["status"], cls["result"]["cls"]["status"], cls["result"].get("todo")], [2, "unknown", "unknown", None])
        self.assertRegex(cls["result"]["cls"]["reason"], "连接失败.*不说明登录失效")

    def test_no_token(self) -> None:
        """login check never prints a token."""
        text = json.dumps([main(["login", "check"], bridge_saying(GOOD_FEISHU), ClsSaying("ok")),
                           main(["login", "check"], bridge_saying(fail("auth_required")), ClsSaying("login_required"))], ensure_ascii=False)
        self.assertTrue("secret-token-1" not in text and "Bearer" not in text and "authorization" not in text.lower())


def lock_of(profile_dir: str) -> str:
    return f"{os.path.abspath(profile_dir)}.lock"


class _VersionOnly:
    """Answers /json/version like a browser whose DevTools socket cannot be joined."""

    def __init__(self, endpoint: str) -> None:
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args) -> None:
                pass

            def do_GET(self) -> None:
                if self.path == "/json/version":
                    body = json.dumps({"Browser": "Chrome", "webSocketDebuggerUrl": f"ws://127.0.0.1:{outer.port}{endpoint}"}).encode()
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


class LockTests(unittest.TestCase):
    def test_waited_for(self) -> None:
        """A profile is used by one command at a time: another process holding it is waited for, and given up on with a clear reason."""
        profile_dir = os.path.join(temp_dir("ra-lock-"), "browser-profile")
        write_file(lock_of(profile_dir), json.dumps({"pid": os.getpid(), "at": js.now_ms()}))
        self.assertRaisesRegex(Blocked, "另一个命令使用", lock_profile, profile_dir, 300)
        threading.Timer(0.3, lambda: os.remove(lock_of(profile_dir))).start()
        release = lock_profile(profile_dir, 5_000)
        self.assertTrue(os.path.exists(lock_of(profile_dir)), "held now by this process")
        release()
        self.assertFalse(os.path.exists(lock_of(profile_dir)), "and let go of")

    def test_ended_holder(self) -> None:
        """A lock left by a command that has ended, or never finished being written long ago, is taken over."""
        profile_dir = os.path.join(temp_dir("ra-lock-"), "browser-profile")
        ended = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"], capture_output=True, text=True)
        os.makedirs(os.path.dirname(profile_dir), exist_ok=True)
        write_file(lock_of(profile_dir), json.dumps({"pid": int(ended.stdout), "at": js.now_ms()}))
        lock_profile(profile_dir, 1_000)()
        write_file(lock_of(profile_dir), "")
        old = time.time() - 60
        os.utime(lock_of(profile_dir), (old, old))
        lock_profile(profile_dir, 1_000)()

    def test_shared_in_process(self) -> None:
        """Within one process the lock is shared, and let go of only when every holder has."""
        profile_dir = os.path.join(temp_dir("ra-lock-"), "browser-profile")
        first = lock_profile(profile_dir, 1_000)
        second = lock_profile(profile_dir, 1_000)
        first()
        first()
        self.assertTrue(os.path.exists(lock_of(profile_dir)), "still held by the second")
        second()
        self.assertFalse(os.path.exists(lock_of(profile_dir)))

    def test_slow_browser(self) -> None:
        """A browser that is running but slow to answer is not taken for gone: no second one is started and its port file stays."""
        server = _VersionOnly("/devtools/browser/abc")
        try:
            profile_dir = os.path.join(temp_dir("ra-lock-"), "browser-profile")
            port_file = write_file(os.path.join(profile_dir, "DevToolsActivePort"), f"{server.port}\n/devtools/browser/abc")
            self.assertRaisesRegex(Blocked, "调试接口没有响应", open_browser, os.path.join(profile_dir, "no-such-browser.exe"), profile_dir)
            self.assertTrue(os.path.exists(port_file), "the running browser can still be found")
            self.assertFalse(os.path.exists(lock_of(profile_dir)), "and the refusal let go of the lock")
        finally:
            server.close()

    def test_gone_browser(self) -> None:
        """The port file of a browser that is gone is no obstacle: the start is tried."""
        import socket

        profile_dir = os.path.join(temp_dir("ra-lock-"), "browser-profile")
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        write_file(os.path.join(profile_dir, "DevToolsActivePort"), f"{port}\n/devtools/browser/abc")
        self.assertRaisesRegex(Blocked, "浏览器没有启动成功", open_browser, os.path.join(profile_dir, "no-such-browser.exe"), profile_dir)
        self.assertFalse(os.path.exists(lock_of(profile_dir)))

    def test_stale_renewal(self) -> None:
        """A lock its holder stopped renewing is taken over, though its process id is alive (Windows gives a dead one's id to others)."""
        profile_dir = os.path.join(temp_dir("ra-lock-"), "browser-profile")
        write_file(lock_of(profile_dir), json.dumps({"pid": os.getpid(), "at": js.now_ms() - 3 * 60_000}))
        lock_profile(profile_dir, 1_000)()
        self.assertFalse(os.path.exists(lock_of(profile_dir)))

    def test_own_lock_only(self) -> None:
        """A holder lets go of its own lock only, and a lock path that cannot be read is waited on in time, not spun on."""
        profile_dir = os.path.join(temp_dir("ra-lock-"), "browser-profile")
        release = lock_profile(profile_dir, 1_000)
        # Taken over meanwhile as abandoned: the old holder must not remove the new holder's lock.
        write_file(lock_of(profile_dir), json.dumps({"pid": os.getpid(), "at": js.now_ms(), "token": "theirs"}))
        release()
        self.assertEqual(read_json(lock_of(profile_dir))["token"], "theirs")
        os.remove(lock_of(profile_dir))
        os.mkdir(lock_of(profile_dir))
        started = time.monotonic()
        self.assertRaises(Blocked, lock_profile, profile_dir, 300)
        self.assertGreaterEqual(time.monotonic() - started, 0.3)

    def test_other_browser_on_port(self) -> None:
        """A port file whose port another browser answers on by now is stale: the start is tried."""
        server = _VersionOnly("/devtools/browser/someone-else")
        try:
            profile_dir = os.path.join(temp_dir("ra-lock-"), "browser-profile")
            write_file(os.path.join(profile_dir, "DevToolsActivePort"), f"{server.port}\n/devtools/browser/abc")
            self.assertRaisesRegex(Blocked, "浏览器没有启动成功", open_browser, os.path.join(profile_dir, "no-such-browser.exe"), profile_dir)
            self.assertFalse(os.path.exists(lock_of(profile_dir)))
        finally:
            server.close()


if __name__ == "__main__":
    unittest.main()
