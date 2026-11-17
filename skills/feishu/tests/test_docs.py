from __future__ import annotations

import os
import unittest

from tests.helpers import Clock, now_ms, temp_dir
from tests.helpers.mock_feishu import MockFeishu

from feishu_sdk.auth import FeishuAuth
from feishu_sdk.cli import Context, main
from feishu_sdk.config import REQUIRED_SCOPES, AppCredentials
from feishu_sdk.docs import parse_link
from feishu_sdk.errors import Blocked
from feishu_sdk.markdown import docx_to_markdown
from feishu_sdk.paths import resolve_data_paths


def run(content: str, style: dict | None = None) -> dict:
    return {"text_run": {"content": content, "text_element_style": style or {}}}


def text(ident: str, kind: int, key: str, elements: list, **extra) -> dict:
    return {"block_id": ident, "block_type": kind, key: {"elements": elements, "style": {}}, "children": [], **extra}


# A script document as a supplier writes one: a heading, a paragraph with marks and a link, a list, a two-column table, a picture.
BLOCKS = [
    {"block_id": "page", "block_type": 1, "page": {"elements": [run("脚本 V2")]}, "children": ["h", "p", "l1", "l2", "tbl", "img", "hr", "code"]},
    text("h", 3, "heading1", [run("开场")]),
    text("p", 2, "text", [run("全球 "), run("3 亿", {"bold": True}), run("玩家，"), run("官网", {"link": {"url": "https%3A%2F%2Fexample.com%2Fa"}})]),
    text("l1", 12, "bullet", [run("镜头一")], children=["l1a"]),
    text("l1a", 12, "bullet", [run("特写")]),
    text("l2", 13, "ordered", [run("镜头二")]),
    {"block_id": "tbl", "block_type": 31, "table": {"cells": ["c1", "c2", "c3", "c4"], "property": {"row_size": 2, "column_size": 2}}, "children": ["c1", "c2", "c3", "c4"]},
    {"block_id": "c1", "block_type": 32, "table_cell": {}, "children": ["t1"]}, text("t1", 2, "text", [run("画面")]),
    {"block_id": "c2", "block_type": 32, "table_cell": {}, "children": ["t2"]}, text("t2", 2, "text", [run("口播")]),
    {"block_id": "c3", "block_type": 32, "table_cell": {}, "children": ["t3"]}, text("t3", 2, "text", [run("冰原|远景")]),
    {"block_id": "c4", "block_type": 32, "table_cell": {}, "children": ["t4", "t5"]}, text("t4", 2, "text", [run("不用氪金")]), text("t5", 2, "text", [run("也能玩")]),
    {"block_id": "img", "block_type": 27, "image": {"token": "imgtoken"}, "children": []},
    {"block_id": "hr", "block_type": 22, "divider": {}, "children": []},
    text("code", 14, "code", [run("a = 1\nb = 2")]),
]


def read(path: str) -> str:
    with open(path, encoding="utf-8", newline="") as handle:
        return handle.read()


class MarkdownTest(unittest.TestCase):
    def test_a_docx_becomes_markdown_that_keeps_its_text_and_structure(self) -> None:
        self.assertEqual(docx_to_markdown(BLOCKS), "\n".join([
            "# 脚本 V2", "", "# 开场", "", "全球 **3 亿**玩家，[官网](https://example.com/a)", "", "- 镜头一", "  - 特写", "1. 镜头二", "",
            "| 画面 | 口播 |", "| --- | --- |", "| 冰原\\|远景 | 不用氪金<br>也能玩 |", "", "[image]", "", "---", "", "```", "a = 1", "b = 2", "```", "",
        ]))

    def test_a_list_in_a_callout_ends_before_what_follows_and_marks_go_round_the_words(self) -> None:
        # Blank lines in code stay, and marks go round the words, not their spaces.
        blocks = [
            {"block_id": "page", "block_type": 1, "page": {"elements": []}, "children": ["callout", "code", "p"]},
            {"block_id": "callout", "block_type": 19, "callout": {}, "children": ["li", "after"]},
            text("li", 12, "bullet", [run("一条")]),
            text("after", 2, "text", [run("之后的段落")]),
            text("code", 14, "code", [run("a = 1\n\n\nb = 2")]),
            text("p", 2, "text", [run("全球"), run(" 3 亿 ", {"bold": True}), run("玩家")]),
        ]
        self.assertEqual(docx_to_markdown(blocks), "\n".join(["> - 一条", ">", "> 之后的段落", ">", "", "```", "a = 1", "", "", "b = 2", "```", "", "全球 **3 亿** 玩家", ""]))

    def test_merged_cells_todos_and_placeholders(self) -> None:
        blocks = [
            {"block_id": "page", "block_type": 1, "page": {"elements": [run("T")]}, "children": ["todo", "tbl", "sheet", "file", "eq"]},
            {"block_id": "todo", "block_type": 17, "todo": {"elements": [run("完成", {"italic": True, "strikethrough": True})], "style": {"done": True}}, "children": []},
            {"block_id": "tbl", "block_type": 31, "table": {"cells": ["a", "b", "c", "d"], "property": {"row_size": 2, "column_size": 2,
             "merge_info": [{"row_span": 1, "col_span": 2}, {}, {}, {}]}}, "children": []},
            {"block_id": "a", "block_type": 32, "children": ["ta"]}, text("ta", 2, "text", [run("合并")]),
            {"block_id": "b", "block_type": 32, "children": ["tb"]}, text("tb", 2, "text", [run("被盖住")]),
            {"block_id": "c", "block_type": 32, "children": []}, {"block_id": "d", "block_type": 32, "children": []},
            {"block_id": "sheet", "block_type": 30, "children": []},
            {"block_id": "file", "block_type": 23, "file": {"name": "a.pdf"}, "children": []},
            text("eq", 2, "text", [{"equation": {"content": " x^2 \n"}}, {"mention_user": {}}, {"mention_doc": {"title": "D", "url": "https://x/%E4%B8%AD"}}]),
        ]
        self.assertEqual(docx_to_markdown(blocks), "\n".join([
            "# T", "", "- [x] ~~**完成**~~".replace("**", "*"), "", "| 合并 |  |", "| --- | --- |", "|  |  |", "", "[embedded sheet]", "", "[file: a.pdf]", "",
            "$x^2$@user[D](https://x/中)", "",
        ]))

    def test_only_feishu_document_links_are_read_and_an_old_style_doc_is_named_as_such(self) -> None:
        self.assertEqual(parse_link("https://centurygames.feishu.cn/wiki/CSy2dZ3XvoLDmBxolPXcNbtunuc"), {"kind": "wiki", "token": "CSy2dZ3XvoLDmBxolPXcNbtunuc", "sheet": None})
        self.assertEqual(parse_link("https://x.feishu.cn/sheets/shtABCDEFGH?sheet=abc"), {"kind": "sheets", "token": "shtABCDEFGH", "sheet": "abc"})
        for bad in ["http://x.feishu.cn/docx/ABCDEFGH", "https://feishu.cn.evil.com/docx/ABCDEFGH", "https://x.feishu.cn/file/ABCDEFGH", "nope",
                    "https://u:p@x.feishu.cn/docx/ABCDEFGH"]:
            with self.assertRaises(Blocked, msg=bad):
                parse_link(bad)
        with self.assertRaisesRegex(Blocked, "legacy document"):
            parse_link("https://x.feishu.cn/docs/ABCDEFGH")
        with self.assertRaisesRegex(Blocked, "Not a valid link"):
            parse_link("nope")


class DocFetchTest(unittest.TestCase):
    def setUp(self) -> None:
        self.mock = MockFeishu().start()
        self.addCleanup(self.mock.stop)

    def logged_in(self) -> Context:
        folder = temp_dir("fs-doc-")
        clock = Clock(now_ms())
        self.mock.scope = " ".join([*REQUIRED_SCOPES, "offline_access"])
        auth = FeishuAuth(paths=resolve_data_paths({"AGENT_USER_DIR": folder}), app=AppCredentials(self.mock.app_id, self.mock.app_secret),
                          endpoints=self.mock.endpoints, now=clock.now, sleep=clock.sleep)
        auth.start_login()
        self.mock.device_script = ["issue"]
        auth.poll_login(10_000)
        return Context(env={"AGENT_USER_DIR": folder, "FEISHU_APP_ID": self.mock.app_id, "FEISHU_APP_SECRET": self.mock.app_secret}, endpoints=self.mock.endpoints)

    def test_a_wiki_node_holding_a_docx_is_saved_as_markdown_in_a_new_file(self) -> None:
        def more(pathname: str, query: dict):
            if pathname == "/open-apis/wiki/v2/spaces/get_node":
                return {"node": {"node_token": "wikiNODE1", "obj_type": "docx", "obj_token": "doxABCDEFGH", "title": "脚本 V2"}}
            if pathname == "/open-apis/docx/v1/documents/doxABCDEFGH":
                return {"document": {"revision_id": 3, "title": "脚本 V2"}}
            if pathname == "/open-apis/docx/v1/documents/doxABCDEFGH/blocks":
                return {"items": BLOCKS, "has_more": False}
            return None

        self.mock.more = more
        ctx = self.logged_in()
        output = os.path.join(temp_dir("fs-out-"), "脚本.md")
        result, code = main(["doc", "fetch", "--url", "https://centurygames.feishu.cn/wiki/wikiNODE1", "--output", output], ctx)
        self.assertEqual(code, 0)
        self.assertEqual(list(result), ["status", "file", "kind", "title", "chars"])
        self.assertEqual((result["status"], result["kind"], result["title"]), ("saved", "docx", "脚本 V2"))
        self.assertRegex(read(output), r"\| 冰原\\\|远景 \| 不用氪金<br>也能玩 \|")
        self.assertNotIn("\r\n", read(output), "lines end as they did in the plugin")
        self.assertEqual(result["chars"], len(read(output)))
        again, code = main(["doc", "fetch", "--url", "https://centurygames.feishu.cn/wiki/wikiNODE1", "--output", output], ctx)
        self.assertEqual((code, again["status"]), (2, "blocked"), "an existing file is not overwritten")

    def test_a_sheet_is_saved_tab_by_tab_and_without_a_login_it_says_what_to_do(self) -> None:
        tabs = [
            {"sheet_id": "s1", "title": "口播", "index": 0, "resource_type": "sheet", "grid_properties": {"row_count": 3, "column_count": 2}},
            {"sheet_id": "s2", "title": "分镜", "index": 1, "resource_type": "sheet", "grid_properties": {"row_count": 2, "column_count": 1}},
            {"sheet_id": "s3", "title": "隐藏", "index": 2, "resource_type": "sheet", "hidden": True, "grid_properties": {"row_count": 1, "column_count": 1}},
        ]
        values = {"s1": [["时间", "口播"], [None, None], ["00:01", [{"type": "text", "text": "零氪也能玩"}]]], "s2": [["冰原"]]}

        def more(pathname: str, query: dict):
            if pathname == "/open-apis/sheets/v3/spreadsheets/shtABCDEFGH":
                return {"spreadsheet": {"title": "口播表"}}
            if pathname == "/open-apis/sheets/v3/spreadsheets/shtABCDEFGH/sheets/query":
                return {"sheets": tabs}
            if pathname.endswith("/values_batch_get"):
                return {"valueRanges": [{"values": values[query["ranges"].split("!")[0]]}]}
            return None

        self.mock.more = more
        ctx = self.logged_in()
        everything = os.path.join(temp_dir("fs-out-"), "all.md")
        result, code = main(["doc", "fetch", "--url", "https://centurygames.feishu.cn/sheets/shtABCDEFGH", "--output", everything], ctx)
        self.assertEqual((code, result["kind"], result["tabs"]), (0, "sheet", ["口播", "分镜"]))
        self.assertEqual(read(everything), "\n".join([
            "# 口播表", "", "## 口播", "", "| Row | A | B |", "| --- | --- | --- |", "| 1 | 时间 | 口播 |", "| 3 | 00:01 | 零氪也能玩 |", "",
            "## 分镜", "", "| Row | A |", "| --- | --- |", "| 1 | 冰原 |", "",
        ]))
        batch = self.mock.requests("/open-apis/sheets/v2/spreadsheets/shtABCDEFGH/values_batch_get")
        self.assertTrue(all("valueRenderOption=FormattedValue" in r.url for r in batch), "numbers as the cells show them")
        one = os.path.join(temp_dir("fs-out-"), "one.md")
        main(["doc", "fetch", "--url", "https://centurygames.feishu.cn/sheets/shtABCDEFGH?sheet=s2", "--output", one], ctx)
        self.assertRegex(read(one), r"## 分镜\n\n\| Row \| A \|", "the linked tab is there")
        self.assertNotRegex(read(one), r"口播 \|")

        nobody, code = main(["doc", "fetch", "--url", "https://centurygames.feishu.cn/sheets/shtABCDEFGH", "--output", os.path.join(temp_dir("fs-out-"), "x.md")],
                            Context(env={"AGENT_USER_DIR": temp_dir("fs-none-"), "FEISHU_APP_ID": self.mock.app_id, "FEISHU_APP_SECRET": self.mock.app_secret},
                                    endpoints=self.mock.endpoints))
        self.assertEqual((code, nobody["status"]), (2, "login_required"))
        self.assertRegex(nobody["todo"][0], "login qr")

    def test_a_document_the_account_may_not_read_is_said_to_be_so_and_a_missing_tab_names_the_tabs(self) -> None:
        def more(pathname: str, query: dict):
            if pathname == "/open-apis/sheets/v3/spreadsheets/shtABCDEFGH":
                return {"spreadsheet": {"title": "表"}}
            if pathname == "/open-apis/sheets/v3/spreadsheets/shtABCDEFGH/sheets/query":
                return {"sheets": [{"sheet_id": "s1", "title": "口播", "index": 0, "resource_type": "sheet"}]}
            return None

        self.mock.more = more
        ctx = self.logged_in()
        tab, _ = main(["doc", "fetch", "--url", "https://centurygames.feishu.cn/sheets/shtABCDEFGH", "--sheet", "分镜", "--output",
                       os.path.join(temp_dir("fs-out-"), "x.md")], ctx)
        self.assertRegex(tab["reason"], "has no sheet '分镜'; it has: 口播")
        # The real answer for a docx the account may not read (2026-10-29): HTTP 403, code 1770032, "forBidden".
        forbidden, code = main(["doc", "fetch", "--url", "https://centurygames.feishu.cn/docx/doxNOACCESS1", "--output", os.path.join(temp_dir("fs-out-"), "y.md")], ctx)
        self.assertEqual((code, forbidden["status"]), (2, "blocked"))
        self.assertRegex(forbidden["reason"], r"may not read the document \(code 1770032\)")


if __name__ == "__main__":
    unittest.main()
