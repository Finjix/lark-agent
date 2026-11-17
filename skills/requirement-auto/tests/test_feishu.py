"""The Feishu side: bridge failures, the probe, a whole append against a fake Feishu, reading documents, context, dropdowns (gof-feishu.test.ts)."""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import threading
import time
import unittest

from tests.helpers import fixture, make_zip, py_script, temp_dir, write_file

from gof import js
from gof.batch import prepare, record, report, status
from gof.brief import read_brief
from gof.cli import main
from gof.context import context
from gof.feishu import (Bridge, OccupiedRefusal, SCOPES, add_option, execute, plan_with_api, probe, read_rules, reconcile,
                        recompute_api_plan, recompute_plan)
from gof.agent_feishu import BRIDGE_VERSION, MISSING, FeishuServe, locate_feishu
from gof.rules import acceptance, dropdowns
from gof.seal import seal
from gof.snapshot import Snapshot
from gof.util import Blocked, NODE, WriteSent, read_json
from gof.zipread import ZipReader


def tmp() -> str:
    return temp_dir("gof-feishu-")


def reply(data: object) -> dict:
    return {"ok": True, "data": data}


def failure(kind: str, message: str = "m") -> dict:
    return {"ok": False, "error": {"kind": kind, "message": message}}


def bridge_with(handler) -> tuple[Bridge, list[dict]]:
    calls: list[dict] = []

    def run(name: str, payload: object) -> dict:
        calls.append({"name": name, "payload": payload})
        return handler(name, payload)

    return Bridge(run), calls


class BridgeTests(unittest.TestCase):
    def fails(self, fn, pattern: str, kind: type = Blocked) -> Exception:
        with self.assertRaises(kind) as caught:
            fn()
        self.assertRegex(str(caught.exception), pattern)
        return caught.exception

    def test_failures(self) -> None:
        """Bridge failures map to the refusals the workflow expects, and keep the reason the bridge gave."""
        cases = [
            (failure("occupied"), "非空", OccupiedRefusal),
            (failure("unknown_result"), "结果未知", Blocked),
            (failure("auth_required", "飞书登录已过期，请重新授权"), "auth_required.*重新授权", Blocked),
            (failure("scope_missing", "缺少权限：x"), "缺少权限", Blocked),
            (failure("api", "导出任务仍未完成（ticket=T123）；稍后只读查询该任务"), r"飞书接口调用失败（api）：导出任务仍未完成（ticket=T123）", Blocked),
        ]
        for result, pattern, kind in cases:
            bridge, _ = bridge_with(lambda n, p, r=result: r)
            self.fails(lambda: bridge.call("api-get", {"path": "/open-apis/x"}), pattern, kind)
        bridge, _ = bridge_with(lambda n, p: {"ok": False, "error": {"kind": "api", "code": 1310213, "message": "飞书接口返回错误（HTTP 403）：forbidden"}})
        self.fails(lambda: bridge.call("api-get", {}), r"错误码 1310213）：飞书接口返回错误（HTTP 403）：forbidden$")

    def test_no_answer(self) -> None:
        """A write that got no answer is an unknown result, a read that got none is a plain failure."""
        bridge, _ = bridge_with(lambda n, p: failure("network", "无法连接飞书"))
        self.fails(lambda: bridge.call("sheets-cells-set", {}, write=True), "结果未知")
        self.fails(lambda: bridge.call("api-get", {}), "飞书接口调用失败")

    def test_partial(self) -> None:
        """A reply that reports partial success or truncation is refused."""
        for data in ({"errors": [{"code": 1}]}, {"status": "partial_success"}, {"nested": {"has_more": True}}, {"ok": False}, {"failed_count": 2}):
            bridge, _ = bridge_with(lambda n, p, d=data: reply(d))
            self.fails(lambda: bridge.call("api-get", {}), "接口")
        bridge, _ = bridge_with(lambda n, p: reply({"items": [{"ok": True, "has_more": False, "errors": []}]}))
        self.assertEqual(bridge.call("api-get", {}), {"items": [{"ok": True, "has_more": False, "errors": []}]})

    def test_allowlist(self) -> None:
        """Reads stay under /open-apis/ without query strings, and a write without the no-overwrite flag never leaves."""
        bridge, calls = bridge_with(lambda n, p: reply({}))
        for bad in ("/other", "/open-apis/a/../b", "/open-apis/a?x=1", "https://x/open-apis/a"):
            self.fails(lambda: bridge.api_read(bad), "非法 API 路径")
        for writes in ([], [{"sheet_id": "s", "range": "A1:A1", "cells": [[{}]]}], [{"allow_overwrite": True}], "x"):
            self.fails(lambda: bridge.cells_set("shtABCDEFGH", writes), "不可覆盖")
        self.fails(lambda: bridge.cells_set("../x", [{"allow_overwrite": False}]), "非法表格 token")
        self.assertEqual(len(calls), 0)
        bridge.cells_set("shtABCDEFGH", [{"sheet_id": "s", "range": "A1:A1", "cells": [[{"value": 1}]], "allow_overwrite": False}])
        self.assertEqual(calls[0]["name"], "sheets-cells-set")
        self.assertIs(calls[0]["payload"]["writes"][0]["allow_overwrite"], False)

    def test_doctor(self) -> None:
        """Doctor needs the right bridge, a configured app, a login and every scope."""
        def handler(overrides: dict):
            replies = {"info": reply({"bridge": BRIDGE_VERSION, "app_configured": True, "node": "v24"}),
                       "auth-status": reply({"logged_in": True, "name": "李小明", "open_id": "ou_1", "storage": "host"}),
                       "auth-check-scopes": reply({"complete": True, "granted": SCOPES, "missing": []}), **overrides}
            return lambda name, payload: replies[name]

        bridge, calls = bridge_with(handler({}))
        result = bridge.doctor()
        self.assertEqual([result["user_name"], result["open_id"], result["identity"]], ["李小明", "ou_1", "user"])
        self.assertEqual([c["name"] for c in calls], ["info", "auth-status", "auth-check-scopes"])
        self.assertEqual(calls[2]["payload"]["required"], SCOPES)
        self.assertNotIn("token", js.canonical(result).lower())
        for overrides, pattern in (({"info": reply({"bridge": "other/9", "app_configured": True})}, "版本"),
                                   ({"info": reply({"bridge": BRIDGE_VERSION, "app_configured": False})}, "未配置飞书应用"),
                                   ({"auth-status": reply({"logged_in": False})}, "尚未授权"),
                                   ({"auth-check-scopes": reply({"complete": False, "granted": [], "missing": ["x"]})}, "缺少本流程必要权限")):
            self.fails(bridge_with(handler(overrides))[0].doctor, pattern)

    def test_serve_protocol(self) -> None:
        """The Feishu module's `serve` line protocol: requests by id, replies by id, this user's environment."""
        fake = py_script(tmp(), "feishu.py", "import json, os, sys\n"
                         "assert sys.argv[1:] == ['serve']\n"
                         "for line in sys.stdin:\n"
                         "    request = json.loads(line)\n"
                         "    name = request['command']\n"
                         "    if name == 'info':\n"
                         "        data = {'bridge': 'feishu-sdk-bridge/1', 'app_configured': True, 'storage': 'host', 'browser_profile': os.path.join(os.environ['LARK_AGENT_USER_DIR'], 'feishu', 'browser-profile')}\n"
                         "        answer = {'ok': True, 'data': data}\n"
                         "    elif name == 'auth-status':\n"
                         "        answer = {'ok': True, 'data': {'logged_in': False}}\n"
                         "    else:\n"
                         "        answer = {'ok': False, 'error': {'kind': 'invalid_request', 'message': '路径不在白名单内'}}\n"
                         "    print(json.dumps({'id': request['id'], 'reply': answer}), flush=True)\n")
        user = tmp()
        serve = FeishuServe({**os.environ, "LARK_AGENT_USER_DIR": user, "LARK_AGENT_FEISHU": fake})
        try:
            bridge = Bridge(serve.call)
            info = bridge.call("info")
            self.assertEqual([info["bridge"], info["storage"], info["browser_profile"]], [BRIDGE_VERSION, "host", os.path.join(user, "feishu", "browser-profile")])
            self.assertIs(bridge.call("auth-status")["logged_in"], False)
            self.fails(bridge.doctor, "尚未授权")
            self.fails(lambda: bridge.call("api-get", {"path": "/open-apis/im/v1/messages"}), "invalid_request")
        finally:
            serve.close()

    @unittest.skipUnless(os.environ.get("GOF_TEST_REAL_FEISHU") == "1", "set GOF_TEST_REAL_FEISHU=1 to run against lark-agent's own feishu-sdk/feishu.py")
    def test_real_agent_feishu(self) -> None:
        """lark-agent's Feishu module (the real one, in this project) answers for this user and enforces its own allowlist."""
        user = tmp()
        real = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))), "feishu-sdk", "feishu.py")
        env = {**os.environ, "LARK_AGENT_FEISHU": real}
        serve = FeishuServe({**env, "LARK_AGENT_USER_DIR": user, "FEISHU_APP_ID": "cli_x", "FEISHU_APP_SECRET": "secret-000000"})
        try:
            bridge = Bridge(serve.call)
            info = bridge.call("info")
            self.assertEqual([info["bridge"], info["storage"], info["browser_profile"]], [BRIDGE_VERSION, "host", os.path.join(user, "feishu", "browser-profile")])
            self.assertIs(bridge.call("auth-status")["logged_in"], False)
            self.fails(bridge.doctor, "尚未授权")
            self.fails(lambda: bridge.call("api-get", {"path": "/open-apis/im/v1/messages"}), "invalid_request")
        finally:
            serve.close()

    def test_process_gone(self) -> None:
        """A bridge process that ends before it reads a request fails the call: the command does not crash on the broken pipe."""
        # A feishu.py that does not serve (an old or broken copy): it ends at once and never reads its input.
        fake = py_script(tmp(), "feishu.py", "import sys\nsys.exit(2)\n")
        serve = FeishuServe({**os.environ, "LARK_AGENT_FEISHU": fake})
        # Far more than a pipe holds, so it is still being written when the process ends.
        self.assertRaisesRegex(ConnectionError, "飞书模块进程已结束", serve.call, "sheets-cells-set", {"writes": "x" * (4 << 20)})
        serve.close()
        # As the bridge sees it: no answer, which a write takes as an unknown result.
        bridge = Bridge(FeishuServe({**os.environ, "LARK_AGENT_FEISHU": fake}).call)
        self.fails(lambda: bridge.call("sheets-cells-set", {}, write=True), "结果未知")

    def test_not_found(self) -> None:
        """A Feishu module that cannot be found is said as such, not as a failed bridge call."""
        serve = FeishuServe({**os.environ, "LARK_AGENT_FEISHU": os.path.join(tmp(), "nowhere", "feishu.py")})
        bridge = Bridge(serve.call)
        self.fails(bridge.doctor, r"^LARK_AGENT_FEISHU 不是 feishu\.py 的绝对路径$")
        self.fails(lambda: bridge.cells_set("shtABCDEFGH", [{"sheet_id": "s", "range": "A1:A1", "cells": [[{"value": 1}]], "allow_overwrite": False}]), "^LARK_AGENT_FEISHU")
        self.assertEqual(bridge.check_login(), {"status": "blocked", "reason": "LARK_AGENT_FEISHU 不是 feishu.py 的绝对路径"})
        # Run outside lark-agent (nothing names the module): the message says so.
        with self.assertRaises(Blocked) as caught:
            locate_feishu({})
        self.assertEqual(caught.exception.message, MISSING)
        entry = write_file(os.path.join(tmp(), "feishu-sdk", "feishu.py"), "")
        self.assertEqual(locate_feishu({"LARK_AGENT_FEISHU": entry}), entry)

    def test_dropdown_keeps_rule(self) -> None:
        """A dropdown keeps the options of the rule it follows and adds only what the request asks for."""
        rule = {"sheet_id": "s1", "ranges": ["E2:E5"], "type": "list", "options": ["a", "b"], "multiple": False, "highlight": True, "colors": {"a": "#111111", "b": "#222222"}}
        # `spec.options` comes from the XLSX export, which can carry the options of a stray rule on a few old cells; none of that may be written.
        plan = {"range": "E6:E8", "request": {"groups": [{"secondary_tag": "2min"}, {"secondary_tag": "a"}, {}]},
                "dropdowns": [{"field": "secondary_tag", "range": "E6:E8", "options": ["stray1", "stray2", "a", "2min"], "missing": ["stray1", "stray2", "2min"]}]}
        d = dropdowns(plan, "s1", [rule])[0]
        self.assertEqual([d["options"], d["missing"], d["colors"][:2]], [["a", "b", "2min"], ["2min"], ["#111111", "#222222"]])

    def test_color_case(self) -> None:
        """Dropdown colors are the same color in either letter case."""
        sheet = [{"sheet_id": "s1", "grid_properties": {"row_count": 10, "column_count": 5}}]

        def rule(color: str) -> dict:
            return {"dataValidationType": "list", "conditionValues": ["a"], "ranges": ["s1!A2:A5"], "options": {"colorValueMap": {"a": color}, "highlightValidData": True}}

        def colors(color: str) -> dict:
            return read_rules(Bridge(lambda n, p: reply({"sheetId": "s1", "dataValidations": [rule(color)]})), "tok", sheet)[0]["colors"]

        self.assertEqual(colors("#BACEFD"), {"a": "#bacefd"})
        self.assertEqual(colors("#bacefd"), {"a": "#bacefd"})

    def test_rule_reply_shape(self) -> None:
        """A dropdown-rule reply of the wrong shape is refused with its field names and types, never its values."""
        sheet = [{"sheet_id": "s1", "grid_properties": {"row_count": 10, "column_count": 5}}]

        def read(data: object):
            return read_rules(Bridge(lambda n, p: reply(data)), "tok", sheet)

        self.assertEqual(read({"sheetId": "s1", "dataValidations": []}), [])
        # The real reply for a sheet without dropdowns leaves `dataValidations` out entirely.
        self.assertEqual(read({"sheetId": "s1", "revision": 7, "spreadsheetToken": "tok"}), [])
        error = self.fails(lambda: read({"sheetId": "s1", "secret": "hunter2"}), "sheetId 一致；字段 sheetId:string,secret:string")
        self.assertNotIn("hunter2", str(error))
        self.fails(lambda: read({"dataValidations": []}), "sheetId 缺失；字段 dataValidations:array")
        self.fails(lambda: read({"sheetId": "other", "dataValidations": []}), "sheetId 与请求不同")
        self.fails(lambda: read({"sheetId": "s1", "dataValidations": None}), "dataValidations:null")
        self.fails(lambda: read([]), "sheetId 缺失")


def production_node() -> dict:
    return {"node": {"node_token": NODE, "obj_type": "sheet", "obj_token": "shtPRODUCTION"}}


class FakeSheets:
    """The server side of the safety probe: it refuses to write over a non-empty cell, like the real allow_overwrite=false."""

    def __init__(self) -> None:
        self.enforce = True
        self.probe_cells: dict[str, str] = {}
        self.writes: list[str] = []
        self.production = None

    def handle(self, name: str, payload: dict) -> dict:
        if name == "info":
            return reply({"bridge": BRIDGE_VERSION, "app_configured": True, "node": "v24"})
        if name == "auth-status":
            return reply({"logged_in": True, "name": "李小明", "open_id": "ou_1", "storage": "host"})
        if name == "auth-check-scopes":
            return reply({"complete": True, "granted": payload["required"], "missing": []})
        if name == "sheets-create":
            return reply({"spreadsheet_token": "shtPROBE12345"})
        token = (payload or {}).get("token")
        if token is None:
            m = re.search(r"spreadsheets/(\w+)/", (payload or {}).get("path", ""))
            token = m.group(1) if m else None
        if token == "shtPROBE12345":
            return self._probe(name, payload)
        return self.production(name, payload)

    def _probe(self, name: str, payload: dict) -> dict:
        if name == "api-get":
            if payload["path"].endswith("/sheets/query"):
                return reply({"sheets": [{"sheet_id": "p1", "resource_type": "sheet", "title": "Sheet1", "index": 0}]})
            if payload["path"].endswith("/values_batch_get"):
                return reply({"valueRanges": [{"values": [[self.probe_cells.get("A1", ""), self.probe_cells.get("B1", "")]]}]})
        if name == "sheets-cells-set":
            for write in payload["writes"]:
                cols = ["B"] if write["range"] == "B1:B1" else ["A"] if write["range"] == "A1:A1" else ["A", "B"]
                targets = [f"{c}1" for c in cols]
                if self.enforce and any(t in self.probe_cells for t in targets):
                    return failure("occupied")
                for i, cell in enumerate(write["cells"][0]):
                    self.probe_cells[targets[i]] = cell.get("formula", cell.get("value"))
                self.writes.append(write["range"])
            return reply({"applied": True})
        raise RuntimeError(f"unexpected probe call {name}")


class FakeProduction:
    def __init__(self) -> None:
        self.appended = False
        self.production_writes = 0
        self.fail_write = None
        #: What the workbook looks like before the write (a fixture, or a file of the test's own).
        self.base = fixture("base.xlsx")
        #: What the workbook looks like once the write landed: the appended rows, plus whatever colleagues changed meanwhile.
        self.after = fixture("applied-complete.xlsx")
        #: How long an export takes (seconds).
        self.delay = 0.0
        #: A write that lands but whose answer is lost on the way back.
        self.land_then_fail = None
        #: Dropdown rules colleagues change once the write is in.
        self.colleague_rules = lambda rules: rules
        self.extra_rules: list[dict] = []
        self.sheets = [{"sheet_id": "s1", "title": "说明"}, {"sheet_id": "s2", "title": "GOFCN创意进度 26年2月"}, {"sheet_id": "s3", "title": "GOFCN创意进度 26年3月"}]
        self._lock = threading.Lock()

    def handle(self, name: str, payload: dict) -> dict:
        if name == "api-get":
            p = payload["path"]
            if p.endswith("/spaces/get_node"):
                return reply(production_node())
            if p.endswith("/sheets/query"):
                return reply({"sheets": [{**s, "index": i, "resource_type": "sheet", "grid_properties": {"row_count": 1000, "column_count": 20}} for i, s in enumerate(self.sheets)]})
            if p.endswith("/dataValidation"):
                sid = str(payload["params"]["range"]).split("!")[0]
                return reply({"sheetId": sid, "dataValidations": self._main_rules() if sid == "s3" else []})
        if name == "sheets-revision":
            return reply({"revision": 42})
        if name == "sheets-export":
            if self.delay:
                time.sleep(self.delay)
            with open(self.after if self.appended else self.base, "rb") as src, open(payload["output"], "xb") as dst:
                shutil.copyfileobj(src, dst)
            return reply({"path": payload["output"], "bytes": 1})
        if name == "sheets-cells-set":
            if self.fail_write:
                return self.fail_write
            assert all(w["allow_overwrite"] is False for w in payload["writes"])
            with self._lock:
                self.appended = True
                self.production_writes += 1
            for w in payload["writes"]:
                first = w["cells"][0][0].get("data_validation")
                if first:
                    self.extra_rules.append({"dataValidationType": "list", "conditionValues": first["items"], "ranges": [f"s3!{w['range']}"],
                                             "options": {"multipleValues": first["support_multiple_values"], "highlightValidData": first["enable_highlight"], "colorValueMap": {}}})
            if self.land_then_fail:
                return self.land_then_fail
            return reply({"applied": True})
        return FakeSheets().handle(name, payload)

    def _main_rules(self) -> list[dict]:
        def rule(items: list[str], rng: str) -> dict:
            return {"dataValidationType": "list", "conditionValues": items, "ranges": [f"s3!{rng}"], "options": {"multipleValues": False, "highlightValidData": False, "colorValueMap": {}}}

        rules = [rule(["玩法", "画面", "系统"], "D2:D500"), rule(["卖点", "体验"], "E2:E500"), rule(["视频", "图文"], "F2:F500"),
                 rule(["供应商甲", "供应商乙"], "H2:H5"), rule(["张三", "李四"], "I2:I500"), *self.extra_rules]
        return self.colleague_rules(rules) if self.appended else rules


def approved_batch(test: unittest.TestCase, set_up=None) -> dict:
    fake = FakeProduction()
    if set_up:
        set_up(fake)
    probe_server = FakeSheets()
    probe_server.production = fake.handle
    bridge = Bridge(probe_server.handle)
    base = Snapshot(fake.base)
    request = read_json(os.path.join(fixture("requests"), "basic.json"))
    approved = plan_with_api(bridge, base, request)
    test.assertEqual(approved["api_plan"]["sheet_id"], "s3")
    test.assertEqual([[d["field"], d["range"], d["missing"]] for d in approved["api_plan"]["dropdowns"]],
                     [["primary_tag", "D6:D8", []], ["secondary_tag", "E6:E8", []], ["creative_form", "F6:F8", []], ["supplier", "H6:H8", ["供应商丙"]], ["creator", "I6:I8", []]])
    recomputed = recompute_api_plan(bridge, base, approved)
    directory = prepare(approved["batch_plan"], base, output_root=tmp(), api_plan=recomputed)["batch"]
    receipt = os.path.join(tmp(), "receipt.json")
    probe(bridge, receipt)
    return {"fake": fake, "bridge": bridge, "dir": directory, "receipt": receipt, "base": base, "approved": approved}


def changing_bridge(bridge: Bridge, revision: int) -> Bridge:
    inner = bridge._run
    return Bridge(lambda name, payload: reply({"revision": revision}) if name == "sheets-revision" else inner(name, payload))


def with_rule(name: str, rule: str) -> str:
    """A fixture with one more dropdown rule on the appended sheet, as the XLSX export can show one that the API has elsewhere or not at all."""
    with open(fixture(name), "rb") as handle:
        zip_ = ZipReader(handle.read())
    sheet = "xl/worksheets/sheet3.xml"
    xml = re.sub(r'<dataValidations count="([0-9]+)">', lambda m: f'<dataValidations count="{int(m.group(1)) + 1}">{rule}', zip_.text(sheet), count=1)
    assert xml != zip_.text(sheet)
    file = os.path.join(tmp(), name)
    write_file(file, make_zip([{"name": n, "data": xml if n == sheet else zip_.read(n)} for n in zip_.names()]))
    return file


class AppendTests(unittest.TestCase):
    def fails(self, fn, pattern: str, kind: type = Blocked) -> Exception:
        with self.assertRaises(kind) as caught:
            fn()
        self.assertRegex(str(caught.exception), pattern)
        return caught.exception

    def test_probe(self) -> None:
        """The probe records three refused overwrites; its receipt opens production appends only for this bridge."""
        server = FakeSheets()

        def production(name: str, payload: dict) -> dict:
            assert payload["path"].endswith("/spaces/get_node")
            return reply(production_node())

        server.production = production
        bridge, _ = bridge_with(server.handle)
        directory = tmp()
        receipt = os.path.join(directory, "receipt.json")
        result = probe(bridge, receipt)
        self.assertEqual(result["status"], "safety_verified")
        self.assertEqual(server.writes, ["A1:B1"])
        data = acceptance(receipt, BRIDGE_VERSION)
        self.assertEqual([data["bridge_version"], data["schema"]], [BRIDGE_VERSION, 2])
        self.assertEqual(data["passed"], ["occupied_value", "empty_formula", "repeated_occupancy"])
        self.assertNotEqual(data["probe_workbook"], data["production_token"])
        body = {k: v for k, v in data.items() if k != "receipt_id"}
        for forged in ({**body, "bridge_version": "feishu-sdk-bridge/0"}, {**body, "schema": 1},
                       {**{k: v for k, v in body.items() if k != "bridge_version"}, "schema": 1, "cli_version": "1.0.98"}):
            file = write_file(os.path.join(directory, "f.json"), json.dumps(seal(forged, "receipt_id"), ensure_ascii=False))
            self.assertRaises(Blocked, acceptance, file, BRIDGE_VERSION)
            os.remove(file)
        self.assertRaises(Blocked, acceptance, write_file(os.path.join(directory, "edited.json"), json.dumps({**data, "passed": []}, ensure_ascii=False)), BRIDGE_VERSION)
        # An existing receipt is never overwritten, and nothing is sent.
        other, calls = bridge_with(server.handle)
        self.fails(lambda: probe(other, receipt), "不能覆盖")
        self.assertEqual(len(calls), 0)

    def test_probe_fails(self) -> None:
        """A server that overwrites fails the probe and no receipt is written."""
        server = FakeSheets()
        server.enforce = False
        server.production = lambda name, payload: reply(production_node())
        receipt = os.path.join(tmp(), "receipt.json")
        self.fails(lambda: probe(bridge_with(server.handle)[0], receipt), "防覆盖探测失败")
        self.assertFalse(os.path.exists(receipt))

    def test_leftover_attempt(self) -> None:
        """An append killed before anything was sent leaves no folder that stops the next one."""
        t = approved_batch(self)
        leftover = os.path.join(t["dir"], f"api-attempt-{str(len(status(t['dir'])['events']) + 1).zfill(6)}")
        os.mkdir(leftover)
        write_file(os.path.join(leftover, "preflight.xlsx"), "left over")
        self.assertEqual(execute(t["bridge"], t["dir"], t["receipt"])["status"], "append_verified")
        self.assertEqual(t["fake"].production_writes, 1)

    def test_whole_append(self) -> None:
        """A whole append: plan, prepare, safety receipt, one write, read back and verified, then a read-only reconcile."""
        t = approved_batch(self)
        result = execute(t["bridge"], t["dir"], t["receipt"])
        self.assertEqual(result["status"], "append_verified")
        self.assertEqual(result["range"], "A6:I8")
        self.assertEqual(result["sheet_url"], "https://centurygames.feishu.cn/wiki/GH7cwIxzZi3gVdk2s3tc1dDbnQc?sheet=s3")
        self.assertEqual(read_json(result["report"])["links"]["progress_sheet"], result["sheet_url"])
        self.assertEqual(t["fake"].production_writes, 1)
        state = status(t["dir"])
        self.assertEqual([state["append_verified"], state["blocked"]], [True, False])
        self.assertTrue(os.path.exists(result["report"]))
        self.assertNotIn("bearer", js.canonical(state).lower())
        # Nothing is ever sent twice.
        self.fails(lambda: execute(t["bridge"], t["dir"], t["receipt"]), "已有接口写入尝试")
        self.assertEqual(reconcile(t["bridge"], t["dir"])["status"], "complete")
        self.assertTrue(t["fake"].appended)

    def test_unknown_write(self) -> None:
        """A write whose result is unknown blocks the batch instead of retrying."""
        t = approved_batch(self)
        t["fake"].fail_write = failure("unknown_result")
        self.fails(lambda: execute(t["bridge"], t["dir"], t["receipt"]), "结果未知")
        self.assertTrue(status(t["dir"])["blocked"])
        t["fake"].fail_write = None
        self.fails(lambda: execute(t["bridge"], t["dir"], t["receipt"]), "已有接口写入尝试")

    def test_changed_revision(self) -> None:
        """An append is refused when the spreadsheet changed since the approved plan."""
        t = approved_batch(self)
        self.fails(lambda: execute(changing_bridge(t["bridge"], 43), t["dir"], t["receipt"]), "表格版本变化")
        self.assertEqual(len(status(t["dir"])["events"]), 0)
        # A refusal before anything was sent leaves no attempt directory behind, so the same batch can try again.
        self.assertEqual([n for n in os.listdir(t["dir"]) if n.startswith("api-attempt-")], [])
        self.assertEqual(execute(t["bridge"], t["dir"], t["receipt"])["status"], "append_verified")

    def test_colleagues_elsewhere(self) -> None:
        """Colleagues editing elsewhere in the shared workbook while the append runs do not fail it: their edits are listed, the append is verified."""
        for after in ("applied-outside-value.xlsx", "applied-outside-sheet.xlsx", "applied-outside-style.xlsx", "applied-filter-changed.xlsx", "applied-extra-sheet.xlsx"):
            t = approved_batch(self)
            t["fake"].after = fixture(after)
            result = execute(t["bridge"], t["dir"], t["receipt"])
            self.assertEqual(result["status"], "append_verified", after)
            self.assertGreater(len(result["foreign_changes"]), 0, after)
            self.assertTrue(status(t["dir"])["append_verified"], after)

    def test_colleague_dropdowns(self) -> None:
        """A dropdown a colleague changes in a column the append does not write is theirs; one changed in a written column still fails it."""
        other = approved_batch(self)
        other["fake"].colleague_rules = lambda rules: [*rules, {**rules[0], "conditionValues": ["是", "否"], "ranges": ["s3!K2:K500"]}]
        self.assertEqual(execute(other["bridge"], other["dir"], other["receipt"])["status"], "append_verified")
        written = approved_batch(self)
        written["fake"].colleague_rules = lambda rules: [{**r, "conditionValues": [*r["conditionValues"], "新玩法"]} if i == 0 else r for i, r in enumerate(rules)]
        self.fails(lambda: execute(written["bridge"], written["dir"], written["receipt"]), "写入已发出.*历史下拉规则", WriteSent)

    def test_stray_export_rule(self) -> None:
        """A dropdown the XLSX export lists with options the API's rule lacks does not fail a correct append: the new rows are judged by the rules read back through the API."""
        stray = '<dataValidation sqref="E2:E1048576" type="list" allowBlank="1"><formula1>"老选项"</formula1></dataValidation>'

        def set_up(fake: FakeProduction) -> None:
            fake.base = with_rule("base.xlsx", stray)
            fake.after = with_rule("applied-complete.xlsx", stray)
            # Through the API the stray rule is on the one old cell it really is on.
            fake.extra_rules.append({"dataValidationType": "list", "conditionValues": ["老选项"], "ranges": ["s3!E3:E3"],
                                     "options": {"multipleValues": False, "highlightValidData": False, "colorValueMap": {}}})

        t = approved_batch(self, set_up)

        def options(plan: dict) -> list[str]:
            return next(d for d in plan["dropdowns"] if d["field"] == "secondary_tag")["options"]

        self.assertEqual([options(t["approved"]["batch_plan"]), options(t["approved"]["api_plan"])], [["老选项", "卖点", "体验"], ["卖点", "体验"]],
                         "the plan reads the export's list, the append writes the API's")
        self.assertEqual(execute(t["bridge"], t["dir"], t["receipt"])["status"], "append_verified")
        self.assertEqual(t["fake"].production_writes, 1)
        self.assertEqual(reconcile(t["bridge"], t["dir"])["status"], "complete")

        # What the append wrote is still checked: a new row whose dropdown reads back without one of its options fails it.
        def lose(fake: FakeProduction) -> None:
            set_up(fake)
            fake.colleague_rules = lambda rules: [{**r, "conditionValues": ["卖点"]} if r["ranges"][0] == "s3!E6:E8" else r for r in rules]

        lost = approved_batch(self, lose)
        self.fails(lambda: execute(lost["bridge"], lost["dir"], lost["receipt"]), "写入已发出.*新增下拉规则缺失/冲突", WriteSent)

    def test_two_at_once(self) -> None:
        """Two appends of one batch at once send one write: the first is verified, the second is refused before it sends anything."""
        t = approved_batch(self, lambda f: setattr(f, "delay", 0.3))
        outcomes: dict[str, object] = {}

        def run(key: str, wait: float) -> None:
            time.sleep(wait)
            try:
                outcomes[key] = execute(t["bridge"], t["dir"], t["receipt"])
            except Exception as error:  # noqa: BLE001
                outcomes[key] = error

        threads = [threading.Thread(target=run, args=("first", 0)), threading.Thread(target=run, args=("second", 0.1))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertIsInstance(outcomes["first"], dict, repr(outcomes["first"]))
        self.assertEqual(outcomes["first"]["status"], "append_verified")
        self.assertTrue(isinstance(outcomes["second"], Blocked) and not isinstance(outcomes["second"], WriteSent), repr(outcomes["second"]))
        self.assertEqual(t["fake"].production_writes, 1)
        self.assertEqual([status(t["dir"])["append_verified"], status(t["dir"])["blocked"]], [True, False])
        # The first run's export and proof are where its record says: the report verifies them again.
        self.assertEqual(report(t["dir"])["status"], "report_ready")

    def test_touching_rows(self) -> None:
        """What touches the appended rows still fails the append, and every refusal after the send says the write went out."""
        for after, issue in (("applied-occupied-optional.xlsx", "J6"), ("applied-bad-value.xlsx", "D6"), ("applied-protected.xlsx", "保护")):
            t = approved_batch(self)
            t["fake"].after = fixture(after)
            error = self.fails(lambda: execute(t["bridge"], t["dir"], t["receipt"]), "写入已发出.*reconcile.*追加未完整通过验收", WriteSent)
            self.assertEqual(error.batch, t["dir"])
            self.assertTrue(status(t["dir"])["blocked"], after)
            readback = reconcile(t["bridge"], t["dir"])
            self.assertEqual(readback["status"], "conflict", after)
            self.assertRegex(" ".join(readback["issues"]), issue, after)

    def test_reconcile_nowhere(self) -> None:
        """Reconcile of a write that landed nowhere gives its verdict even though the new rows have no dropdowns."""
        t = approved_batch(self)
        t["fake"].fail_write = failure("unknown_result")
        self.fails(lambda: execute(t["bridge"], t["dir"], t["receipt"]), "写入已发出", WriteSent)
        readback = reconcile(t["bridge"], t["dir"])
        self.assertEqual(readback["status"], "not_applied")
        self.assertRegex(readback["rule_issue"], "新增下拉规则")

    def test_landed_unverified(self) -> None:
        """A request whose earlier write landed but was never verified cannot be planned and appended again; one that landed nowhere can."""
        landed = approved_batch(self)
        landed["fake"].land_then_fail = failure("unknown_result")
        self.fails(lambda: execute(landed["bridge"], landed["dir"], landed["receipt"]), "写入已发出", WriteSent)
        request = read_json(os.path.join(fixture("requests"), "basic.json"))
        # The sheet as it is now: the rows of the lost answer are in it, so a new plan would put them in again after themselves.
        now = Snapshot(fixture("applied-complete.xlsx"))
        again = plan_with_api(landed["bridge"], now, request)
        self.assertNotEqual(again["batch_plan"]["first_number"], landed["approved"]["batch_plan"]["first_number"])
        self.assertRaisesRegex(Blocked, "已有一批写入发出、尚未核对", prepare, again["batch_plan"], now, output_root=os.path.dirname(landed["dir"]), api_plan=again["api_plan"])

        nowhere = approved_batch(self)
        nowhere["fake"].fail_write = failure("unknown_result")
        self.fails(lambda: execute(nowhere["bridge"], nowhere["dir"], nowhere["receipt"]), "写入已发出", WriteSent)
        retry = recompute_api_plan(nowhere["bridge"], nowhere["base"], nowhere["approved"])
        # Same sheet as approved (nothing landed): a batch of its own beside the first, which keeps its record.
        fresh = prepare(nowhere["approved"]["batch_plan"], nowhere["base"], output_root=os.path.dirname(nowhere["dir"]), api_plan=retry)
        self.assertEqual(fresh["status"], "prepared")
        self.assertEqual(os.path.basename(fresh["batch"]), f"{os.path.basename(nowhere['dir'])}-2")
        nowhere["fake"].fail_write = None
        self.assertEqual(execute(nowhere["bridge"], fresh["batch"], nowhere["receipt"])["status"], "append_verified")

    def test_api_batch_cls_evidence(self) -> None:
        """A batch made through the bridge takes CLS evidence only as API receipts, whatever the evidence says about itself."""
        t = approved_batch(self)
        execute(t["bridge"], t["dir"], t["receipt"])
        record(t["dir"], "cls_started", "01")
        shot = write_file(os.path.join(tmp(), "shot.png"), "x")
        forged = {"confirmed": True, "unpublished": True, "url": "https://creative.centurygames.cn/requirement/1/edit-outer", "screenshot": shot}
        self.assertRaisesRegex(Blocked, "transport=cls_api", record, t["dir"], "draft_saved", "01", forged)
        self.assertRaisesRegex(Blocked, "transport=cls_api", record, t["dir"], "draft_saved", "01", {**forged, "transport": "ui"})
        self.assertRaisesRegex(Blocked, "不属于某个组|追加验证", record, t["dir"], "append_verified", "01", {})

    def test_command_line_refusal(self) -> None:
        """The command line reports a refusal as blocked JSON with exit code 2."""
        result = main(["batch", "status", "--batch", os.path.join(tmp(), "nothing")])
        self.assertEqual(result["code"], 2)
        self.assertEqual(result["result"]["status"], "blocked")
        self.assertEqual(main(["frobnicate"])["code"], 2)
        self.assertEqual(main(["batch", "plan", "--bogus", "x"])["code"], 2)

    def test_context(self) -> None:
        """The context gives the target sheet, the next number and the dropdown options, and no earlier rows or batches."""
        fake = FakeProduction()
        probe_server = FakeSheets()
        probe_server.production = fake.handle
        result = context(Bridge(probe_server.handle), Snapshot(fixture("base.xlsx")))
        self.assertEqual([result["worksheet"], result["sheet_id"], result["next_number"]], ["GOFCN创意进度 26年3月", "s3", 1011])
        # How the next row will be named, so that files named for their rows can be split into its parts.
        self.assertEqual(result["naming"], "GOFCN<素材编号>_<一级标签>_<二级标签>_<创意形式>_<提炼词>")
        self.assertEqual(result["options"], {"primary_tag": ["玩法", "画面", "系统"], "secondary_tag": ["卖点", "体验"], "creative_form": ["视频", "图文"],
                                             "supplier": ["供应商甲", "供应商乙"], "creator": ["张三", "李四"]})
        self.assertFalse("recent" in result or "past_requests" in result)

    def test_recompute(self) -> None:
        """The approved plan is made again on a fresh snapshot: a new revision is fine, changed rows or numbers need a new approval."""
        t = approved_batch(self)
        same = recompute_plan(t["bridge"], t["base"], t["approved"])
        self.assertEqual(js.canonical(same), js.canonical(t["approved"]))
        # The approved dropdown additions (or anybody's edit elsewhere) give the workbook a new revision.
        later = changing_bridge(t["bridge"], 43)
        self.fails(lambda: recompute_api_plan(later, t["base"], t["approved"]), "版本/规则与获批计划变化")
        fresh = recompute_plan(later, t["base"], t["approved"])
        self.assertEqual(fresh["api_plan"]["revision"], 43)
        self.assertEqual(js.canonical(fresh["batch_plan"]), js.canonical(t["approved"]["batch_plan"]))
        moved = Snapshot(fixture("applied-complete.xlsx"))
        self.fails(lambda: recompute_plan(t["bridge"], moved, t["approved"]), "素材编号或名称与获批计划不同")
        self.fails(lambda: recompute_plan(t["bridge"], t["base"], {**t["approved"], "batch_plan": {**t["approved"]["batch_plan"], "range": "A7:I9"}}), "获批计划被修改过")
        self.fails(lambda: recompute_plan(t["bridge"], t["base"], {"batch_plan": t["approved"]["batch_plan"]}), "获批计划不完整")

    def test_plan_output(self) -> None:
        """plan --output saves the whole plan and prints only what the user is shown, with the dropdown options to add."""
        t = approved_batch(self)
        out = os.path.join(tmp(), "plan.json")
        args = ["feishu", "plan", "--request", os.path.join(fixture("requests"), "basic.json"), "--snapshot", fixture("base.xlsx"), "--output", out]
        answer = main(args, t["bridge"])
        self.assertEqual(answer["code"], 0, answer["result"])
        result = answer["result"]
        self.assertEqual(sorted(read_json(out).keys()), ["api_plan", "batch_plan"])
        self.assertEqual([result["status"], result["worksheet"], result["sheet_id"], result["range"], result["first_number"], result["last_number"]],
                         ["plan_saved", "GOFCN创意进度 26年3月", "s3", "A6:I8", 1011, 1013])
        self.assertEqual(result["groups"][0], {"group": "01", "quantity": 2, "requirement_name": "GOFCN1011-GOFCN1012", "supplier": "供应商丙", "reviewer": "张三",
                                               "script_url": "https://centurygames.feishu.cn/docx/ABC123def", "message_kind": "玩法*2",
                                               "items": [{"quantity": 2, "first": "GOFCN1011_玩法_卖点_视频_新爆点", "last": "GOFCN1012_玩法_卖点_视频_新爆点"}]})
        self.assertEqual(result["new_options"], [{"field": "supplier", "option": "供应商丙"}])
        again = main(args, t["bridge"])
        self.assertEqual([again["code"], again["result"]["reason"]], [2, "计划文件已存在，不覆盖"])

    def test_plan_without_ticket(self) -> None:
        """A plan of rows without a CLS ticket (finished materials) shows no requirement name, script link or group message."""
        t = approved_batch(self)
        directory = tmp()
        request = {"groups": [{"supplier": "供应商丙", "reviewer": "张三", "primary_tag": "玩法", "secondary_tag": "卖点", "creative_form": "视频",
                               "items": [{"quantity": 1, "keyword": "企鹅保龄球_竖版", "material_name": "GOFCNxxxxx_玩法_卖点_视频_企鹅保龄球_竖版"}]}]}
        write_file(os.path.join(directory, "request.json"), json.dumps(request, ensure_ascii=False))
        answer = main(["feishu", "plan", "--request", os.path.join(directory, "request.json"), "--snapshot", fixture("base.xlsx"), "--output", os.path.join(directory, "plan.json")], t["bridge"])
        self.assertEqual(answer["code"], 0, answer["result"])
        self.assertEqual(answer["result"]["groups"][0], {"group": "01", "quantity": 1, "supplier": "供应商丙", "reviewer": "张三",
                                                         "items": [{"quantity": 1, "first": "GOFCN1011_玩法_卖点_视频_企鹅保龄球_竖版", "last": "GOFCN1011_玩法_卖点_视频_企鹅保龄球_竖版"}]})

    def test_plan_keywords(self) -> None:
        """The plan shows each row's 提炼词 and says how many rows have none of their own (the default 关键词), for the user to confirm."""
        t = approved_batch(self)
        directory = tmp()
        cls = {"direction": "方向一", "task_type": "creative_a"}
        request = {"groups": [
            {"supplier": "供应商丙", "reviewer": "张三", "script_url": "https://centurygames.feishu.cn/docx/ABC123def", "primary_tag": "玩法", "secondary_tag": "卖点",
             "creative_form": "视频", "cls": cls, "items": [{"quantity": 1, "keyword": "企鹅保龄球"}, {"quantity": 1, "keyword": "冻鱼飞盘"}, {"quantity": 2, "keyword": "围炉煮冰"}]},
            {"supplier": "供应商甲", "reviewer": "李四", "script_url": "https://centurygames.feishu.cn/wiki/XYZ789", "primary_tag": "玩法", "secondary_tag": "卖点",
             "creative_form": "视频", "quantity": 2, "cls": cls}]}
        write_file(os.path.join(directory, "request.json"), json.dumps(request, ensure_ascii=False))
        answer = main(["feishu", "plan", "--request", os.path.join(directory, "request.json"), "--snapshot", fixture("base.xlsx"), "--output", os.path.join(directory, "plan.json")], t["bridge"])
        self.assertEqual(answer["code"], 0, answer["result"])
        result = answer["result"]
        self.assertEqual([[i["quantity"], i["first"], i["last"]] for i in result["groups"][0]["items"]], [
            [1, "GOFCN1011_玩法_卖点_视频_企鹅保龄球", "GOFCN1011_玩法_卖点_视频_企鹅保龄球"], [1, "GOFCN1012_玩法_卖点_视频_冻鱼飞盘", "GOFCN1012_玩法_卖点_视频_冻鱼飞盘"],
            [2, "GOFCN1013_玩法_卖点_视频_围炉煮冰", "GOFCN1014_玩法_卖点_视频_围炉煮冰"]])
        self.assertNotIn("default_keyword_rows", result["groups"][0], "every row has a 提炼词 of its own")
        self.assertEqual(result["groups"][1]["default_keyword_rows"], 2)
        self.assertEqual(result["groups"][1]["items"][0]["first"], "GOFCN1015_玩法_卖点_视频_关键词")


class FakeDocs:
    def __init__(self) -> None:
        self.tabs = [
            {"sheet_id": "t1", "title": "2026.10.10", "index": 0, "resource_type": "sheet", "hidden": False, "grid_properties": {"row_count": 4, "column_count": 4}},
            {"sheet_id": "t2", "title": "2026.10.16", "index": 2, "resource_type": "sheet", "hidden": False, "grid_properties": {"row_count": 4, "column_count": 4}},
            {"sheet_id": "tx", "title": "隐藏", "index": 3, "resource_type": "sheet", "hidden": True, "grid_properties": {"row_count": 4, "column_count": 4}},
            {"sheet_id": "tb", "title": "多维表格", "index": 1, "resource_type": "bitable", "hidden": False}]
        self.values = [["序号", "创意形式\n（制作形式）", "脚本", None], [1, "AI剧情", "x" * 400, [{"type": "attachment", "text": "1.mp4", "fileToken": "f1"}]],
                       [None, None, "", None], [2, "设定3.0", [{"type": "text", "text": "短"}, {"type": "mention", "text": "@张三"}], {"type": "embed-image", "fileToken": "g1"}]]
        self.ranges: list[str] = []

    def handle(self, name: str, payload: dict) -> dict:
        assert name == "api-get"
        p = payload["path"]
        if p.endswith("/spaces/get_node"):
            nodes = {"wikiSHEET": {"obj_type": "sheet", "obj_token": "shtREQ", "title": "丁杰AI", "parent_node_token": "wikiDIR"},
                     "wikiDIR": {"obj_type": "docx", "obj_token": "doxDIR", "title": "张天伟", "parent_node_token": "wikiROOT"},
                     "wikiROOT": {"obj_type": "docx", "obj_token": "doxROOT", "title": "点点互动", "parent_node_token": ""},
                     "wikiDOC": {"obj_type": "docx", "obj_token": "doxREQ", "title": "丁杰代言人需求表"},
                     "wikiMIND": {"obj_type": "mindnote", "obj_token": "mndREQ", "title": "脑图"}}
            return reply({"node": nodes.get(payload["params"]["token"])})
        if p == "/open-apis/sheets/v3/spreadsheets/shtREQ":
            return reply({"spreadsheet": {"title": "杨燃对接文档"}})
        if p.endswith("/sheets/query"):
            return reply({"sheets": self.tabs})
        if p.endswith("/values_batch_get"):
            self.ranges.append(payload["params"]["ranges"])
            return reply({"valueRanges": [{"range": payload["params"]["ranges"], "values": self.values}]})
        if p == "/open-apis/docx/v1/documents/doxREQ/raw_content":
            return reply({"content": "丁杰代言人需求表\n2026.10.19\n根据给到的物料混剪产出44条素材"})
        raise RuntimeError(f"unexpected {p}")


class DocumentTests(unittest.TestCase):
    def test_sheet(self) -> None:
        """A requirement sheet is read as the linked tab (or the newest), its header and its non-empty rows, cells as plain text."""
        docs = FakeDocs()
        bridge, _ = bridge_with(docs.handle)
        linked = read_brief(bridge, "https://centurygames.feishu.cn/wiki/wikiSHEET?sheet=t1")
        self.assertEqual([linked["kind"], linked["title"], linked["sheet"]], ["sheet", "丁杰AI", {"sheet_id": "t1", "title": "2026.10.10", "chosen_by": "link"}])
        self.assertEqual(linked["path"], ["点点互动", "张天伟"])
        self.assertEqual(linked["tabs"], ["2026.10.10", "2026.10.16"])
        self.assertEqual(linked["columns"], ["A", "B", "C", "D"])
        self.assertEqual(linked["header"], ["序号", "创意形式\n（制作形式）", "脚本", ""])
        self.assertEqual([r["row"] for r in linked["rows"]], [2, 4])
        self.assertEqual(linked["rows"][0]["cells"], ["1", "AI剧情", f"{'x' * 300}…", "[附件:1.mp4]"])
        self.assertEqual(linked["rows"][1]["cells"], ["2", "设定3.0", "短@张三", "[图片]"])
        self.assertEqual([linked["row_count"], linked["clipped_cells"], docs.ranges[0]], [2, 1, "t1!A1:D4"])
        newest = read_brief(bridge, "https://centurygames.feishu.cn/wiki/wikiSHEET")
        self.assertEqual(newest["sheet"], {"sheet_id": "t2", "title": "2026.10.16", "chosen_by": "last"})
        asked = read_brief(bridge, "https://centurygames.feishu.cn/wiki/wikiSHEET", sheet="2026.10.10", full=True)
        self.assertEqual([asked["sheet"]["chosen_by"], len(asked["rows"][0]["cells"][2]), asked.get("clipped_cells")], ["asked", 400, None])
        direct = read_brief(bridge, "https://centurygames.feishu.cn/sheets/shtREQ?sheet=t2")
        self.assertEqual([direct["title"], direct["sheet"]["chosen_by"], direct.get("path")], ["杨燃对接文档", "link", None])
        self.assertRaisesRegex(Blocked, "链接指向的工作表不存在或已隐藏", read_brief, bridge, "https://centurygames.feishu.cn/wiki/wikiSHEET?sheet=tx")
        self.assertRaisesRegex(Blocked, "没有工作表“没有”", read_brief, bridge, "https://centurygames.feishu.cn/wiki/wikiSHEET", "没有")

    def test_docx(self) -> None:
        """A requirement doc is read as its text with its title; other kinds of document and other hosts are refused."""
        bridge, _ = bridge_with(FakeDocs().handle)
        doc = read_brief(bridge, "https://centurygames.feishu.cn/wiki/wikiDOC")
        self.assertEqual([doc["kind"], doc["title"], doc["chars"]], ["docx", "丁杰代言人需求表", 36])
        self.assertRegex(doc["text"], "产出44条素材")
        self.assertEqual(read_brief(bridge, "https://centurygames.feishu.cn/docx/doxREQ")["title"], "丁杰代言人需求表")
        self.assertRaisesRegex(Blocked, r"暂不支持读取这种飞书文档（mindnote）", read_brief, bridge, "https://centurygames.feishu.cn/wiki/wikiMIND")
        self.assertRaisesRegex(Blocked, "不是公司飞书文档链接", read_brief, bridge, "https://evil.example.com/docx/doxREQ")
        self.assertRaisesRegex(Blocked, "只用于表格", read_brief, bridge, "https://centurygames.feishu.cn/docx/doxREQ", "t1")

    def test_dropdown_add(self) -> None:
        """dropdown-add finds the column of each of the five fields by its header and adds to the whole column."""
        header: list = ["素材编号", "提炼词", "素材名称", "一级标签\n（方向）", "二级标签（卖点）", [{"type": "text", "text": "创意形式（制作形式）"}], "客户端", "设计师供应商", "创意者\n脚本审核人"]
        sent: list[dict] = []

        def handler(name: str, payload: dict) -> dict:
            if name == "info":
                return reply({"bridge": BRIDGE_VERSION, "app_configured": True, "node": "v24"})
            if name == "auth-status":
                return reply({"logged_in": True, "name": "李小明", "open_id": "ou_1", "storage": "host"})
            if name == "auth-check-scopes":
                return reply({"complete": True, "granted": payload["required"], "missing": []})
            if name == "sheets-dropdown-add":
                sent.append(payload)
                return reply({"updated_ranges": 2})
            p = payload["path"]
            if p.endswith("/spaces/get_node"):
                return reply(production_node())
            if p.endswith("/sheets/query"):
                return reply({"sheets": [{"sheet_id": "s3", "title": "26年1月-GOFCN", "index": 0, "resource_type": "sheet", "grid_properties": {"row_count": 7000, "column_count": 14}}]})
            if p.endswith("/values_batch_get"):
                assert payload["params"]["ranges"] == "s3!A1:N1"
                return reply({"valueRanges": [{"values": [header]}]})
            raise RuntimeError(f"unexpected {name} {p}")

        bridge, _ = bridge_with(handler)
        columns = []
        for field in ("primary", "secondary", "form", "supplier", "reviewer", "creative_form", "creator"):
            result = add_option(bridge, field, "新选项", "26年1月-GOFCN", "#bacefd")
            columns.append(result["column"])
            self.assertEqual(result["updated_ranges"], 4)
        self.assertEqual(columns, ["D", "E", "F", "H", "I", "F", "I"])
        self.assertEqual([[s["column"], s["first_row"], s["last_row"], s["option"], s["color"]] for s in sent[:2]],
                         [["D", 2, 5001, "新选项", "#bacefd"], ["D", 5002, 7000, "新选项", "#bacefd"]])
        self.assertRaisesRegex(Blocked, "--field 必须是", add_option, bridge, "client", "x", "26年1月-GOFCN")
        self.assertRaisesRegex(Blocked, "找不到工作表", add_option, bridge, "supplier", "x", "没有的表")
        header.append("供应商备注")
        self.assertRaisesRegex(Blocked, "找不到唯一的供应商列", add_option, bridge, "supplier", "x", "26年1月-GOFCN")


if __name__ == "__main__":
    unittest.main()
