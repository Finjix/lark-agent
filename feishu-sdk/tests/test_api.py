from __future__ import annotations

import json
import os
import unittest
from concurrent.futures import ThreadPoolExecutor

from tests.helpers import Clock, temp_dir
from tests.helpers.mock_feishu import MockFeishu, Request

from feishu_sdk.api import FeishuApi
from feishu_sdk.auth import FeishuAuth
from feishu_sdk.config import AppCredentials
from feishu_sdk.errors import BridgeError
from feishu_sdk.paths import resolve_data_paths

TOKEN = "shtABCDEFGH"


def write(**overrides):
    return {"sheet_id": "s1", "range": "A1:A1", "cells": [[{"value": "x"}]], "allow_overwrite": False, **overrides}


class ApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.mock = MockFeishu().start()
        self.addCleanup(self.mock.stop)
        self.dir = temp_dir("fs-api-")
        clock = Clock()
        self.auth = FeishuAuth(paths=resolve_data_paths({"AGENT_USER_DIR": self.dir}), app=AppCredentials(self.mock.app_id, self.mock.app_secret),
                               endpoints=self.mock.endpoints, now=clock.now, sleep=clock.sleep)
        self.auth.start_login()
        self.mock.device_script = ["issue"]
        self.auth.poll_login(10_000)
        self.api = FeishuApi(self.auth, lambda ms: None)

    def refused(self, kind: str, call) -> BridgeError:
        with self.assertRaises(BridgeError) as caught:
            call()
        self.assertEqual(caught.exception.kind, kind, caught.exception.message)
        return caught.exception

    def test_reads_go_only_to_the_allowlisted_endpoints(self) -> None:
        node = self.api.get("/open-apis/wiki/v2/spaces/get_node", {"token": "wiki1", "obj_type": "wiki"})
        self.assertEqual(node["node"]["obj_token"], "shtABCDEFGH")
        self.assertRegex(self.mock.requests("/open-apis/wiki/v2/spaces/get_node")[0].url, r"token=wiki1&obj_type=wiki|obj_type=wiki&token=wiki1")
        for bad in ["/open-apis/drive/v1/files/abc", "/open-apis/wiki/v2/spaces/get_node?x=1", "/open-apis/wiki/../authen/v1/user_info",
                    "https://evil.example/open-apis/wiki/v2/spaces/get_node", "/open-apis/sheets/v2/spreadsheets/abc/values", "/open-apis/im/v1/messages",
                    "/open-apis/wiki/v2/spaces/get_node\n"]:
            self.refused("invalid_request", lambda: self.api.get(bad))
        self.refused("invalid_request", lambda: self.api.get("/open-apis/wiki/v2/spaces/get_node", {"token": {"nested": 1}}))
        self.assertEqual(len(self.mock.requests("/open-apis/drive")), 0, "refused calls are never sent")

    def test_a_token_the_server_rejects_is_renewed_once_and_the_read_retried(self) -> None:
        self.mock.reject_token_once = True
        before = self.mock.current_access
        node = self.api.get("/open-apis/wiki/v2/spaces/get_node", {"token": "wiki1"})
        self.assertEqual(node["node"]["node_token"], "wiki1")
        self.assertNotEqual(self.mock.current_access, before)
        self.assertEqual(self.mock.refresh_count, 1)

    def test_the_revision_comes_from_the_metainfo_endpoint(self) -> None:
        self.assertEqual(self.api.revision(TOKEN), 7)
        self.mock.revision = 8
        self.assertEqual(self.api.revision(TOKEN), 8)
        self.refused("invalid_request", lambda: self.api.revision("../x"))

    def test_a_write_goes_out_exactly_as_lark_cli_sends_it_with_overwriting_switched_off(self) -> None:
        self.api.cells_set("shtTEST123", [write()])
        sent = self.mock.requests("/open-apis/sheet_ai/v2/spreadsheets/shtTEST123/tools/invoke_write")
        self.assertEqual(len(sent), 1)
        # Captured from `lark-cli sheets +cells-set --writes - --allow-overwrite=false --dry-run` (1.0.98).
        captured = ('{"input":"{\\"excel_id\\":\\"shtTEST123\\",\\"operations\\":[{\\"input\\":{\\"allow_overwrite\\":false,\\"cells\\":[[{\\"value\\":\\"x\\"}]],'
                    '\\"excel_id\\":\\"shtTEST123\\",\\"range\\":\\"A1:A1\\",\\"sheet_id\\":\\"s1\\"},\\"tool_name\\":\\"set_cell_range\\"}]}","tool_name":"batch_update"}')
        self.assertEqual(sent[0].body, captured)
        self.assertEqual(sent[0].headers["authorization"], f"Bearer {self.mock.current_access}")

    def test_a_write_that_does_not_forbid_overwriting_or_is_malformed_is_never_sent(self) -> None:
        for writes in ([write(allow_overwrite=True)], [write(allow_overwrite=None)], [write(allow_overwrite="false")], [write(range="A1:A1; DROP")],
                       [write(sheet_id="")], [write(cells=[])], [write(cells=[[]])], [], [write() for _ in range(101)],
                       [write(), write(allow_overwrite=True)], [write(range="A1\n")]):
            self.refused("invalid_request", lambda: self.api.cells_set(TOKEN, writes))
        self.refused("invalid_request", lambda: self.api.cells_set("../x", [write()]))
        self.assertEqual(len(self.mock.requests("/open-apis/sheet_ai")), 0)

    def test_the_server_refusing_a_non_empty_target_is_told_apart_from_other_failures(self) -> None:
        self.mock.write_reply = (200, {"code": 1310999, "msg": "target range is non-empty, cannot overwrite"})
        self.refused("occupied", lambda: self.api.cells_set(TOKEN, [write()]))
        self.mock.write_reply = (400, {"code": 1310999, "msg": "范围内已有内容，禁止覆盖"})
        self.refused("occupied", lambda: self.api.cells_set(TOKEN, [write()]))
        self.mock.write_reply = (200, {"code": 1310001, "msg": "invalid range"})
        self.refused("api", lambda: self.api.cells_set(TOKEN, [write()]))
        self.mock.write_reply = (200, {"code": 99991679, "msg": "no permission"})
        self.refused("scope_missing", lambda: self.api.cells_set(TOKEN, [write()]))
        self.mock.write_reply = (502, {"msg": "bad gateway"})
        self.refused("unknown_result", lambda: self.api.cells_set(TOKEN, [write()]))

    def test_a_write_that_gets_no_answer_is_an_unknown_result_and_it_is_not_resent(self) -> None:
        attempts = []

        def cut(req: Request) -> bool:
            if "invoke_write" in req.url:
                attempts.append(1)
                req.drop()
                return True
            return False

        self.mock.override = cut
        self.refused("unknown_result", lambda: self.api.cells_set(TOKEN, [write()]))
        self.assertEqual(len(attempts), 1, "sent once, never retried")

    def test_a_token_turned_down_during_a_write_is_renewed_and_the_write_sent_once_more(self) -> None:
        # Feishu turns a token down before it looks at the request.
        self.mock.reject_token_once = True
        self.assertEqual(self.api.cells_set(TOKEN, [write()]), {"ok": True})
        self.assertEqual(len(self.mock.requests("/open-apis/sheet_ai")), 2)
        self.assertEqual(self.mock.refresh_count, 1)

    def test_reads_turned_down_together_cause_one_renewal_and_each_of_them_goes_through(self) -> None:
        self.mock.revoke_access()
        with ThreadPoolExecutor(4) as pool:
            results = list(pool.map(lambda _: self.api.revision(TOKEN), range(4)))
        self.assertEqual(results, [7, 7, 7, 7])
        self.assertEqual(self.mock.refresh_count, 1, "the others found the token renewed already")

    def test_a_read_turned_down_for_too_many_requests_is_asked_again_a_little_later_a_write_is_not(self) -> None:
        self.mock.rate_limited = 2
        self.assertEqual(self.api.revision(TOKEN), 7)
        self.assertEqual(len(self.mock.requests("/open-apis/sheets/v2")), 3)
        self.mock.rate_limited = 4
        self.assertEqual(self.api.revision(TOKEN), 7)
        self.mock.rate_limited = 5
        self.refused("api", lambda: self.api.revision(TOKEN))
        self.mock.rate_limited = 1
        self.refused("api", lambda: self.api.cells_set(TOKEN, [write()]))
        self.assertEqual(len(self.mock.requests("/open-apis/sheet_ai")), 1)

    def test_the_sheets_services_own_too_many_requests_is_asked_again_too(self) -> None:
        self.mock.rate_limited_as_sheets = True
        self.mock.rate_limited = 3
        self.assertEqual(self.api.revision(TOKEN), 7)
        self.mock.rate_limited = 1
        self.refused("api", lambda: self.api.cells_set(TOKEN, [write()]))

    def test_the_workbook_is_exported_to_a_new_xlsx_and_an_existing_file_is_never_replaced(self) -> None:
        out = os.path.join(self.dir, "snap", "one.xlsx")
        result = self.api.export_xlsx(TOKEN, out)
        self.assertEqual(result["path"], out)
        self.assertGreaterEqual(result["bytes"], 22)
        with open(out, "rb") as handle:
            self.assertEqual(handle.read(2), b"PK")
        create = next(item for item in self.mock.requests("/open-apis/drive/v1/export_tasks") if item.method == "POST")
        self.assertEqual(json.loads(create.body), {"file_extension": "xlsx", "token": TOKEN, "type": "sheet"})
        self.refused("invalid_request", lambda: self.api.export_xlsx(TOKEN, out))
        self.refused("invalid_request", lambda: self.api.export_xlsx(TOKEN, os.path.join(self.dir, "x.csv")))
        self.refused("invalid_request", lambda: self.api.export_xlsx(TOKEN, "relative.xlsx"))

        self.mock.export_job = [{"job_status": 3}]
        failed = os.path.join(self.dir, "snap", "two.xlsx")
        self.refused("api", lambda: self.api.export_xlsx(TOKEN, failed))
        self.assertFalse(os.path.exists(failed))

    def test_a_new_dropdown_option_keeps_every_rule_its_mode_and_colors(self) -> None:
        # A multiple-choice column stays multiple choice.
        posted = []
        rules = [
            {"dataValidationType": "list", "conditionValues": ["菲比", "丁杰"], "ranges": ["s1!M2:M5000"],
             "options": {"multipleValues": True, "highlightValidData": True, "colorValueMap": {"菲比": "#bacefd", "丁杰": "#A9EFE6"}}},
            {"dataValidationType": "list", "conditionValues": ["甲"], "ranges": ["s1!M5001:M5002", "s1!M5005"], "options": {"multipleValues": False, "highlightValidData": False}},
            {"dataValidationType": "list", "conditionValues": ["杨燃", "乙"], "ranges": ["s1!M5003"], "options": {"highlightValidData": False}},
        ]

        def validations(req: Request) -> bool:
            if req.url.startswith("/open-apis/sheets/v2/spreadsheets/shtTEST123/dataValidation"):
                if req.method == "POST":
                    posted.append(json.loads(req.body))
                req.json(200, {"code": 0, "data": {"sheetId": "s1", "dataValidations": rules} if req.method == "GET" else {}})
                return True
            return False

        self.mock.override = validations
        result = self.api.add_dropdown_option("shtTEST123", "s1", "M", 2, 5001, "杨燃", "#fed4a4")
        self.assertEqual(result["ranges"], ["s1!M2:M5000", "s1!M5001:M5002", "s1!M5005"])
        self.assertEqual(posted[0], {"range": "s1!M2:M5000", "dataValidationType": "list", "dataValidation": {
            "conditionValues": ["菲比", "丁杰", "杨燃"], "options": {"multipleValues": True, "highlightValidData": True, "colors": ["#bacefd", "#A9EFE6", "#fed4a4"]}}})
        self.assertEqual(posted[1], {"range": "s1!M5001:M5002", "dataValidationType": "list", "dataValidation": {
            "conditionValues": ["甲", "杨燃"], "options": {"multipleValues": False, "highlightValidData": False}}})
        self.assertEqual(len(posted), 3, "a rule that already has the option is left alone")
        rules[0]["options"] = {"multipleValues": True, "highlightValidData": True, "colorValueMap": {"菲比": "#bacefd"}}
        error = self.refused("api", lambda: self.api.add_dropdown_option("shtTEST123", "s1", "M", 2, 5001, "新", "#fed4a4"))
        self.assertRegex(error.message, "colours cannot be kept reliably")
        self.assertEqual(len(posted), 3, "nothing is sent when a rule cannot be kept as it is")

    def test_a_new_workbook_is_created_only_through_the_dedicated_call(self) -> None:
        def create(req: Request) -> bool:
            if req.url == "/open-apis/sheets/v3/spreadsheets" and req.method == "POST":
                req.json(200, {"code": 0, "data": {"spreadsheet": {"spreadsheet_token": "shtNEW12345", "url": "https://x/sheets/shtNEW12345"}}})
                return True
            return False

        self.mock.override = create
        self.assertEqual(self.api.create_workbook("GOFCN_API_SAFETY_x")["spreadsheet_token"], "shtNEW12345")
        self.refused("invalid_request", lambda: self.api.create_workbook(""))

    def test_document_blocks_are_read_page_by_page_at_one_revision(self) -> None:
        pages = {"": {"items": [{"block_id": "a"}], "has_more": True, "page_token": "p2"}, "p2": {"items": [{"block_id": "b"}], "has_more": False}}
        heads = iter([1, 2, 2, 2])

        def more(pathname: str, query: dict[str, str]):
            if pathname == "/open-apis/docx/v1/documents/doxABCDEFGH":
                return {"document": {"revision_id": next(heads), "title": "T"}}
            if pathname == "/open-apis/docx/v1/documents/doxABCDEFGH/blocks":
                self.assertEqual(query.get("document_revision_id"), "-1")
                return pages[query.get("page_token", "")]
            return None

        self.mock.more = more
        read = self.api.docx_blocks("doxABCDEFGH")
        self.assertEqual((read["revision"], read["title"], [b["block_id"] for b in read["blocks"]]), (2, "T", ["a", "b"]))
        self.refused("invalid_request", lambda: self.api.docx_insert("doxABCDEFGH", "blkABCDEFGH", 0, ["x"], [], "not-a-uuid"))
        self.refused("invalid_request", lambda: self.api.docx_set_texts("doxABCDEFGH", [{"block_id": "../x", "elements": [{}]}], "1" * 8 + "-1111-1111-1111-" + "1" * 12))


if __name__ == "__main__":
    unittest.main()
