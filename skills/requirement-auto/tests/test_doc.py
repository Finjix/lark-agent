"""Writing into the user's requirement document: outline, insert, fill, and the picture's checks."""

from __future__ import annotations

import copy
import json
import os
import re
import unittest
from typing import Any, Callable, Optional

from tests.helpers import temp_dir, write_file

from gof import js
from gof.cli import main
from gof.doc import edit_doc, outline
from gof.feishu import Bridge
from gof.agent_feishu import BRIDGE_VERSION
from gof.shots_online import doc_picture
from gof.util import Blocked, NODE

URL_ = "https://centurygames.feishu.cn/wiki/DocNode123456"
SHEET_URL = "https://centurygames.feishu.cn/sheets/ReqSheet123456?sheet=t1"


def ok(data: Any) -> dict:
    return {"ok": True, "data": data}


def fail(kind: str, message: str, code: Optional[int] = None) -> dict:
    error: dict[str, Any] = {"kind": kind, "message": message}
    if code:
        error["code"] = code
    return {"ok": False, "error": error}


def text(ident: str, content: str, parent: str, kind: int = 2) -> dict:
    return {"block_id": ident, "block_type": kind, "parent_id": parent, {2: "text", 3: "heading1"}[kind]: {"elements": [{"text_run": {"content": content}}], "style": {}}}


class FakeDoc:
    """A docx as Feishu keeps it: blocks by ID, the page's children in order, a revision that moves with every edit."""

    def __init__(self) -> None:
        self.blocks: dict[str, dict] = {}
        self.revision = 10
        self.n = 0
        self.blocks["PageBlock0001"] = {"block_id": "PageBlock0001", "block_type": 1, "children": [], "page": {"elements": [{"text_run": {"content": "达治-GOFCN-需求表"}}]}}
        self.add(text("HeadOld000001", "1023——AI片头衔接需求", "PageBlock0001", 3))
        self.table([["命名", "方向"], ["GOFCN1_a", ""], ["GOFCN2_b", "已有"]], [{"row_span": 1, "col_span": 1}, {"row_span": 1, "col_span": 1}, {"row_span": 1, "col_span": 1},
                                                                          {"row_span": 2, "col_span": 1}, {"row_span": 1, "col_span": 1}, {"row_span": 1, "col_span": 1}])

    def id(self) -> str:
        self.n += 1
        return f"Real{str(self.n).zfill(9)}"

    @property
    def page(self) -> dict:
        return self.blocks["PageBlock0001"]

    def add(self, block: dict, index: Optional[int] = None) -> None:
        self.blocks[block["block_id"]] = block
        self.page["children"].insert(len(self.page["children"]) if index is None else index, block["block_id"])

    def table(self, rows: list[list[str]], merge: Optional[list[dict]] = None) -> None:
        ident = self.id()
        cells = []
        for content in [c for row in rows for c in row]:
            cell = self.id()
            t = self.id()
            self.blocks[t] = text(t, content, cell)
            self.blocks[cell] = {"block_id": cell, "block_type": 32, "parent_id": ident, "children": [t], "table_cell": {}}
            cells.append(cell)
        prop: dict[str, Any] = {"row_size": len(rows), "column_size": len(rows[0])}
        if merge:
            prop["merge_info"] = merge
        self.add({"block_id": ident, "block_type": 31, "parent_id": "PageBlock0001", "children": cells, "table": {"cells": cells, "property": prop}})

    def insert(self, p: dict) -> dict:
        real = {d["block_id"]: self.id() for d in p["descendants"]}
        for d in p["descendants"]:
            block = {**copy.deepcopy(d), "block_id": real[d["block_id"]], "children": [real[c] for c in (d.get("children") or [])]}
            if block["block_type"] == 31:
                block["table"]["cells"] = block["children"]
            self.blocks[block["block_id"]] = block
        for offset, c in enumerate(p["children_id"]):
            self.page["children"].insert(p["index"] + offset, real[c])
        self.revision += 1
        return {"children": [], "block_id_relations": [{"temporary_block_id": t, "block_id": b} for t, b in real.items()]}

    def set_texts(self, p: dict) -> dict:
        for u in p["updates"]:
            self.blocks[u["block_id"]]["text"]["elements"] = u["elements"]
        self.revision += 1
        return {}


class Harness:
    def __init__(self, scopes: Optional[list[str]] = None, insert: Optional[Callable[[dict], dict]] = None) -> None:
        self.doc = FakeDoc()
        self.sheet: dict[str, str] = {"A2": "GOFCN9_x"}
        self.calls: list[dict] = []
        granted = ["docx:document"] if scopes is None else scopes

        def run(name: str, payload: dict) -> dict:
            self.calls.append({"name": name, "payload": payload})
            if name == "info":
                return ok({"bridge": BRIDGE_VERSION, "node": "v24", "app_configured": True})
            if name == "auth-status":
                return ok({"logged_in": True, "name": "测试", "open_id": "ou_x"})
            if name == "auth-check-scopes":
                missing = [s for s in payload["required"] if s not in granted and not s.startswith(("sheets", "wiki", "drive")) and s != "docx:document:readonly"]
                return ok({"complete": not missing, "granted": [s for s in payload["required"] if s not in missing], "missing": missing})
            if name == "api-get":
                if payload["path"] == "/open-apis/wiki/v2/spaces/get_node":
                    if payload["params"]["token"] == NODE:
                        return ok({"node": {"node_token": NODE, "obj_type": "sheet", "obj_token": "ProgressSheet1"}})
                    return ok({"node": {"obj_type": "docx", "obj_token": "DocToken12345", "title": "达治-GOFCN-需求表", "parent_node_token": ""}})
                if payload["path"].endswith("/sheets/query"):
                    return ok({"sheets": [{"sheet_id": "t1", "title": "1027", "index": 0, "resource_type": "sheet"}]})
                if payload["path"].endswith("/values_batch_get"):
                    return ok({"valueRanges": [{"range": r, "values": [[self.sheet.get(r.split("!")[1].split(":")[0])]]} for r in str(payload["params"]["ranges"]).split(",")]})
                return fail("invalid_request", f"unexpected {payload['path']}")
            if name == "docx-blocks":
                return ok({"revision": self.doc.revision, "title": "达治-GOFCN-需求表", "blocks": [copy.deepcopy(b) for b in self.doc.blocks.values()]})
            if name == "docx-insert":
                return insert(payload) if insert else ok(self.doc.insert(payload))
            if name == "docx-set-texts":
                return ok(self.doc.set_texts(payload))
            if name == "sheets-cells-set":
                for w in payload["writes"]:
                    cell = w["range"].split(":")[0]
                    if w["allow_overwrite"] is not False or self.sheet.get(cell):
                        return fail("occupied", "non-empty")
                for w in payload["writes"]:
                    self.sheet[w["range"].split(":")[0]] = w["cells"][0][0]["value"]
                return ok({})
            return fail("invalid_request", f"unexpected {name}")

        self.bridge = Bridge(run)

    def writes(self) -> list[dict]:
        return [c for c in self.calls if c["name"] in ("docx-insert", "docx-set-texts", "sheets-cells-set")]


ENTRY = {"insert": {"at": {"before": 0, "expect": "1023——AI片头衔接需求"}, "blocks": [
    {"heading1": "1027-AI片头需求"}, {"text": "需求数量：2条"}, {"table": [["命名", "方向"], ["GOFCN67867_巨手", "巨手"], ["GOFCN67868_巨手", ""]]}]}}


class DocTests(unittest.TestCase):
    def test_outline(self) -> None:
        """The outline shows the top level, a table by its first rows, a merged cell as None, and one block whole."""
        h = Harness()
        h.doc.table([["h"], ["1"], ["2"], ["3"], ["4"], ["5"]])
        full = outline(h.bridge, URL_)
        self.assertEqual(full["title"], "达治-GOFCN-需求表")
        self.assertEqual(full["blocks"][:2], [{"index": 0, "type": "heading1", "text": "1023——AI片头衔接需求"},
                                              {"index": 1, "type": "table", "text": "命名 | 方向", "rows": 3, "cols": 2, "cells": [["命名", "方向"], ["GOFCN1_a", ""], ["GOFCN2_b", None]]}])
        self.assertEqual([len(full["blocks"][2]["cells"]), full["blocks"][2]["more_rows"]], [4, 2])
        one = outline(h.bridge, URL_, block=2)
        self.assertEqual([len(one["blocks"]), len(one["blocks"][0]["cells"])], [1, 6])

    def test_insert(self) -> None:
        """New blocks go in where the outline showed, are read back, and an edit run twice writes once."""
        h = Harness()
        check = edit_doc(h.bridge, URL_, ENTRY, True)
        self.assertEqual([check["status"], check["index"], check["preview"]], ["edit_ready", 0, ["heading1: 1027-AI片头需求", "text: 需求数量：2条", "table: 命名 | 方向"]])
        self.assertEqual(len(h.writes()), 0)
        done = edit_doc(h.bridge, URL_, ENTRY)
        self.assertEqual([done["status"], done["index"]], ["doc_updated", 0])
        # The picture of the edit opens the document on the first new block.
        self.assertEqual(done["focus"]["block"], h.doc.page["children"][0])
        sent = h.writes()[0]["payload"]
        self.assertEqual(sent["parent"], "PageBlock0001")
        self.assertEqual(len([d for d in sent["descendants"] if d["block_type"] == 32]), 6)
        self.assertRegex(sent["client_token"], r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-a[0-9a-f]{3}-[0-9a-f]{12}$")
        after = outline(h.bridge, URL_, full=True)
        self.assertEqual([b["text"] for b in after["blocks"]], ["1027-AI片头需求", "需求数量：2条", "命名 | 方向", "1023——AI片头衔接需求", "命名 | 方向"])
        self.assertEqual(after["blocks"][2]["cells"], [["命名", "方向"], ["GOFCN67867_巨手", "巨手"], ["GOFCN67868_巨手", ""]])
        # The anchor has moved down by the three new blocks: it is found by its text, and the blocks are there already.
        again = edit_doc(h.bridge, URL_, ENTRY)
        self.assertEqual([again["status"], len(h.writes()), again["focus"]["block"]], ["already_present", 1, h.doc.page["children"][0]])
        # At the end, and after a block.
        self.assertEqual(edit_doc(h.bridge, URL_, {"insert": {"at": "end", "blocks": [{"divider": True}, {"bullet": [{"text": "链接", "link": "https://a.b/c?d=1"}]}]}})["status"], "doc_updated")
        link = next(b for b in h.doc.blocks.values() if b["block_type"] == 12)["bullet"]["elements"][0]["text_run"]["text_element_style"]["link"]["url"]
        self.assertEqual(link, js.encode_uri_component("https://a.b/c?d=1"))
        self.assertEqual(edit_doc(h.bridge, URL_, {"insert": {"at": {"after": 0, "expect": "1027-AI片头需求"}, "blocks": [{"heading2": "补充"}]}})["index"], 1)

    def test_nothing_written(self) -> None:
        """An edit whose place is gone, or that cannot be checked, writes nothing."""
        h = Harness()
        self.assertRaisesRegex(Blocked, "that text is nowhere else; read the outline again", edit_doc, h.bridge, URL_, {"insert": {"at": {"before": 0, "expect": "别的标题"}, "blocks": [{"text": "x"}]}})
        self.assertRaisesRegex(Blocked, "each with the same number of text cells", edit_doc, h.bridge, URL_, {"insert": {"at": {"before": 1, "expect": "命名 | 方向"}, "blocks": [{"table": [["a", "b"], ["c"]]}]}})
        self.assertRaisesRegex(Blocked, "the kind \"image\" is not supported", edit_doc, h.bridge, URL_, {"insert": {"at": "start", "blocks": [{"image": "x"}]}})
        self.assertRaisesRegex(Blocked, "must have exactly one kind", edit_doc, h.bridge, URL_, {"insert": {"at": "start", "blocks": [{"text": "x", "bold": True}]}})
        self.assertRaisesRegex(Blocked, "insert.*fill", edit_doc, h.bridge, URL_, {"replace": {}})
        self.assertEqual(len(h.writes()), 0)
        # A login from before document editing: the user authorizes once more; the check itself still runs.
        old = Harness(scopes=[])
        self.assertEqual(edit_doc(old.bridge, URL_, ENTRY, True)["status"], "edit_ready")
        self.assertRaisesRegex(Blocked, "lacks the permission to edit documents", edit_doc, old.bridge, URL_, ENTRY)
        self.assertEqual(len(old.writes()), 0)

    def test_refusals(self) -> None:
        """Feishu refusing the edit is said plainly, and a write whose answer is lost is read back by running it again."""
        denied = Harness(insert=lambda p: fail("api", "飞书接口返回错误（HTTP 403）：forbidden", 1770032))
        self.assertRaisesRegex(Blocked, "no edit access to this document", edit_doc, denied.bridge, URL_, ENTRY)
        lost = [True]
        holder: dict[str, Harness] = {}

        def flaky_insert(p: dict) -> dict:
            answer = holder["flaky"].doc.insert(p)
            if lost[0]:
                lost[0] = False
                return fail("unknown_result", "timeout")
            return ok(answer)

        holder["flaky"] = Harness(insert=flaky_insert)
        self.assertRaisesRegex(Blocked, "run the same doc-edit again", edit_doc, holder["flaky"].bridge, URL_, ENTRY)
        self.assertEqual(edit_doc(holder["flaky"].bridge, URL_, ENTRY)["status"], "already_present")
        # A read-back that does not show what was planned is reported, not retried.

        def odd_insert(p: dict) -> dict:
            changed = [{**d, "heading1": {"elements": [{"text_run": {"content": "别的"}}]}} if d["block_type"] == 3 else d for d in p["descendants"]]
            return ok(holder["odd"].doc.insert({**p, "descendants": changed}))

        holder["odd"] = Harness(insert=odd_insert)
        self.assertRaisesRegex(Blocked, "the read-back differs from the plan", edit_doc, holder["odd"].bridge, URL_, ENTRY)

    def test_fill_table(self) -> None:
        """Table cells are filled only where empty, never under a merge, and filling again changes nothing."""
        h = Harness()

        def fill(cells: list[dict]) -> dict:
            return {"fill": {"table": 1, "expect": "命名 | 方向", "cells": cells}}

        self.assertRaisesRegex(Blocked, "holds \"GOFCN2_b\" already and is not overwritten", edit_doc, h.bridge, URL_, fill([{"row": 2, "col": 0, "text": "x"}]))
        self.assertRaisesRegex(Blocked, "is merged under another cell", edit_doc, h.bridge, URL_, fill([{"row": 2, "col": 1, "text": "x"}]))
        self.assertRaisesRegex(Blocked, "is outside the 3x2 table", edit_doc, h.bridge, URL_, fill([{"row": 3, "col": 0, "text": "x"}]))
        self.assertEqual(len(h.writes()), 0)
        done = edit_doc(h.bridge, URL_, fill([{"row": 1, "col": 1, "text": "巨手"}, {"row": 0, "col": 0, "text": "命名"}]))
        self.assertEqual([done["status"], done["to_write"], done["already"]], ["doc_updated", 1, 1])
        self.assertEqual(outline(h.bridge, URL_)["blocks"][1]["cells"][1][1], "巨手")
        self.assertEqual(edit_doc(h.bridge, URL_, fill([{"row": 1, "col": 1, "text": "巨手"}]))["status"], "already_present")
        self.assertEqual(len(h.writes()), 1)

    def test_fill_sheet(self) -> None:
        """A sheet gets text in empty cells only, written so that Feishu refuses an occupied one; the progress sheet is not edited here."""
        h = Harness()
        self.assertRaisesRegex(Blocked, "cell A2 holds \"GOFCN9_x\" already and is not overwritten", edit_doc, h.bridge, SHEET_URL, {"fill": {"cells": [{"cell": "A2", "text": "GOFCN1_y"}]}})
        self.assertRaisesRegex(Blocked, "cannot start with =", edit_doc, h.bridge, SHEET_URL, {"fill": {"cells": [{"cell": "B2", "text": "=SUM(1)"}]}})
        done = edit_doc(h.bridge, SHEET_URL, {"fill": {"cells": [{"cell": "b3", "text": "GOFCN67867_巨手"}, {"cell": "A2", "text": "GOFCN9_x"}]}})
        self.assertEqual([done["status"], done["sheet"], done["to_write"], h.sheet["B3"]], ["doc_updated", "1027", 1, "GOFCN67867_巨手"])
        self.assertEqual(done["focus"], {"sheet_id": "t1", "range": "A2:B3"})
        self.assertTrue(all(w["allow_overwrite"] is False for w in h.writes()[0]["payload"]["writes"]))
        self.assertRaisesRegex(Blocked, "the progress sheet is written only by gof feishu append", edit_doc, h.bridge, f"https://centurygames.feishu.cn/wiki/{NODE}",
                               {"fill": {"sheet": "x", "cells": [{"cell": "A1", "text": "x"}]}})
        self.assertRaisesRegex(Blocked, "this is a spreadsheet", edit_doc, h.bridge, SHEET_URL, {"insert": {"at": "start", "blocks": [{"text": "x"}]}})

    def test_command_line(self) -> None:
        """The command line reads an outline and runs an edit file."""
        h = Harness()
        file = write_file(os.path.join(temp_dir("gof-doc-"), "edit.json"), json.dumps(ENTRY, ensure_ascii=False))
        checked = main(["feishu", "doc-edit", "--url", URL_, "--edit", file, "--check"], h.bridge)
        self.assertEqual([checked["code"], checked["result"]["status"]], [0, "edit_ready"])
        shown = main(["feishu", "document", "--url", URL_, "--outline"], h.bridge)
        self.assertEqual([shown["code"], len(shown["result"]["blocks"])], [0, 2])
        self.assertEqual(main(["feishu", "document", "--url", URL_, "--block", "x"], h.bridge)["code"], 2)

    def test_picture_checks(self) -> None:
        """The picture of an edit takes only a company document and the focus doc-edit gave, and never the progress sheet."""
        class NoBrowser:
            def session(self) -> dict:
                raise RuntimeError("no browser in this test")

        online = NoBrowser()
        self.assertRaisesRegex(Blocked, "not a company Feishu document link", doc_picture, online, "https://example.com/docx/abc", {}, "x.png")
        self.assertRaisesRegex(Blocked, "gof feishu screenshot --batch", doc_picture, online, f"https://centurygames.feishu.cn/wiki/{NODE}", {}, "x.png")
        self.assertRaisesRegex(Blocked, "focus.block", doc_picture, online, URL_, {"block": "a/b"}, "x.png")
        self.assertRaisesRegex(Blocked, "like A9 or A9:B12", doc_picture, online, SHEET_URL, {"range": "A9:"}, "x.png")
        self.assertRaisesRegex(RuntimeError, "no browser in this test", doc_picture, online, URL_, {"block": "Real000000001"}, "x.png")


if __name__ == "__main__":
    unittest.main()
