"""The material upload: scanning, the request, the plan from the progress sheet, the run against a fake CLS."""

from __future__ import annotations

import json
import os
import re
import shutil
import unittest
from typing import Any, Optional

from tests.fakes import ACCOUNT, DIRS, GAME, PEOPLE, TYPES, FakeCreative, ScriptedOnline
from tests.helpers import fixture, make_xlsx, make_zip, temp_dir, write_file

from gof import js
from gof.creative import (build_upload_plan, creative_name, find_directory, formula_text, hash_file, load_upload_plan, lookup_rows, match_people,
                          material_number, material_refusal, name_parts, normalize_upload_request, parse_numbers, pending_body, progress_rows_from,
                          scan_materials, search_progress_rows, upload_events, upload_plan_summary, variant_bases)
from gof.creative_adapter import CreativeAdapter
from gof.creative_online import UploadDeps, find_rows, list_files, plan_upload, run_upload, upload_options, upload_status
from gof.cls_adapter import ClsError
from gof.snapshot import Snapshot
from gof.unzip import unpack_zip
from gof.util import Blocked, NODE
from gof.web import FetchError, Response, fetch as real_fetch

OPTIONS = {"account": ACCOUNT, "game": {"id": GAME["id"], "code": GAME["code"], "name": GAME["name"]}, "can_upload": True, "directories": DIRS,
           "creators": PEOPLE["creative_users"], "producers": PEOPLE["producers"], "types": TYPES}
LOCAL = re.compile(r"127\.0\.0\.1")


def materials(files: dict[str, str]) -> str:
    directory = temp_dir("materials-")
    for name, content in files.items():
        write_file(os.path.join(directory, name), content)
    return directory


def hashed(inputs: list[str]) -> tuple[list[dict], list[dict]]:
    found, skipped = scan_materials(inputs)
    return [{**m, **hash_file(m["file"])} for m in found], skipped


def online(fake: FakeCreative, account: Optional[dict] = None) -> ScriptedOnline:
    return ScriptedOnline(session={"authorization": "Bearer good", "account": account or ACCOUNT, "origin": fake.origin})


def deps_for(**adapter: Any) -> UploadDeps:
    return UploadDeps(adapter={"upload_host": LOCAL, "allow_http_upload": True, **adapter}, batch_root=temp_dir("upload-batches-"), tmp_dir=temp_dir("upload-tmp-"),
                      sleep=lambda ms: None, reconcile_tries=1)


def approved_plan(fake: FakeCreative, deps: UploadDeps, request: dict, bridge: Any = None) -> str:
    """A plan the user approved. These tests are about the run, so the files keep their names unless the request asks for the progress sheet."""
    directory = temp_dir("upload-plan-")
    write_file(os.path.join(directory, "request.json"), json.dumps({"progress": False, **request}, ensure_ascii=False))
    summary = plan_upload(online(fake), bridge, os.path.join(directory, "request.json"), os.path.join(directory, "plan.json"), deps)
    assert summary["status"] == "upload_plan"
    return summary["plan"]


# The progress sheet of the 终稿库 example (玩法AI迭代素材.zip): the two originals the new pieces build on, the four rows the requirement
# appended for them (围炉煮冰 twice: the V90 and its 国风配乐 version), and a row named by SUBSTITUTE as some newer rows are.
HEADER = ["", "创意批次", "素材编号", "提炼词", "片头引用（初版编号）", "创意引用（海外汉化解说/迭代）（初版编号）", "素材名称", "一级标签（方向）",
          "二级标签（卖点）", "创意表达（可多选）", "创意形式（制作形式）", "客户端", "设计师供应商", "创意者脚本审核人"]


def progress_row(row: int, number: int, keyword: str, tags: tuple[str, str, str], people: tuple[str, str], substitute: bool = False) -> list:
    inner = f'CONCATENATE("GOFCN",C{row},"_",H{row},"_",I{row},"_",K{row},"_",D{row},L{row})'
    return [None, None, number, keyword, None, None, {"f": f'SUBSTITUTE({inner},"__","_")' if substitute else inner}, tags[0], tags[1], None, tags[2] or None, None, people[0], people[1]]


def final_cut_sheet() -> str:
    file = os.path.join(temp_dir("progress-"), "progress.xlsx")
    write_file(file, make_xlsx([{"name": "26年1月-GOFCN", "rows": [HEADER,
        progress_row(2, 56890, "火车运输御敌-斜", ("汉化", "竞争对抗", "3D"), ("廿十一", "金莉")),
        progress_row(3, 44065, "转斧杀熊烤肉", ("汉化", "资源城建", "3D"), ("廿十一", "金莉")),
        progress_row(4, 67680, "企鹅保龄球", ("AI生成", "设定3.0", "3D"), ("廿十一", "金莉")),
        progress_row(5, 67681, "冻鱼飞盘", ("AI生成", "设定3.0", "3D"), ("廿十一", "金莉")),
        progress_row(6, 67682, "围炉煮冰", ("AI生成", "设定3.0", "3D"), ("廿十一", "金莉")),
        progress_row(7, 67683, "围炉煮冰", ("AI生成", "设定3.0", "3D"), ("廿十一", "金莉")),
        progress_row(8, 67684, "关键词", ("AI生成", "设定3.0", ""), ("菲比", "钱程"), True)]}]))
    return file


class SheetBridge:
    """Feishu as far as the progress lookup goes: the plugin's sheet node, exported as the given workbook."""

    def __init__(self, xlsx: str, exports: Optional[list[str]] = None) -> None:
        self.xlsx = xlsx
        self.exports = exports if exports is not None else []

    def doctor(self) -> dict:
        return {"status": "ok"}

    def api_read(self, path: str, params: Optional[dict] = None) -> dict:
        return {"node": {"node_token": NODE, "obj_type": "sheet", "obj_token": "shtTEST"}}

    def export_sheet(self, token: str, output: str) -> dict:
        self.exports.append(output)
        shutil.copyfile(self.xlsx, output)
        return {"status": "exported"}


# The four videos of 玩法AI迭代素材.zip, under their names there, and which row each is according to the 终稿库 document.
FINAL_CUT_FILES = {"g44065_gf_v90.mp4": "guofeng", "g44065_v90.mp4": "cartoon", "g56890_a_v15.mp4": "penguin", "g56890_c_v21.mp4": "fish"}
FINAL_CUT_ITEMS = [{"file": "g56890_a_v15.mp4", "number": 67680}, {"file": "g56890_c_v21.mp4", "number": 67681},
                   {"file": "g44065_v90.mp4", "number": 67682}, {"file": "g44065_gf_v90.mp4", "number": "GOFCN67683"}]


def writes(fake: FakeCreative, suffix: str) -> int:
    return len([r for r in fake.requests if r["method"] == "POST" and r["path"].endswith(suffix)])


def plan_of(file: str) -> dict:
    with open(file, encoding="utf-8") as handle:
        return json.load(handle)


class OfflineTests(unittest.TestCase):
    def test_scan(self) -> None:
        """One file is one creative named like the file; folders are read one level deep; what cannot go up is said with the reason."""
        directory = materials({"GOFCN66367_片头_竖版.mp4": "v", "封面.PNG": "p", "notes.txt": "t", "pack.zip": "z", ".hidden.mp4": "h", "empty.mp4": "", "sub/inner.mp4": "i"})
        found, skipped = scan_materials([directory])
        self.assertEqual(sorted([m["name"], m["kind"], m["number"] or 0] for m in found), sorted([["GOFCN66367_片头_竖版", "video", 66367], ["封面", "image", 0]]))
        reasons = {os.path.basename(s["file"]): s["reason"] for s in skipped}
        self.assertRegex(reasons["notes.txt"], "does not take this format")
        self.assertRegex(reasons["pack.zip"], "zip")
        self.assertEqual(reasons[".hidden.mp4"], "hidden file")
        self.assertEqual(reasons["empty.mp4"], "empty file")
        self.assertRegex(reasons["sub"], "folder inside the folder")
        self.assertEqual(creative_name("a\u202ab\u202cc.mp4"), "abc", "the marks the page strips are stripped")
        self.assertIsNone(material_number("GOFCNIM14462_图片"), "another numbering series is no material number")
        self.assertIsNone(material_number("GOFCN20261019002_需求"), "a requirement number is no material number")
        self.assertEqual(material_number("x_GOFCN67471_AI剧情"), 67471)

    def test_request(self) -> None:
        """The request takes only the known fields, each in its form."""
        self.assertRaisesRegex(Blocked, "unknown fields in the upload request: tags", normalize_upload_request, {"files": ["a.mp4"], "tags": []})
        self.assertRaisesRegex(Blocked, "files", normalize_upload_request, {"files": []})
        self.assertRaisesRegex(Blocked, "producers", normalize_upload_request, {"files": ["a.mp4"], "producers": "丁杰"})
        self.assertRaisesRegex(Blocked, "unknown fields: creater", normalize_upload_request, {"files": ["a.mp4"], "items": [{"file": "a.mp4", "creater": "x"}]})
        self.assertEqual(normalize_upload_request({"files": ["a.mp4"], "progress": False}), {"files": ["a.mp4"], "progress": False})
        self.assertEqual(normalize_upload_request({"files": ["a.mp4"]}), {"files": ["a.mp4"], "progress": True}, "the progress sheet is read unless the request says not to")
        self.assertEqual([i["number"] for i in normalize_upload_request({"files": ["a.mp4"], "items": [{"file": "a.mp4", "number": "GOFCN67680"}, {"file": "b.mp4", "number": 67681}]})["items"]], [67680, 67681])
        self.assertRaisesRegex(Blocked, "GOFCN material number", normalize_upload_request, {"files": ["a.mp4"], "items": [{"file": "a.mp4", "number": "GOFCN20261022001"}]})
        for bad in (20261022001, 67680.5, 0, -3, "企鹅保龄球"):
            self.assertRaisesRegex(Blocked, "GOFCN material number", normalize_upload_request, {"files": ["a.mp4"], "items": [{"file": "a.mp4", "number": bad}]})
        self.assertEqual(normalize_upload_request({"files": ["a.mp4"], "items": [{"file": "a.mp4", "number": 1001, "name": "新名称"}]})["items"][0]["name"], "新名称")
        self.assertEqual(normalize_upload_request({"files": ["a.mp4"], "items": [{"file": "a.mp4", "number": 1001, "sheet": "26年3月-GOFCN"}]})["items"][0]["sheet"], "26年3月-GOFCN")
        self.assertEqual(normalize_upload_request({"files": ["a.mp4"], "progress": False, "items": [{"file": "a.mp4", "name": "新名称"}]})["items"][0]["name"], "新名称")
        self.assertRaisesRegex(Blocked, "only work with the progress sheet", normalize_upload_request, {"files": ["a.mp4"], "progress": False, "items": [{"file": "a.mp4", "number": 67680}]})

    def test_numbers(self) -> None:
        """Material numbers on the command line: single, ranges as CLS names them, with or without GOFCN."""
        self.assertEqual(parse_numbers("67680-67683,67690"), [67680, 67681, 67682, 67683, 67690])
        self.assertEqual(parse_numbers("GOFCN67680-GOFCN67682 GOFCN67680"), [67680, 67681, 67682])
        self.assertEqual(parse_numbers("67682、67680"), [67682, 67680])
        self.assertRaisesRegex(Blocked, "invalid number range", parse_numbers, "67683-67680")
        self.assertRaisesRegex(Blocked, "invalid number range", parse_numbers, "1-1000")
        self.assertRaisesRegex(Blocked, "cannot read the number", parse_numbers, "企鹅")

    def test_formulas(self) -> None:
        """Naming formulas read as the sheet shows them: CONCATENATE with empty arguments, SUBSTITUTE around it, a cell of another row."""
        cells = {"C9": "67651", "H9": "AI生成", "I9": "设定3.0", "K9": "", "D9": "关键词", "L9": "", "F8": "GOFCN23972", "G7": None}

        def at(coord: str) -> Optional[str]:
            return cells[coord] if coord in cells else ""

        self.assertEqual(formula_text('=CONCATENATE("GOFCN",C9,"_",H9,"_",I9,"_",K9,"_",D9,L9)', at), "GOFCN67651_AI生成_设定3.0__关键词")
        self.assertEqual(formula_text('=SUBSTITUTE(CONCATENATE("GOFCN",C9,"_",H9,"_",I9,"_",K9,"_",D9,L9),"__","_")', at), "GOFCN67651_AI生成_设定3.0_关键词")
        self.assertEqual(formula_text('=CONCATENATE("GOFCN",$C$9,"_",H9,"_",D9,,L9)', at), "GOFCN67651_AI生成_关键词", "an empty argument is an empty text")
        self.assertEqual(formula_text('=concat("GOFCN",C9,"_迭代_",F8)', at), "GOFCN67651_迭代_GOFCN23972", "another row is just another cell")
        self.assertEqual(formula_text('=CONCATENATE("say ""hi""",1)', at), 'say "hi"1')
        self.assertIsNone(formula_text('=CONCATENATE("GOFCN",G7)', at), "a cell that is itself a formula is not followed")
        self.assertIsNone(formula_text('=CONCATENATE("GOFCN",C9,"_",A1:0)', at), "a range is no text")
        self.assertIsNone(formula_text('=TRIM(CONCATENATE("GOFCN",C9))', at), "other functions are not guessed")
        self.assertIsNone(formula_text('=CONCATENATE("GOFCN",C9', at))
        self.assertIsNone(formula_text("GOFCN67651", at))

    def test_directory(self) -> None:
        """An upload directory is found by its name, a path ending or its ID, and only when that is unique."""
        self.assertEqual(find_directory(DIRS, "双端共用-视频"), {"id": 1585089, "path": "无尽冬日 - NEW / 常规素材 / 双端共用-视频"})
        self.assertEqual(find_directory(DIRS, "常规素材/图片")["id"], 1573621)
        self.assertEqual(find_directory(DIRS, "1585181")["id"], 1585181)
        self.assertRaisesRegex(Blocked, "is not unique", find_directory, DIRS, "图片")
        self.assertRaisesRegex(Blocked, "has no upload directory", find_directory, DIRS, "不存在的目录")

    def test_plan_basics(self) -> None:
        """The plan: the default directory, 素材类型 by file kind, short names matched to CLS people, a file setting over the request."""
        directory = materials({"a.mp4": "video-a", "b.jpg": "image-b", "c.mp3": "audio-c"})
        found, skipped = hashed([directory])
        request = normalize_upload_request({"files": [directory], "progress": False, "creator": "张天伟", "producers": ["丁杰"],
                                            "items": [{"file": "b.jpg", "creator": "雷佳威", "producers": ["杨燃", "谢振文"], "directory": "常规素材/图片"}]})
        plan = build_upload_plan({"request": request, "options": OPTIONS, "materials": found, "skipped": skipped, "existing": set()})
        by = {i["name"]: i for i in plan["items"]}
        self.assertEqual([by["a"]["type"]["name"], by["b"]["type"]["name"], by["c"]["type"]["name"]], ["视频", "图片", "其他"])
        self.assertEqual([by["a"]["directory"]["id"], by["b"]["directory"]["id"]], [1585089, 1573621])
        self.assertEqual(by["a"]["producers"], [{"id": 663, "name": "北京柒曦文化传媒有限公司（丁杰）"}])
        self.assertEqual([p["id"] for p in by["b"]["producers"]], [1163, 1405])
        self.assertEqual(by["b"]["creator"]["name"], "雷佳威")
        self.assertEqual([by["a"]["source"]["creator"], by["b"]["source"]["creator"]], ["request", "item"])
        self.assertEqual(upload_plan_summary(plan, "plan.json")["types"], {"视频": 1, "图片": 1, "其他": 1})

    def test_match_people(self) -> None:
        """The people of files not registered yet are matched to CLS before any plan: each name to its CLS name, or what is wrong with it."""
        self.assertEqual(match_people(OPTIONS, {"creator": "金莉", "producers": ["丁杰", "廿十一"]}), {
            "creator": {"wanted": "金莉", "id": 2001, "name": "金莉"},
            "producers": [{"wanted": "丁杰", "id": 663, "name": "北京柒曦文化传媒有限公司（丁杰）"}, {"wanted": "廿十一", "problem": "CLS 制作人 \"廿十一\": nothing matches; settle it before the plan"}]})
        self.assertEqual(match_people(OPTIONS, {"creator": "李晨阳"}), {"creator": {"wanted": "李晨阳", "problem": "CLS 创意人 \"李晨阳\": 2 matches; settle it before the plan"}})
        self.assertEqual(match_people(OPTIONS, {"creator": "492", "producers": ["长沙廿拾一文化传媒有限公司"]}),
                         {"creator": {"wanted": "492", "id": 492, "name": "李晨阳"}, "producers": [{"wanted": "长沙廿拾一文化传媒有限公司", "id": 2002, "name": "长沙廿拾一文化传媒有限公司"}]})
        self.assertEqual(match_people(OPTIONS, {}), {})
        # gof creative options --creator 金莉 --producers 廿十一: the same, from what CLS offers this account.
        fake = FakeCreative()
        try:
            offered = upload_options(online(fake), creator="金莉", producers=["廿十一"], deps=deps_for())
            self.assertEqual(offered["people"], {"creator": {"wanted": "金莉", "id": 2001, "name": "金莉"},
                                                 "producers": [{"wanted": "廿十一", "problem": "CLS 制作人 \"廿十一\": nothing matches; settle it before the plan"}]})
            self.assertNotIn("people", upload_options(online(fake), deps=deps_for()))
        finally:
            fake.close()

    def test_everything_at_once(self) -> None:
        """The plan says everything that is not settled, at once: no 创意人, ambiguous people, a taken or doubled name."""
        directory = materials({"a.mp4": "1", "x.mp4": "2", "x.png": "3"})
        found, skipped = hashed([directory])

        def build(request: dict, existing: Optional[set] = None) -> dict:
            return build_upload_plan({"request": normalize_upload_request({"files": [directory], "progress": False, **request}), "options": OPTIONS,
                                      "materials": found, "skipped": skipped, "existing": existing or set()})

        with self.assertRaises(Blocked) as caught:
            build({"producers": ["丁杰"]})
        self.assertTrue("创意人 not decided" in caught.exception.message and "the same creative name \"x\"" in caught.exception.message)
        self.assertRaisesRegex(Blocked, "李晨阳.*2 matches", build, {"creator": "李晨阳", "producers": ["丁杰"], "items": [{"file": "x.png", "skip": True}]})
        by_id = build({"creator": "492", "producers": ["丁杰"], "items": [{"file": "x.png", "skip": True}]})
        self.assertTrue(all(i["creator"]["id"] == 492 for i in by_id["items"]), "a CLS user ID picks one of two people with the same name")
        self.assertTrue(any(s["reason"] == "skipped as asked" for s in by_id["skipped"]))
        self.assertRaisesRegex(Blocked, "CLS has a creative of this name already.*: a$", build, {"creator": "张天伟", "producers": ["丁杰"], "items": [{"file": "x.png", "skip": True}]}, {"a"})
        renamed = build({"creator": "张天伟", "producers": ["丁杰"], "items": [{"file": "x.png", "name": "x_封面"}]})
        self.assertEqual(sorted(i["name"] for i in renamed["items"]), ["a", "x", "x_封面"])
        cover = next(i for i in renamed["items"] if i["name"] == "x_封面")
        self.assertEqual([cover["renamed_from"], cover["upload_name"]], ["x", "x_封面.png"], "a renamed file goes up under the new name too")
        self.assertNotIn("upload_name", next(i for i in renamed["items"] if i["name"] == "a"))

    def test_progress_rows(self) -> None:
        """The progress sheet gives the name (素材名称), 创意人 and 制作人 by material number; a missing or doubled number is asked."""
        snapshot = Snapshot(fixture("base.xlsx"))
        rows = progress_rows_from(snapshot, [1001, 1002, 1004, 9999])
        self.assertEqual(len(rows[1001]), 2, "a number on two rows is two rows")
        self.assertEqual(len(rows[1004]), 1)
        row = rows[1004][0]
        self.assertEqual({k: row[k] for k in ("sheet", "row", "number", "supplier", "reviewer")}, {"sheet": "GOFCN创意进度 26年3月", "row": 2, "number": 1004, "supplier": "供应商甲", "reviewer": "张三"})
        self.assertRegex(row["name"], "^GOFCN1004_", "the name comes out of the naming formula")
        self.assertEqual(rows[1002][0]["name"], "GOFCN1002_玩法_卖点_视频_爆点B")
        self.assertNotIn(9999, rows)

        people = {**OPTIONS, "creators": [*OPTIONS["creators"], {"id": 9001, "name": "张三"}], "producers": [*OPTIONS["producers"], {"id": 9002, "name": "某某公司（供应商甲）"}]}
        directory = materials({"GOFCN1004_成片.mp4": "a"})
        found, skipped = hashed([directory])

        def request(items: Optional[list] = None) -> dict:
            return normalize_upload_request({"files": [directory], "progress": True, **({"items": items} if items else {})})

        self.assertRaisesRegex(Blocked, r"the row of GOFCN1004 \(the number in the file name\) has the 素材名称 .*not the file's name.*give it in items\[\]\.number", build_upload_plan,
                               {"request": request(), "options": people, "materials": found, "skipped": skipped, "existing": set(), "progress": rows})
        plan = build_upload_plan({"request": request([{"file": "GOFCN1004_成片.mp4", "number": 1004}]), "options": people, "materials": found, "skipped": skipped,
                                  "existing": set(), "progress": rows})
        item = plan["items"][0]
        self.assertEqual(item["creator"]["name"], "张三")
        self.assertEqual(item["producers"], [{"id": 9002, "name": "某某公司（供应商甲）"}])
        self.assertEqual([item["source"]["progress_row"]["row"], item["source"]["progress_row"]["by"]], [2, "item"])
        self.assertEqual(item["name"], rows[1004][0]["name"], "a file named otherwise goes up as its row is named")
        self.assertEqual([item["renamed_from"], item["upload_name"]], ["GOFCN1004_成片", f"{rows[1004][0]['name']}.mp4"])
        for file, items, pattern in (("GOFCN1001_x.mp4", [{"file": "GOFCN1001_x.mp4", "number": 1001}], "is on 2 progress-sheet rows that differ.*a number repeated within one tab"),
                                     ("GOFCN9999_x.mp4", None, r"the progress sheet has no GOFCN9999 \(the number in the file name\)"), ("无编号.mp4", None, "has no GOFCN number")):
            other = materials({file: "b"})
            found_other, skipped_other = hashed([other])
            self.assertRaisesRegex(Blocked, pattern, build_upload_plan, {"request": normalize_upload_request({"files": [other], "progress": True, **({"items": items} if items else {})}),
                                                                       "options": people, "materials": found_other, "skipped": skipped_other, "existing": set(), "progress": rows})

    def test_final_cut(self) -> None:
        """The 终稿库 example: each video goes up named as the row the requirement says it is, two rows of one name told apart by their numbers."""
        snapshot = Snapshot(final_cut_sheet())
        rows = progress_rows_from(snapshot, [56890, 44065, 67680, 67681, 67682, 67683, 67684])
        self.assertEqual(rows[67682][0]["name"], "GOFCN67682_AI生成_设定3.0_3D_围炉煮冰")
        self.assertEqual(rows[67683][0]["name"], "GOFCN67683_AI生成_设定3.0_3D_围炉煮冰")
        self.assertEqual(rows[67684][0]["name"], "GOFCN67684_AI生成_设定3.0_关键词", 'SUBSTITUTE(CONCATENATE(…),"__","_") as the sheet shows it')
        found, skipped = hashed([materials(FINAL_CUT_FILES)])

        def build(request: dict, existing: Optional[set] = None, progress: Any = rows, options: dict = OPTIONS) -> dict:
            return build_upload_plan({"request": normalize_upload_request({"files": ["unused"], **request}), "options": options, "materials": found, "skipped": skipped,
                                      "existing": existing or set(), "progress": progress})

        # As they come out of the zip, none of the names is a 素材名称 and none carries a GOFCN number: all four are asked about at once.
        self.assertRaisesRegex(Blocked, r"has no GOFCN number.*g44065_gf_v90\.mp4, g44065_v90\.mp4, g56890_a_v15\.mp4, g56890_c_v21\.mp4", build, {})
        # The sheet's supplier is not CLS's name for it: the refusal says so and how to name it.
        self.assertRaisesRegex(Blocked, r"制作人 \"廿十一\".*this is the 设计师供应商 of GOFCN6768[0-9] in the progress sheet.*give the CLS name in this file's items\[\]\.producers", build, {"items": FINAL_CUT_ITEMS})
        plan = build({"producers": ["长沙廿拾一文化传媒有限公司"], "items": FINAL_CUT_ITEMS})
        by = {i["file_name"]: i for i in plan["items"]}
        self.assertEqual({f: [i["name"], i["upload_name"]] for f, i in by.items()}, {
            "g56890_a_v15.mp4": ["GOFCN67680_AI生成_设定3.0_3D_企鹅保龄球", "GOFCN67680_AI生成_设定3.0_3D_企鹅保龄球.mp4"],
            "g56890_c_v21.mp4": ["GOFCN67681_AI生成_设定3.0_3D_冻鱼飞盘", "GOFCN67681_AI生成_设定3.0_3D_冻鱼飞盘.mp4"],
            "g44065_v90.mp4": ["GOFCN67682_AI生成_设定3.0_3D_围炉煮冰", "GOFCN67682_AI生成_设定3.0_3D_围炉煮冰.mp4"],
            "g44065_gf_v90.mp4": ["GOFCN67683_AI生成_设定3.0_3D_围炉煮冰", "GOFCN67683_AI生成_设定3.0_3D_围炉煮冰.mp4"]})
        self.assertTrue(all(i["creator"]["name"] == "金莉" and i["source"]["creator"] == "progress" and i["source"]["progress_row"]["by"] == "item" for i in plan["items"]))
        self.assertEqual([by["g56890_a_v15.mp4"]["renamed_from"], by["g56890_a_v15.mp4"]["number"]], ["g56890_a_v15", 67680])
        summary = upload_plan_summary(plan, "plan.json")
        self.assertEqual(summary["renamed"], 4)
        self.assertEqual(next(i for i in summary["items"] if i["file"] == "g44065_gf_v90.mp4"),
                         {"file": "g44065_gf_v90.mp4", "name": "GOFCN67683_AI生成_设定3.0_3D_围炉煮冰", "renamed": True, "type": "视频", "creator": "金莉",
                          "producers": ["长沙廿拾一文化传媒有限公司"], "progress_row": "26年1月-GOFCN row 7", "row_from": "request"})
        # Rows without a 设计师供应商: finished materials were made by someone, so the 制作人 is asked for, never CLS's 待定供应商.
        open_rows = {n: [{**r, "supplier": ""} for r in found_rows] for n, found_rows in rows.items()}
        options = {**OPTIONS, "producers": [*OPTIONS["producers"], {"id": 51, "name": "待定供应商"}]}
        self.assertRaisesRegex(Blocked, r"制作人 not decided \(the 设计师供应商 of GOFCN67680 in the progress sheet is empty, as it is when the supplier is the reviewer: ask the user who made the material", build,
                               {"items": FINAL_CUT_ITEMS}, None, open_rows, options)
        named = build({"producers": ["廿拾一文化"], "items": FINAL_CUT_ITEMS}, None, open_rows, options)
        self.assertTrue(all(",".join(p["name"] for p in i["producers"]) == "长沙廿拾一文化传媒有限公司" and i["source"]["producers"] == "request" for i in named["items"]))

    def test_binding(self) -> None:
        """Binding a file to a row: one file per row, an original's number in a file name is caught, a file already named right is left as it is."""
        rows = progress_rows_from(Snapshot(final_cut_sheet()), [56890, 44065, 67680, 67681, 67682, 67683])

        def plan(files: dict, request: dict, existing: tuple = ()) -> dict:
            found, skipped = hashed([materials(files)])
            return build_upload_plan({"request": normalize_upload_request({"files": ["unused"], "producers": ["长沙廿拾一文化传媒有限公司"], **request}), "options": OPTIONS,
                                      "materials": found, "skipped": skipped, "existing": set(existing), "progress": rows})

        self.assertRaisesRegex(Blocked, r"two files are the same progress-sheet row GOFCN67682 \(a\.mp4, b\.mp4\)", plan, {"a.mp4": "1", "b.mp4": "2"},
                               {"items": [{"file": "a.mp4", "number": 67682}, {"file": "b.mp4", "number": 67682}]})
        self.assertRaisesRegex(Blocked, r"the progress sheet has no GOFCN67999: a\.mp4", plan, {"a.mp4": "1"}, {"items": [{"file": "a.mp4", "number": 67999}]})
        # Named like the 终稿库 headings, the number is the original the piece builds on (GOFCN56890).
        original = "GOFCN56890_汉化_竞争对抗_3D_火车运输御敌-斜"
        for existing in ((original,), (), ("GOFCN56890_火车运输_终版",)):
            self.assertRaisesRegex(Blocked, r"the number may be the earlier piece it builds on.*: GOFCN56890_企鹅保龄球_V15\.mp4", plan, {"GOFCN56890_企鹅保龄球_V15.mp4": "p"}, {}, existing)
        bound = plan({"GOFCN56890_企鹅保龄球_V15.mp4": "p"}, {"items": [{"file": "GOFCN56890_企鹅保龄球_V15.mp4", "number": 67680}]}, (original,))
        self.assertEqual(bound["items"][0]["name"], "GOFCN67680_AI生成_设定3.0_3D_企鹅保龄球", "the row the request names wins over the number in the file name")
        self.assertRaisesRegex(Blocked, "CLS has this row's creative already", plan, {"x.mp4": "p"}, {"items": [{"file": "x.mp4", "number": 67680}]}, ("GOFCN67680_AI生成_设定3.0_3D_企鹅保龄球",))
        right = plan({"GOFCN67681_AI生成_设定3.0_3D_冻鱼飞盘.mp4": "f"}, {})
        self.assertEqual(right["items"][0]["name"], "GOFCN67681_AI生成_设定3.0_3D_冻鱼飞盘")
        self.assertTrue("renamed_from" not in right["items"][0] and "upload_name" not in right["items"][0])
        self.assertNotIn("renamed", upload_plan_summary(right, "p.json"))
        self.assertRaisesRegex(Blocked, "looks uploaded before", plan, {"GOFCN67681_AI生成_设定3.0_3D_冻鱼飞盘.mp4": "f"}, {}, ("GOFCN67681_AI生成_设定3.0_3D_冻鱼飞盘",))

    def test_rows_without_names(self) -> None:
        """Rows the sheet cannot name: alike duplicates are one row, items[].sheet picks a tab, and only a row without a usable name takes items[].name."""
        file = os.path.join(temp_dir("progress-dup-"), "progress.xlsx")
        write_file(file, make_xlsx([
            {"name": "26年1月-GOFCN", "rows": [HEADER, progress_row(2, 66909, "饮料睡袋", ("AI生成", "设定3.0", "2D"), ("廿十一", "金莉")),
                                              progress_row(3, 66910, "冰雕", ("AI生成", "设定3.0", "2D"), ("廿十一", "金莉")),
                                              [None, None, 66913, "丁杰的片子", None, None, None, "AI生成", "设定3.0", None, "2D", None, "廿十一", "金莉"],
                                              [None, None, 66914, "横版", None, None, "GOFCN66914_AI生成_横版/竖版", "AI生成", "设定3.0", None, "2D", None, "廿十一", "金莉"]]},
            {"name": "26年2月-GOFCN", "rows": [HEADER, progress_row(2, 66909, "饮料睡袋", ("AI生成", "设定3.0", "2D"), ("廿十一", "金莉")),
                                              progress_row(3, 66910, "冰雕", ("AI生成", "设定3.0", "2D"), ("长沙廿拾一文化传媒有限公司", "金莉"))]}]))
        rows = progress_rows_from(Snapshot(file), [66909, 66910, 66913, 66914])

        def plan(items: list[dict]) -> dict:
            found, skipped = hashed([materials({"a.mp4": "a"})])
            return build_upload_plan({"request": normalize_upload_request({"files": ["unused"], "producers": ["长沙廿拾一文化传媒有限公司"], "items": items}), "options": OPTIONS,
                                      "materials": found, "skipped": skipped, "existing": set(), "progress": rows})

        self.assertEqual(plan([{"file": "a.mp4", "number": 66909}])["items"][0]["name"], "GOFCN66909_AI生成_设定3.0_2D_饮料睡袋", "carried into the next month alike: one row")
        self.assertRaisesRegex(Blocked, r"GOFCN66910 is on 2 progress-sheet rows that differ.*name the tab with items\[\]\.sheet", plan, [{"file": "a.mp4", "number": 66910}])
        picked = plan([{"file": "a.mp4", "number": 66910, "sheet": "26年2月-GOFCN"}])
        self.assertEqual([picked["items"][0]["source"]["progress_row"]["sheet"], picked["items"][0]["name"]], ["26年2月-GOFCN", "GOFCN66910_AI生成_设定3.0_2D_冰雕"])
        self.assertRaisesRegex(Blocked, r"the progress-sheet tab \"25年12月-GOFCN\" has no GOFCN66910 \(it is on 26年1月-GOFCN, 26年2月-GOFCN\)", plan, [{"file": "a.mp4", "number": 66910, "sheet": "25年12月-GOFCN"}])
        self.assertRaisesRegex(Blocked, r"GOFCN66913.*is empty or cannot be worked out: the user fixes the progress sheet and the upload is planned again, or the user confirms a name, given in items\[\]\.name", plan, [{"file": "a.mp4", "number": 66913}])
        confirmed = plan([{"file": "a.mp4", "number": 66913, "name": "GOFCN66913_AI生成_设定3.0_2D_丁杰的片子"}])
        self.assertEqual([confirmed["items"][0]["name"], confirmed["items"][0]["source"]["name"]], ["GOFCN66913_AI生成_设定3.0_2D_丁杰的片子", "item"])
        self.assertRaisesRegex(Blocked, r"contains / or \\", plan, [{"file": "a.mp4", "number": 66914}])
        self.assertEqual(plan([{"file": "a.mp4", "number": 66914, "name": "GOFCN66914_AI生成_横版竖版"}])["items"][0]["name"], "GOFCN66914_AI生成_横版竖版")
        self.assertRaisesRegex(Blocked, r"which is the name: drop items\[\]\.name", plan, [{"file": "a.mp4", "number": 66909, "name": "另起的名字"}])

    def test_two_zips(self) -> None:
        """Files of the same name in two zips are told apart by zip, and a refusal names every file that needs a row."""
        root = temp_dir("zip-same-")
        a = write_file(os.path.join(root, "供应商A.zip"), make_zip([{"name": "01.mp4", "data": "a"}]))
        b = write_file(os.path.join(root, "供应商B.zip"), make_zip([{"name": "01.mp4", "data": "b"}, *[{"name": f"{str(i + 2).zfill(2)}.mp4", "data": f"b{i}"} for i in range(11)]]))
        unpacked = {z.lower(): unpack_zip(z, os.path.join(root, "unpacked"), material_refusal).dir for z in (a, b)}
        scanned, scan_skipped = scan_materials([a, b], unpacked)
        found = [{**m, **hash_file(m["file"])} for m in scanned]
        rows = progress_rows_from(Snapshot(final_cut_sheet()), [67680, 67681])

        def plan(items: list[dict]) -> dict:
            return build_upload_plan({"request": normalize_upload_request({"files": [a, b], "producers": ["长沙廿拾一文化传媒有限公司"], "items": items}), "options": OPTIONS,
                                      "materials": found, "skipped": scan_skipped, "existing": set(), "progress": rows})

        self.assertRaisesRegex(Blocked, r"items names \"01\.mp4\", which matches several files \(供应商A\.zip：01\.mp4, 供应商B\.zip：01\.mp4\): name one of them", plan, [{"file": "01.mp4", "number": 67680}])
        with self.assertRaises(Blocked) as caught:
            plan([])
        self.assertTrue(all(f in caught.exception.message for f in ("供应商A.zip：01.mp4", "供应商B.zip：01.mp4", "供应商B.zip：12.mp4")) and "等 " not in caught.exception.message,
                        "all 13 files are named, by zip")
        rest = [{"file": f"{str(i + 2).zfill(2)}.mp4", "skip": True} for i in range(11)]
        done = plan([{"file": "供应商A.zip：01.mp4", "number": 67680}, {"file": "供应商B.zip：01.mp4", "number": 67681}, *rest])
        self.assertEqual([[i["origin"], i["name"]] for i in done["items"]], [["供应商A.zip：01.mp4", "GOFCN67680_AI生成_设定3.0_3D_企鹅保龄球"], ["供应商B.zip：01.mp4", "GOFCN67681_AI生成_设定3.0_3D_冻鱼飞盘"]])
        self.assertEqual([i["file"] for i in upload_plan_summary(done, "p.json")["items"]], ["供应商A.zip：01.mp4", "供应商B.zip：01.mp4"])

    def test_rows(self) -> None:
        """Rows: by number and by text in 素材名称 or 提炼词, newest first; numbers not on the sheet are said."""
        snapshot = Snapshot(final_cut_sheet())
        found = search_progress_rows(snapshot, [67683, 99999], ["围炉煮冰", "企鹅保龄球"])
        self.assertEqual([r["number"] for r in found["rows"]], [67683, 67682, 67680], "the numbers asked for first, then the text hits not already listed")
        self.assertEqual(found["not_found"], [99999])
        self.assertEqual(found["rows"][0]["keyword"], "围炉煮冰")
        self.assertEqual([[r["number"], r["name"], r["primary_tag"], r["supplier"], r["reviewer"]] for r in search_progress_rows(snapshot, [], ["转斧杀熊"])["rows"]],
                         [[44065, "GOFCN44065_汉化_资源城建_3D_转斧杀熊烤肉", "汉化", "廿十一", "金莉"]])
        exports: list[str] = []
        listed = find_rows(SheetBridge(final_cut_sheet(), exports), [67680, 67681], [], True, UploadDeps(tmp_dir=temp_dir("rows-tmp-")))
        self.assertEqual(listed["status"], "progress_rows")
        self.assertEqual([[r["number"], r["name"], r["where"]] for r in listed["rows"]], [
            [67680, "GOFCN67680_AI生成_设定3.0_3D_企鹅保龄球", "26年1月-GOFCN row 4"], [67681, "GOFCN67681_AI生成_设定3.0_3D_冻鱼飞盘", "26年1月-GOFCN row 5"]])
        self.assertEqual(len(exports), 1)
        self.assertFalse(os.path.exists(exports[0]), "the export is removed again")
        self.assertRaisesRegex(Blocked, "--numbers or --find", find_rows, SheetBridge(final_cut_sheet()), [], [])

    def test_files_check(self) -> None:
        """files --check: a file named with its number is found in the progress sheet, or said to differ from its row, or to have none."""
        directory = materials({"GOFCN67680_AI生成_设定3.0_3D_企鹅保龄球.mp4": "registered", "GOFCN67681_AI生成_设定3.0_3D_冻鱼飞盘_竖版.mp4": "variant",
                               "GOFCN67682_AI生成_设定3.0_3D_企鹅保龄球.mp4": "differs", "GOFCN99999_AI生成_设定3.0_3D_冻鱼飞盘.mp4": "missing", "01.mp4": "plain"})
        exports: list[str] = []
        listed = list_files([directory], UploadDeps(tmp_dir=temp_dir("files-tmp-")), {"bridge": SheetBridge(final_cut_sheet(), exports), "progress": True})

        def row(name: str) -> Any:
            return next(f for f in listed["files"] if f["name"] == name).get("row")

        self.assertEqual(row("GOFCN67680_AI生成_设定3.0_3D_企鹅保龄球"), {"status": "registered", "rows": [{"number": 67680, "name": "GOFCN67680_AI生成_设定3.0_3D_企鹅保龄球", "where": "26年1月-GOFCN row 4"}]})
        self.assertEqual(row("GOFCN67681_AI生成_设定3.0_3D_冻鱼飞盘_竖版"), {"status": "variant", "rows": [{"number": 67681, "name": "GOFCN67681_AI生成_设定3.0_3D_冻鱼飞盘", "where": "26年1月-GOFCN row 5"}]})
        self.assertEqual(row("GOFCN67682_AI生成_设定3.0_3D_企鹅保龄球"), {"status": "differs",
                                                                    "rows": [{"number": 67682, "name": "GOFCN67682_AI生成_设定3.0_3D_围炉煮冰", "where": "26年1月-GOFCN row 6"}],
                                                                    "same_name_rows": [{"number": 67680, "name": "GOFCN67680_AI生成_设定3.0_3D_企鹅保龄球", "where": "26年1月-GOFCN row 4"}]})
        self.assertEqual(row("GOFCN99999_AI生成_设定3.0_3D_冻鱼飞盘"), {"status": "not_found",
                                                                  "same_name_rows": [{"number": 67681, "name": "GOFCN67681_AI生成_设定3.0_3D_冻鱼飞盘", "where": "26年1月-GOFCN row 5"}]})
        self.assertIsNone(row("01"), "a file without a number is not looked for")
        self.assertEqual([listed["progress_source"], listed["registered"], listed["to_verify"]], ["the skill's progress sheet", 1, 3])
        self.assertEqual(len(exports), 1)
        # GOFCNxxxxx_… files are found by their name without the number; without --check nothing is read.
        pending = list_files([materials(PENDING)], UploadDeps(tmp_dir=temp_dir("files-tmp-")), {"bridge": SheetBridge(registered_sheet()), "progress": True})
        self.assertEqual(sorted([f["row"]["status"], f["row"]["rows"][0]["number"]] for f in pending["files"]), [["registered", 67738], ["registered", 67739], ["registered", 67740]])
        offline = list_files([directory], UploadDeps(tmp_dir=temp_dir("files-tmp-")))
        self.assertTrue(all("row" not in f for f in offline["files"]) and "registered" not in offline)

    def test_same_content_listed(self) -> None:
        """Two files of the same content are said: creative files lists the other copies."""
        directory = materials({"01.mp4": "same bytes", "01 - 副本.mp4": "same bytes", "02.mp4": "other bytes"})
        listed = list_files([directory], deps_for())
        self.assertEqual({f["file"]: f.get("same_content") for f in listed["files"]}, {"01 - 副本.mp4": ["01.mp4"], "01.mp4": ["01 - 副本.mp4"], "02.mp4": None})

    def test_zip_renamed(self) -> None:
        """Files a zip names with characters Windows cannot hold are listed under the name they are read by, and said to be renamed."""
        zip_ = write_file(os.path.join(temp_dir("zip-input-"), "mac.zip"), make_zip([{"name": "GOFCNxxxxx_AI玩法优化_搂冰块抢资源_对抗:竖版.mp4", "data": "v"}, {"name": "ok.mp4", "data": "o"}]))
        listed = list_files([zip_], UploadDeps(tmp_dir=temp_dir("files-tmp-")))
        self.assertEqual(sorted(f["file"] for f in listed["files"]), ["mac.zip：GOFCNxxxxx_AI玩法优化_搂冰块抢资源_对抗_竖版.mp4", "mac.zip：ok.mp4"])
        self.assertEqual(listed["zip_renamed"], [{"file": "mac.zip：GOFCNxxxxx_AI玩法优化_搂冰块抢资源_对抗:竖版.mp4", "as": "GOFCNxxxxx_AI玩法优化_搂冰块抢资源_对抗_竖版.mp4"}])


def registered_sheet(extra: Optional[list] = None) -> str:
    """The production sheet after the files of 玩法AI迭代素材 were registered: rows named GOFCN<编号>_<一级>_<二级>_<提炼词>, the newest formula."""
    file = os.path.join(temp_dir("progress-"), "progress.xlsx")

    def row(r: int, number: int, tags: tuple[str, str], keyword: str) -> list:
        return [None, None, number, keyword, None, None, {"f": f'CONCATENATE("GOFCN",C{r},"_",H{r},"_",I{r},"_",D{r})'}, tags[0], tags[1], None, None, None, "廿十一", "金莉"]

    write_file(file, make_xlsx([{"name": "26年1月-GOFCN", "rows": [HEADER, row(2, 67738, ("AI玩法优化", "搂冰块抢资源"), "割资源冰块对抗_竖版"),
                                                                   row(3, 67739, ("AI玩法优化", "火车运输御敌"), "兵临城下塔防_企鹅_竖版"),
                                                                   row(4, 67740, ("AI玩法优化", "火车运输御敌"), "兵临城下塔防_冻鱼_竖版"),
                                                                   *[row(5 + i, *cells) for i, cells in enumerate(extra or [])]]}]))
    return file


PENDING = {"GOFCNxxxxx_AI玩法优化_搂冰块抢资源_割资源冰块对抗_竖版.mp4": "ice", "GOFCNxxxxx_AI玩法优化_火车运输御敌_兵临城下塔防_企鹅_竖版.mp4": "penguin",
           "GOFCNxxxxx_AI玩法优化_火车运输御敌_兵临城下塔防_冻鱼_竖版.mp4": "fish"}


def variant_sheet(extra: Optional[list] = None) -> str:
    """合并变体名称: the AI剧情 pieces registered as one row each, and a piece registered so for files named GOFCNxxxxx_…."""
    file = os.path.join(temp_dir("progress-"), "progress.xlsx")

    def row(r: int, number: int, tags: tuple[str, str], keyword: str) -> list:
        return [None, None, number, keyword, None, None, {"f": f'CONCATENATE("GOFCN",C{r},"_",H{r},"_",I{r},"_",D{r})'}, tags[0], tags[1], None, None, None, "廿十一", "金莉"]

    write_file(file, make_xlsx([{"name": "26年1月-GOFCN", "rows": [HEADER, row(2, 67076, ("AI剧情", "短剧表达"), "原生家庭WM"), row(3, 67146, ("AI剧情", "短剧表达"), "得加钱WM"),
                                                                   row(4, 67750, ("AI玩法优化", "火车运输御敌"), "兵临城下塔防"),
                                                                   *[row(5 + i, *cells) for i, cells in enumerate(extra or [])]]}]))
    return file


VARIANTS = {"GOFCN67076_AI剧情_短剧表达_原生家庭WM_剧情衔接_割资源冰块对抗_竖版.mp4": "ice", "GOFCN67076_AI剧情_短剧表达_原生家庭WM_剧情衔接_剧情叙事_竖版.mp4": "story",
            "GOFCN67146_AI剧情_短剧表达_得加钱WM熔炉版_剧情衔接_割资源冰块对抗_竖版.mp4": "furnace", "GOFCNxxxxx_AI剧情_短剧表达_得加钱WM熔炉版_剧情衔接_扩岛对抗_竖版.mp4": "furnace-island",
            "GOFCNxxxxx_AI玩法优化_火车运输御敌_兵临城下塔防_冻鱼_竖版.mp4": "fish", "GOFCNxxxxx_AI玩法优化_火车运输御敌_兵临城下塔防_企鹅_竖版.mp4": "penguin", "01.mp4": "island"}


class PendingAndVariantTests(unittest.TestCase):
    def test_pending(self) -> None:
        """Files named GOFCNxxxxx_… before their rows existed go up as those rows once they do, the number filled in."""
        self.assertEqual([pending_body("GOFCNxxxxx_AI玩法优化_搂冰块"), pending_body("GOFCN_a_b"), pending_body("GOFCN67738_a_b"), pending_body("01")], ["AI玩法优化_搂冰块", "a_b", None, None])
        # Read the way materials are named: 一级_二级_[创意形式_]提炼词; everything after the tags is `rest`, 竖版 / 横版 included.
        self.assertEqual(name_parts("AI玩法优化_火车运输御敌_兵临城下塔防_企鹅_竖版"), {"primary_tag": "AI玩法优化", "secondary_tag": "火车运输御敌", "rest": "兵临城下塔防_企鹅_竖版"})
        self.assertEqual(name_parts("AI生成_设定3.0_3D_围炉_煮冰_横板"), {"primary_tag": "AI生成", "secondary_tag": "设定3.0", "rest": "3D_围炉_煮冰_横板"})
        self.assertEqual(name_parts("AI玩法优化_搂冰块抢资源_割资源冰块对抗_竖版"), {"primary_tag": "AI玩法优化", "secondary_tag": "搂冰块抢资源", "rest": "割资源冰块对抗_竖版"})
        self.assertIn("unclear", name_parts("AI玩法优化_搂冰块抢资源"))
        self.assertIn("unclear", name_parts("AI玩法优化__企鹅"))
        directory = materials(PENDING)
        listed = list_files([directory], UploadDeps(tmp_dir=temp_dir("files-tmp-")))
        self.assertEqual(next(f for f in listed["files"] if "企鹅" in f["name"])["parts"]["rest"], "兵临城下塔防_企鹅_竖版")
        self.assertEqual(sorted([f["name_without_number"], f["pending_number"], f["kind"]] for f in listed["files"]),
                         sorted([re.sub(r"\.mp4$", "", re.sub("^GOFCNxxxxx_", "", n)), True, "video"] for n in PENDING))
        fake = FakeCreative()
        try:
            deps = deps_for()
            file = approved_plan(fake, deps, {"files": [directory], "progress": True, "producers": ["长沙廿拾一文化传媒有限公司"]}, SheetBridge(registered_sheet()))
            plan = plan_of(file)
            self.assertEqual(sorted([i["name"], i["source"]["progress_row"]["by"], "renamed_from" in i] for i in plan["items"]), [
                ["GOFCN67738_AI玩法优化_搂冰块抢资源_割资源冰块对抗_竖版", "name", True], ["GOFCN67739_AI玩法优化_火车运输御敌_兵临城下塔防_企鹅_竖版", "name", True],
                ["GOFCN67740_AI玩法优化_火车运输御敌_兵临城下塔防_冻鱼_竖版", "name", True]])
            self.assertTrue(all(i["creator"]["name"] == "金莉" for i in plan["items"]), "创意人 from the row")
            result = run_upload(online(fake), file, deps)
            self.assertEqual(result["status"], "uploaded", result)
            signed = next(r for r in fake.requests if r["path"] == "/api/qcloud/signed-url")["body"]["files"]
            self.assertIn("GOFCN67739_AI玩法优化_火车运输御敌_兵临城下塔防_企鹅_竖版/GOFCN67739_AI玩法优化_火车运输御敌_兵临城下塔防_企鹅_竖版.mp4", signed)
        finally:
            fake.close()

    def test_pending_without_row(self) -> None:
        """A GOFCNxxxxx_… file without its row is to be registered first, and one of two rows of its name is named by number."""
        snapshot = Snapshot(registered_sheet([(67741, ("AI玩法优化", "搂冰块抢资源"), "割资源冰块对抗_竖版")]))

        def plan(files: dict, items: Optional[list] = None) -> dict:
            items = items or []
            found, skipped = hashed([materials(files)])
            bodies = [b for b in (pending_body(m["name"]) for m in found) if b is not None]
            by_number, by_body = lookup_rows(snapshot, [int(i["number"]) for i in items if i.get("number")], bodies)
            return build_upload_plan({"request": normalize_upload_request({"files": ["unused"], "producers": ["长沙廿拾一文化传媒有限公司"], "items": items}), "options": OPTIONS,
                                      "materials": found, "skipped": skipped, "existing": set(), "progress": by_number, "named": by_body})

        self.assertRaisesRegex(Blocked, "the progress sheet has no row whose 素材名称 is \"GOFCN<number>_AI玩法优化_转斧杀熊烤肉_杀熊订单_国风配乐_竖版\": register these materials first", plan,
                               {"GOFCNxxxxx_AI玩法优化_转斧杀熊烤肉_杀熊订单_国风配乐_竖版.mp4": "g"})
        twice = {"GOFCNxxxxx_AI玩法优化_搂冰块抢资源_割资源冰块对抗_竖版.mp4": "ice"}
        self.assertRaisesRegex(Blocked, r"2 progress-sheet rows have the 素材名称 \"GOFCN<number>_AI玩法优化_搂冰块抢资源_割资源冰块对抗_竖版\" \(GOFCN67738, GOFCN67741\): name the row with items\[\]\.number", plan, twice)
        picked = plan(twice, [{"file": "GOFCNxxxxx_AI玩法优化_搂冰块抢资源_割资源冰块对抗_竖版.mp4", "number": 67741}])
        self.assertEqual([picked["items"][0]["name"], picked["items"][0]["source"]["progress_row"]["by"]], ["GOFCN67741_AI玩法优化_搂冰块抢资源_割资源冰块对抗_竖版", "item"])

    def test_variants(self) -> None:
        """合并变体名称: several files of one row go up as <素材名称>_<变体>, their names kept or the number filled in; off, nothing changes."""
        self.assertEqual(variant_bases("AI剧情_短剧表达_原生家庭WM_剧情衔接_竖版")[:2], ["AI剧情_短剧表达_原生家庭WM_剧情衔接", "AI剧情_短剧表达_原生家庭WM"], "cut at _ first, longest first")
        self.assertEqual(variant_bases("AI剧情_短剧表达_得加钱WM"), ["AI剧情_短剧表达_得加钱W", "AI剧情_短剧表达_得加钱", "AI剧情_短剧表达_得加", "AI剧情_短剧表达_得"])
        self.assertIn("AI剧情_短剧表达_得加钱WM", variant_bases("AI剧情_短剧表达_得加钱WM熔炉版_剧情衔接"), "the first word cut short")
        self.assertIn("AI生成_设定3.0_3D_企鹅", variant_bases("AI生成_设定3.0_3D_企鹅保龄球_竖版"), "or the first word after a 创意形式")
        fake = FakeCreative()
        try:
            deps = deps_for()
            directory = materials(VARIANTS)
            request = {"files": [directory], "progress": True, "producers": ["长沙廿拾一文化传媒有限公司"], "items": [{"file": "01.mp4", "number": 67076}]}
            file = approved_plan(fake, deps, {**request, "variants": True, "items": [{"file": "01.mp4", "number": 67076, "variant": "剧情衔接_扩岛对抗_竖版"}]}, SheetBridge(variant_sheet()))
            plan = plan_of(file)
            self.assertEqual({i["file_name"]: [i["name"], i.get("variant"), i["number"], i["source"]["progress_row"]["by"], i["source"].get("variant"), "renamed_from" in i] for i in plan["items"]}, {
                "GOFCN67076_AI剧情_短剧表达_原生家庭WM_剧情衔接_割资源冰块对抗_竖版.mp4": ["GOFCN67076_AI剧情_短剧表达_原生家庭WM_剧情衔接_割资源冰块对抗_竖版", "剧情衔接_割资源冰块对抗_竖版", 67076, "file_name", "file", False],
                "GOFCN67076_AI剧情_短剧表达_原生家庭WM_剧情衔接_剧情叙事_竖版.mp4": ["GOFCN67076_AI剧情_短剧表达_原生家庭WM_剧情衔接_剧情叙事_竖版", "剧情衔接_剧情叙事_竖版", 67076, "file_name", "file", False],
                "GOFCN67146_AI剧情_短剧表达_得加钱WM熔炉版_剧情衔接_割资源冰块对抗_竖版.mp4": ["GOFCN67146_AI剧情_短剧表达_得加钱WM熔炉版_剧情衔接_割资源冰块对抗_竖版", "熔炉版_剧情衔接_割资源冰块对抗_竖版", 67146, "file_name", "file", False],
                "GOFCNxxxxx_AI剧情_短剧表达_得加钱WM熔炉版_剧情衔接_扩岛对抗_竖版.mp4": ["GOFCN67146_AI剧情_短剧表达_得加钱WM熔炉版_剧情衔接_扩岛对抗_竖版", "熔炉版_剧情衔接_扩岛对抗_竖版", 67146, "name", "file", True],
                "GOFCNxxxxx_AI玩法优化_火车运输御敌_兵临城下塔防_冻鱼_竖版.mp4": ["GOFCN67750_AI玩法优化_火车运输御敌_兵临城下塔防_冻鱼_竖版", "冻鱼_竖版", 67750, "name", "file", True],
                "GOFCNxxxxx_AI玩法优化_火车运输御敌_兵临城下塔防_企鹅_竖版.mp4": ["GOFCN67750_AI玩法优化_火车运输御敌_兵临城下塔防_企鹅_竖版", "企鹅_竖版", 67750, "name", "file", True],
                "01.mp4": ["GOFCN67076_AI剧情_短剧表达_原生家庭WM_剧情衔接_扩岛对抗_竖版", "剧情衔接_扩岛对抗_竖版", 67076, "item", "item", True]})
            summary = upload_plan_summary(plan, file)
            self.assertEqual([summary["count"], summary["rows"], summary["renamed"]], [7, 3, 4])
            self.assertEqual(next(i for i in summary["items"] if i["file"] == "01.mp4")["variant"], "剧情衔接_扩岛对抗_竖版")
            result = run_upload(online(fake), file, deps)
            self.assertEqual(result["status"], "uploaded", result)
            self.assertEqual(sorted(u["name"] for u in result["uploaded"]), sorted(i["name"] for i in plan["items"]))
            # Off (the default), each file is a row of its own name: the numbered ones are taken for originals, the others are to be registered.
            off = write_file(os.path.join(temp_dir("off-"), "request.json"), json.dumps(request, ensure_ascii=False))
            with self.assertRaises(Blocked) as caught:
                plan_upload(online(fake), SheetBridge(variant_sheet()), off, os.path.join(os.path.dirname(off), "plan.json"), deps)
            message = caught.exception.message
            self.assertTrue("the number may be the earlier piece it builds on" in message and "has no row whose 素材名称 is \"GOFCN<number>_AI玩法优化_火车运输御敌_兵临城下塔防_冻鱼_竖版\"" in message and "variant" not in message)
        finally:
            fake.close()

    def test_variant_rules(self) -> None:
        """合并变体名称: a row of the file's own name wins, two rows it may be a variant of or two files of one name are asked, the setting only with the sheet."""
        snapshot = Snapshot(variant_sheet([(67751, ("AI玩法优化", "火车运输御敌"), "兵临城下塔防_冻鱼_竖版"), (67752, ("AI剧情", "短剧表达"), "原生家庭WM_剧情衔接")]))

        def plan(files: dict, items: Optional[list] = None, variants: bool = True) -> dict:
            items = items or []
            found, skipped = hashed([materials(files)])
            bodies = [x for b in (pending_body(m["name"]) for m in found) if b is not None for x in [b, *variant_bases(b)]]
            numbers = [*[int(i["number"]) for i in items if i.get("number")], *[m["number"] for m in found if m["number"] is not None]]
            by_number, by_body = lookup_rows(snapshot, numbers, bodies)
            return build_upload_plan({"request": normalize_upload_request({"files": ["unused"], "producers": ["长沙廿拾一文化传媒有限公司"], "variants": variants, "items": items}),
                                      "options": OPTIONS, "materials": found, "skipped": skipped, "existing": set(), "progress": by_number, "named": by_body})

        own = plan({"GOFCNxxxxx_AI玩法优化_火车运输御敌_兵临城下塔防_冻鱼_竖版.mp4": "f"})
        self.assertEqual([own["items"][0]["name"], own["items"][0].get("variant")], ["GOFCN67751_AI玩法优化_火车运输御敌_兵临城下塔防_冻鱼_竖版", None], "registered under its own name before")
        self.assertRaisesRegex(Blocked, r"can be a variant of several progress-sheet rows \(GOFCN67752_AI剧情_短剧表达_原生家庭WM_剧情衔接; GOFCN67076_AI剧情_短剧表达_原生家庭WM\): name the row with items\[\]\.number", plan,
                               {"GOFCNxxxxx_AI剧情_短剧表达_原生家庭WM_剧情衔接_竖版.mp4": "a"})
        picked = plan({"GOFCNxxxxx_AI剧情_短剧表达_原生家庭WM_剧情衔接_竖版.mp4": "a"}, [{"file": "GOFCNxxxxx_AI剧情_短剧表达_原生家庭WM_剧情衔接_竖版.mp4", "number": 67076}])
        self.assertEqual([picked["items"][0]["name"], picked["items"][0]["variant"]], ["GOFCN67076_AI剧情_短剧表达_原生家庭WM_剧情衔接_竖版", "剧情衔接_竖版"])
        # 得加钱WM熔炉版 registered as a row of its own: its files could be variants of either row.
        both = Snapshot(variant_sheet([(67753, ("AI剧情", "短剧表达"), "得加钱WM熔炉版")]))
        furnace, none = hashed([materials({"GOFCNxxxxx_AI剧情_短剧表达_得加钱WM熔炉版_剧情叙事_竖版.mp4": "x"})])
        body = "AI剧情_短剧表达_得加钱WM熔炉版_剧情叙事_竖版"
        self.assertRaisesRegex(Blocked, r"can be a variant of several progress-sheet rows \(GOFCN67753_AI剧情_短剧表达_得加钱WM熔炉版; GOFCN67146_AI剧情_短剧表达_得加钱WM\)", build_upload_plan,
                               {"request": normalize_upload_request({"files": ["unused"], "producers": ["长沙廿拾一文化传媒有限公司"], "variants": True}), "options": OPTIONS,
                                "materials": furnace, "skipped": none, "existing": set(), "progress": {}, "named": lookup_rows(both, [], [body, *variant_bases(body)])[1]})
        self.assertRaisesRegex(Blocked, "has no row whose 素材名称 is \"GOFCN<number>_AI剧情_短剧表达_爆款WM_剧情衔接_竖版\" or its start", plan, {"GOFCNxxxxx_AI剧情_短剧表达_爆款WM_剧情衔接_竖版.mp4": "a"})
        self.assertRaisesRegex(Blocked, r"not the file's name, and the file name does not start with it \(no variant of it\)", plan, {"GOFCN67076_AI剧情_短剧表达_最大功率WM_剧情衔接_竖版.mp4": "a"})
        self.assertRaisesRegex(Blocked, r"two files would get the same creative name \"GOFCN67076_AI剧情_短剧表达_原生家庭WM\" \(01\.mp4, 02\.mp4\): each variant of a row needs its own variant name", plan,
                               {"01.mp4": "1", "02.mp4": "2"}, [{"file": "01.mp4", "number": 67076}, {"file": "02.mp4", "number": 67076}])
        told = plan({"01.mp4": "1", "02.mp4": "2"}, [{"file": "01.mp4", "number": 67076, "variant": "剧情叙事"}, {"file": "02.mp4", "number": 67076}])
        self.assertEqual([i["name"] for i in told["items"]], ["GOFCN67076_AI剧情_短剧表达_原生家庭WM_剧情叙事", "GOFCN67076_AI剧情_短剧表达_原生家庭WM"], "a file without a variant is the row itself")
        self.assertRaisesRegex(Blocked, "two files are the same progress-sheet row GOFCN67076", plan, {"01.mp4": "1", "02.mp4": "2"}, [{"file": "01.mp4", "number": 67076}, {"file": "02.mp4", "number": 67076}], False)
        self.assertRaisesRegex(Blocked, r"only works with variants: true", normalize_upload_request, {"files": ["a"], "items": [{"file": "a", "variant": "x"}]})
        self.assertRaisesRegex(Blocked, "progress cannot be false", normalize_upload_request, {"files": ["a"], "progress": False, "variants": True})
        self.assertRaisesRegex(Blocked, "takes name or variant, not both", normalize_upload_request, {"files": ["a"], "variants": True, "items": [{"file": "a", "name": "n", "variant": "x"}]})
        for bad in ("_竖版", "竖版_", "剧情__竖版", "横/竖"):
            self.assertRaisesRegex(Blocked, "cannot start or end with _", normalize_upload_request, {"files": ["a"], "variants": True, "items": [{"file": "a", "variant": bad}]})
        self.assertRaisesRegex(Blocked, "variants is true or false", normalize_upload_request, {"files": ["a"], "variants": "yes"})


class RunTests(unittest.TestCase):
    def test_sealed(self) -> None:
        """An approved plan cannot be edited by hand."""
        fake = FakeCreative()
        try:
            file = approved_plan(fake, deps_for(), {"files": [materials({"a.mp4": "x"})], "creator": "张天伟", "producers": ["丁杰"]})
            plan = plan_of(file)
            plan["items"][0]["creator"] = {"id": 1104, "name": "雷佳威"}
            write_file(file, json.dumps(plan, ensure_ascii=False))
            self.assertRaisesRegex(Blocked, "digest does not match", load_upload_plan, file)
        finally:
            fake.close()

    def test_run(self) -> None:
        """A run does what the page does: sign, put each file without CLS credentials, create once, annotate, read back."""
        fake = FakeCreative()
        try:
            deps = deps_for()
            directory = materials({"GOFCN66367_片头.mp4": "video bytes", "封面.png": "png bytes"})
            file = approved_plan(fake, deps, {"files": [directory], "creator": "张天伟", "producers": ["丁杰"]})
            result = run_upload(online(fake), file, deps)
            self.assertEqual(result["status"], "uploaded", result)
            self.assertEqual(len(result["uploaded"]), 2)
            self.assertTrue(all(u["verified"] and u["creator"] == "张天伟" and u["producers"][0] == "北京柒曦文化传媒有限公司（丁杰）" for u in result["uploaded"]))
            order = [f"{r['method']} {'/cos' if r['path'].startswith('/cos/') else r['path']}" for r in fake.requests if r["method"] != "GET"]
            self.assertEqual(order, ["POST /api/qcloud/signed-url", "POST /api/report/log", "PUT /cos", "PUT /cos", "POST /api/report/log", "POST /api/creative/multiple-upload", "POST /api/creative/multiple-edit"])
            create = next(r for r in fake.requests if r["path"] == "/api/creative/multiple-upload")
            self.assertEqual(create["query"], "?confirmed=1")
            self.assertEqual(create["body"]["adsdesk_dir_ids"], {"GOFCN66367_片头": 1585089, "封面": 1585089})
            edit = next(r for r in fake.requests if r["path"] == "/api/creative/multiple-edit")
            self.assertEqual(sorted([c["sponsor"], c["producers"], c["types"], c["custom_tags"]] for c in edit["body"]["creatives"]), [[653, [663], ["image"], []], [653, [663], ["video"], []]])
            self.assertTrue(all(r["authorization"] is None for r in fake.requests if r["method"] == "PUT"), "storage never sees the CLS login")
            self.assertTrue(all(o["content_type"] == "application/x-www-form-urlencoded" for o in fake.objects.values()), "the Content-Type the page sends")
            self.assertTrue(all("requirement-auto" in line for line in fake.logs), "the CLS log says the upload came from the skill")
            # No upload link (with its signature) is ever written down.
            for folder, _, names in os.walk(result["batch"]):
                for name in names:
                    if name.endswith(".json"):
                        with open(os.path.join(folder, name), encoding="utf-8") as handle:
                            self.assertNotRegex(handle.read(), "sign=|q-signature|Bearer")
            # Running the finished plan again only reads.
            before = len([r for r in fake.requests if r["method"] != "GET"])
            self.assertEqual(run_upload(online(fake), file, deps)["status"], "uploaded")
            self.assertEqual(len([r for r in fake.requests if r["method"] != "GET"]), before)
            self.assertIs(upload_status(file, deps)["create_sent"], True)
        finally:
            fake.close()

    def test_lost_create(self) -> None:
        """A create whose answer is lost is found by reading back, and never sent again."""
        fake = FakeCreative()
        try:
            deps = deps_for()
            fake.create_mode = "lose-answer"
            file = approved_plan(fake, deps, {"files": [materials({"a.mp4": "aa", "b.mp4": "bb"})], "creator": "张天伟", "producers": ["丁杰"]})
            result = run_upload(online(fake), file, deps)
            self.assertEqual(result["status"], "uploaded", result)
            self.assertEqual(writes(fake, "multiple-upload"), 1)
            steps = [e["step"] for e in upload_events(result["batch"])]
            self.assertTrue("create_unknown" in steps and "reconciled" in steps)
            fake.create_mode = "normal"
            run_upload(online(fake), file, deps)
            self.assertEqual(writes(fake, "multiple-upload"), 1, "not even on a later run")
        finally:
            fake.close()

    def test_create_not_happened(self) -> None:
        """A create that did not happen is reported as not found yet, and a later run still does not create."""
        fake = FakeCreative()
        try:
            deps = deps_for()
            fake.create_mode = "hang-up"
            file = approved_plan(fake, deps, {"files": [materials({"a.mp4": "aa"})], "creator": "张天伟", "producers": ["丁杰"]})
            result = run_upload(online(fake), file, deps)
            self.assertEqual(result["status"], "unknown")
            self.assertEqual([m["name"] for m in result["missing"]], ["a"])
            fake.create_mode = "normal"
            self.assertEqual(run_upload(online(fake), file, deps)["status"], "unknown")
            self.assertEqual(writes(fake, "multiple-upload"), 1)
            self.assertEqual(len(fake.creatives), 0)
        finally:
            fake.close()

    def test_made_by_hand(self) -> None:
        """A creative of the same name made by hand after a lost create is not taken for this batch's, and is left as it is."""
        fake = FakeCreative()
        try:
            deps = deps_for()
            fake.create_mode = "hang-up"
            file = approved_plan(fake, deps, {"files": [materials({"a.mp4": "aa"})], "creator": "张天伟", "producers": ["丁杰"]})
            self.assertEqual(run_upload(online(fake), file, deps)["status"], "unknown")
            # Meanwhile the user uploads something called "a" on the page, with other people and a tag.
            by_hand = {"id": 99, "name": "a", "upload_user_id": ACCOUNT["id"], "user_id": 1104, "user_ids": [1104], "producers": [1405], "types": ["aigc"],
                       "custom_tags": ["手动标签"], "confirmed": True, "materials": [{"id": 990, "name": "a", "file_hash": "f" * 32}]}
            fake.creatives[99] = json.loads(json.dumps(by_hand))
            fake.create_mode = "normal"
            again = run_upload(online(fake), file, deps)
            self.assertEqual(again["status"], "unknown")
            self.assertEqual([m["name"] for m in again["missing"]], ["a"])
            self.assertEqual(fake.creatives[99], by_hand, "not annotated")
            self.assertEqual(writes(fake, "multiple-edit"), 0)
        finally:
            fake.close()

    def test_same_md5(self) -> None:
        """A file CLS already has (same MD5) is reported, the rest go up."""
        fake = FakeCreative()
        try:
            deps = deps_for()
            directory = materials({"new.mp4": "fresh", "copy.mp4": "dup"})
            fake.creatives[1] = {"id": 1, "name": "old", "upload_user_id": 653, "user_id": 653, "user_ids": [653], "confirmed": True,
                                 "materials": [{"file_hash": hash_file(os.path.join(directory, "copy.mp4"))["md5"]}]}
            file = approved_plan(fake, deps, {"files": [directory], "creator": "张天伟", "producers": ["丁杰"]})
            result = run_upload(online(fake), file, deps)
            self.assertEqual(result["status"], "partial")
            self.assertEqual([u["name"] for u in result["uploaded"]], ["new"])
            self.assertEqual([d["name"] for d in result["not_created"]], ["copy"])
            self.assertRegex(result["not_created"][0]["reason"], "MD5")
        finally:
            fake.close()

    def test_refused_create(self) -> None:
        """A refused create is looked up once: nothing made is blocked, what was made anyway is annotated and the rest reported."""
        for mode, expected in (("refuse", "blocked"), ("refuse-after-first", "partial")):
            fake = FakeCreative()
            try:
                deps = deps_for()
                fake.create_mode = mode
                file = approved_plan(fake, deps, {"files": [materials({"a.mp4": "aa", "b.mp4": "bb"})], "creator": "张天伟", "producers": ["丁杰"]})
                result = run_upload(online(fake), file, deps)
                self.assertEqual(result["status"], expected, result)
                if expected == "blocked":
                    self.assertRegex(result["reason"], "business code 403, 素材处理失败")
                    self.assertEqual(writes(fake, "multiple-edit"), 0)
                else:
                    self.assertEqual([[u["name"], u["verified"]] for u in result["uploaded"]], [["a", True]])
                    self.assertEqual([n["name"] for n in result["not_created"]], ["b"])
                    self.assertRegex(result["not_created"][0]["reason"], "refused to create")
                fake.create_mode = "normal"
                run_upload(online(fake), file, deps)
                self.assertEqual(writes(fake, "multiple-upload"), 1, "a refused create is not sent again either")
            finally:
                fake.close()

    def test_annotation(self) -> None:
        """An annotation that did not take is sent again after the read-back; a refused one is reported."""
        fake = FakeCreative()
        try:
            deps = deps_for()
            fake.annotate_mode = "ignore-once"
            file = approved_plan(fake, deps, {"files": [materials({"a.mp4": "aa"})], "creator": "张天伟", "producers": ["丁杰"]})
            self.assertEqual(run_upload(online(fake), file, deps)["status"], "uploaded")
            self.assertEqual(writes(fake, "multiple-edit"), 2)
        finally:
            fake.close()
        other = FakeCreative()
        try:
            other.annotate_mode = "refuse"
            deps2 = deps_for()
            file2 = approved_plan(other, deps2, {"files": [materials({"b.mp4": "bb"})], "creator": "张天伟", "producers": ["丁杰"]})
            refused = run_upload(online(other), file2, deps2)
            self.assertEqual(refused["status"], "partial")
            self.assertRegex(refused["annotate_error"], "没有编辑权限")
            self.assertEqual(writes(other, "multiple-edit"), 1)
            self.assertEqual(refused["uploaded"][0]["problems"], ["创意人", "制作人", "素材类型"])
        finally:
            other.close()

    def test_nothing_sent(self) -> None:
        """Nothing is sent when a file changed after approval, a name got taken, or another account is logged in."""
        fake = FakeCreative()
        try:
            deps = deps_for()
            directory = materials({"a.mp4": "approved"})
            file = approved_plan(fake, deps, {"files": [directory], "creator": "张天伟", "producers": ["丁杰"]})
            write_file(os.path.join(directory, "a.mp4"), "replaced!")
            self.assertRaisesRegex(Blocked, "since the plan was approved", run_upload, online(fake), file, deps)
            write_file(os.path.join(directory, "a.mp4"), "approved")
            fake.creatives[7] = {"id": 7, "name": "a", "upload_user_id": 653, "confirmed": True, "materials": []}
            self.assertRaisesRegex(Blocked, "creatives of these names: a", run_upload, online(fake), file, deps)
            del fake.creatives[7]
            self.assertRaisesRegex(Blocked, "is not the plan's", run_upload, online(fake, {"id": 2, "name": "别人"}), file, deps)
            self.assertEqual(len([r for r in fake.requests if r["method"] != "GET"]), 0)
        finally:
            fake.close()

    def test_client_limits(self) -> None:
        """The client: no delete or other path, puts only to links CLS gave, and refuses links that do not lead to object storage."""
        fake = FakeCreative()
        try:
            api = CreativeAdapter(fake.origin, "Bearer good", upload_host=LOCAL, allow_http_upload=True)
            self.assertRaisesRegex(ClsError, "no upload link for this file", api.put, "a/a.mp4", __file__, 1)
            fake.sign_elsewhere = True
            self.assertRaisesRegex(ClsError, r"is not object storage \(evil\.example\.com\)", api.sign, 94, ["a/a.mp4"])
            self.assertFalse(any(hasattr(api, name) for name in ("delete", "publish", "confirm")))
            production = CreativeAdapter(fake.origin, "Bearer good")
            fake.sign_elsewhere = False
            self.assertRaisesRegex(ClsError, "is not object storage", production.sign, 94, ["a/a.mp4"])
        finally:
            fake.close()

    def test_no_permission(self) -> None:
        """An account without the upload permission gets no plan."""
        fake = FakeCreative(can_upload=False)
        try:
            self.assertRaisesRegex(Blocked, "may not upload creatives", approved_plan, fake, deps_for(), {"files": [materials({"a.mp4": "x"})], "creator": "张天伟", "producers": ["丁杰"]})
        finally:
            fake.close()

    def test_zipped_folder(self) -> None:
        """A zipped folder (how a chat hands over a folder) is planned and uploaded by the files inside it."""
        fake = FakeCreative()
        try:
            deps = deps_for()
            zip_ = write_file(os.path.join(temp_dir("zip-input-"), "新建文件夹.zip"),
                              make_zip([{"name": "新建文件夹/P01.mp4", "data": "video"}, {"name": "新建文件夹/封面.png", "data": "png"}, {"name": "新建文件夹/说明.txt", "data": "t"}]))
            directory = temp_dir("upload-plan-")
            write_file(os.path.join(directory, "request.json"), json.dumps({"files": [zip_], "progress": False, "creator": "张天伟", "producers": ["丁杰"]}, ensure_ascii=False))
            summary = plan_upload(online(fake), None, os.path.join(directory, "request.json"), os.path.join(directory, "plan.json"), deps)
            self.assertEqual(sorted([i["name"], i["type"]] for i in summary["items"]), [["P01", "视频"], ["封面", "图片"]])
            self.assertEqual(summary["skipped"], [{"file": "新建文件夹.zip：新建文件夹/说明.txt", "reason": "CLS does not take this format (.txt)"}])
            result = run_upload(online(fake), summary["plan"], deps)
            self.assertEqual(result["status"], "uploaded", result)
            self.assertEqual(sorted(next(r for r in fake.requests if r["path"] == "/api/qcloud/signed-url")["body"]["files"]), ["P01/P01.mp4", "封面/封面.png"])
        finally:
            fake.close()

    def test_final_cut_end_to_end(self) -> None:
        """The 终稿库 zip end to end: planned from the progress sheet, uploaded under the 素材名称, the files on disk untouched."""
        fake = FakeCreative()
        try:
            deps = deps_for()
            zip_ = write_file(os.path.join(temp_dir("zip-input-"), "玩法AI迭代素材.zip"), make_zip([{"name": n, "data": d} for n, d in FINAL_CUT_FILES.items()]))
            with open(zip_, "rb") as handle:
                before = handle.read()
            file = approved_plan(fake, deps, {"files": [zip_], "progress": True, "producers": ["长沙廿拾一文化传媒有限公司"], "items": FINAL_CUT_ITEMS}, SheetBridge(final_cut_sheet()))
            result = run_upload(online(fake), file, deps)
            self.assertEqual(result["status"], "uploaded", result)
            signed = next(r for r in fake.requests if r["path"] == "/api/qcloud/signed-url")["body"]["files"]
            self.assertEqual(sorted(signed), [
                "GOFCN67680_AI生成_设定3.0_3D_企鹅保龄球/GOFCN67680_AI生成_设定3.0_3D_企鹅保龄球.mp4", "GOFCN67681_AI生成_设定3.0_3D_冻鱼飞盘/GOFCN67681_AI生成_设定3.0_3D_冻鱼飞盘.mp4",
                "GOFCN67682_AI生成_设定3.0_3D_围炉煮冰/GOFCN67682_AI生成_设定3.0_3D_围炉煮冰.mp4", "GOFCN67683_AI生成_设定3.0_3D_围炉煮冰/GOFCN67683_AI生成_设定3.0_3D_围炉煮冰.mp4"])
            self.assertTrue(all(re.search(r"/GOFCN6768[0-3]_AI生成_设定3\.0_3D_.+\.mp4$", key) for key in fake.objects), "the stored files carry the new names")
            self.assertEqual(sorted(next(r for r in fake.requests if r["path"] == "/api/creative/multiple-upload")["body"]["names"]), sorted(k.split("/")[0] for k in signed))
            self.assertEqual({u["file"]: u["name"] for u in result["uploaded"]}, {
                "g56890_a_v15.mp4": "GOFCN67680_AI生成_设定3.0_3D_企鹅保龄球", "g56890_c_v21.mp4": "GOFCN67681_AI生成_设定3.0_3D_冻鱼飞盘",
                "g44065_v90.mp4": "GOFCN67682_AI生成_设定3.0_3D_围炉煮冰", "g44065_gf_v90.mp4": "GOFCN67683_AI生成_设定3.0_3D_围炉煮冰"})
            # The 国风配乐 version is a creative of its own: the same name as the V90 but for its number, and its own file.
            guofeng = next(c for c in fake.creatives.values() if c["name"] == "GOFCN67683_AI生成_设定3.0_3D_围炉煮冰")
            self.assertEqual(guofeng["materials"][0]["file_hash"], hash_file(os.path.join(materials({"x": "guofeng"}), "x"))["md5"])
            self.assertTrue(all(u["creator"] == "金莉" and u["producers"][0] == "长沙廿拾一文化传媒有限公司" for u in result["uploaded"]))
            with open(zip_, "rb") as handle:
                self.assertEqual(handle.read(), before, "the zip the user sent is not changed")
        finally:
            fake.close()

    def test_same_content(self) -> None:
        """Two files of the same content are one creative: creative files says which, and the plan refuses the second copy."""
        fake = FakeCreative()
        try:
            deps = deps_for()
            directory = materials({"01.mp4": "same bytes", "01 - 副本.mp4": "same bytes", "02.mp4": "other bytes"})
            request = {"files": [directory], "creator": "张天伟", "producers": ["丁杰"]}
            with self.assertRaises(Blocked) as caught:
                approved_plan(fake, deps, request)
            self.assertTrue("the same content as" in caught.exception.message and "01 - 副本.mp4" in caught.exception.message and "01.mp4" in caught.exception.message)
            file = approved_plan(fake, deps, {**request, "items": [{"file": "01 - 副本.mp4", "skip": True}]})
            result = run_upload(online(fake), file, deps)
            self.assertEqual(result["status"], "uploaded", result)
            self.assertEqual(sorted(u["name"] for u in result["uploaded"]), ["01", "02"])
        finally:
            fake.close()

    def test_credential_like_names(self) -> None:
        """A material name that reads like a credential field (Cookie, Secret) is only a name: the run records it and goes on."""
        for mode in ("normal", "lose-answer"):
            fake = FakeCreative()
            try:
                deps = deps_for()
                fake.create_mode = mode
                file = approved_plan(fake, deps, {"files": [materials({"GOFCN67680_AI生成_设定3.0_Cookie大作战.mp4": "c", "Secret_token_竖版.mp4": "s"})], "creator": "张天伟", "producers": ["丁杰"]})
                result = run_upload(online(fake), file, deps)
                self.assertEqual(result["status"], "uploaded", f"{mode}: {result}")
                steps = [e["step"] for e in upload_events(result["batch"])]
                self.assertTrue(all(s in steps for s in ("signed", "create_attempt", "created" if mode == "normal" else "reconciled")), steps)
            finally:
                fake.close()

    def test_left_out_of_answer(self) -> None:
        """A name the create answer leaves out of both lists is looked for, found and annotated, and nothing is created twice."""
        fake = FakeCreative()
        try:
            # CLS makes both creatives, but its answer lists only the first.
            def trimming(url: str, **kwargs: Any) -> Response:
                response = real_fetch(url, **kwargs)
                if "/api/creative/multiple-upload" not in url:
                    return response
                body = response.json()
                body["data"]["succ"] = body["data"]["succ"][:1]
                return Response(response.status, {"Content-Type": "application/json"}, json.dumps(body).encode())

            deps = deps_for(fetch=trimming)
            file = approved_plan(fake, deps, {"files": [materials({"a.mp4": "aa", "b.mp4": "bb"})], "creator": "张天伟", "producers": ["丁杰"]})
            result = run_upload(online(fake), file, deps)
            self.assertEqual(result["status"], "uploaded", result)
            self.assertEqual(next(c for c in fake.creatives.values() if c["name"] == "b")["user_id"], 653, "annotated")
            self.assertEqual(writes(fake, "multiple-upload"), 1)
        finally:
            fake.close()

    def test_name_behind_pages(self) -> None:
        """A name CLS already has is found however many creatives contain it, and is in one refusal with everything else unsettled."""
        # An old creative named exactly 封面, behind more than a page of newer ones whose names contain it.
        existing = [{"id": 10, "name": "封面", "upload_user_id": 653, "confirmed": True, "materials": []},
                    *[{"id": 1000 + i, "name": f"GOFCN{60000 + i}_封面_{i}", "upload_user_id": 653, "confirmed": True, "materials": []} for i in range(60)]]
        fake = FakeCreative(existing=existing)
        try:
            deps = deps_for()
            directory = materials({"封面.png": "p", "b.mp4": "b"})
            self.assertRaisesRegex(Blocked, "CLS has a creative of this name already.*: 封面", approved_plan, fake, deps, {"files": [directory], "creator": "张天伟", "producers": ["丁杰"]})
            # No 创意人 as well: both in the same refusal, not one after the other.
            with self.assertRaises(Blocked) as caught:
                approved_plan(fake, deps, {"files": [directory], "producers": ["丁杰"]})
            self.assertTrue("创意人 not decided" in caught.exception.message and "CLS has a creative of this name already" in caught.exception.message)
        finally:
            fake.close()

    def test_login_runs_out(self) -> None:
        """A login that runs out during the uploads is taken again before the create; with CLS out of reach then, nothing is created and a later run does it."""
        fake = FakeCreative()
        try:
            base = deps_for()
            file = approved_plan(fake, base, {"files": [materials({"a.mp4": "aa"})], "creator": "张天伟", "producers": ["丁杰"]})
            # The token the run starts with stops being taken once the files are up; the browser has a fresh one by then.
            fake.tokens.add("Bearer fresh")
            asked = [0]

            def session() -> dict:
                asked[0] += 1
                return {"authorization": "Bearer fresh" if asked[0] > 1 else "Bearer good", "account": ACCOUNT, "origin": fake.origin}

            def expiring_fetch(url: str, **kwargs: Any) -> Response:
                if kwargs.get("method") == "PUT":
                    fake.tokens.discard("Bearer good")
                return real_fetch(url, **kwargs)

            result = run_upload(ScriptedOnline(session=session), file, deps_for(fetch=expiring_fetch).__class__(
                adapter={**base.adapter, "fetch": expiring_fetch}, batch_root=base.batch_root, tmp_dir=base.tmp_dir, sleep=base.sleep, reconcile_tries=1))
            self.assertEqual(result["status"], "uploaded", result)
            self.assertEqual(asked[0], 2, "the browser was asked once more, right before the create")
            self.assertEqual(next(r for r in fake.requests if r["path"] == "/api/creative/multiple-upload")["authorization"], "Bearer fresh")
        finally:
            fake.close()
        other = FakeCreative()
        try:
            base = deps_for()
            file = approved_plan(other, base, {"files": [materials({"b.mp4": "bb"})], "creator": "张天伟", "producers": ["丁杰"]})
            state = {"outage": True, "put": False}

            def flaky_fetch(url: str, **kwargs: Any) -> Response:
                if kwargs.get("method") == "PUT":
                    state["put"] = True
                if state["outage"] and state["put"] and url.endswith("/api/userinfo"):
                    raise FetchError("fetch failed")
                return real_fetch(url, **kwargs)

            flaky = UploadDeps(adapter={**base.adapter, "fetch": flaky_fetch}, batch_root=base.batch_root, tmp_dir=base.tmp_dir, sleep=base.sleep, reconcile_tries=1)
            self.assertRaisesRegex(Blocked, "CLS cannot be reached for now and no creative was created", run_upload, online(other), file, flaky)
            self.assertEqual([writes(other, "multiple-upload"), upload_status(file, flaky)["create_sent"]], [0, False])
            state["outage"] = False
            self.assertEqual(run_upload(online(other), file, flaky)["status"], "uploaded")
            self.assertEqual(writes(other, "multiple-upload"), 1)
        finally:
            other.close()


if __name__ == "__main__":
    unittest.main()
