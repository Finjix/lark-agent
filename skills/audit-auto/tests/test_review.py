import json
import os
import re
import unittest

from audit_sdk.cli import Context, main
from audit_sdk.config import skill_root
from audit_sdk.errors import Blocked
from audit_sdk.projects import PLACEHOLDER, PROJECTS
from audit_sdk.review import ReviewContext, normalize, render_file, review, review_system, verdict_of
from audit_sdk.rules import save_book

from tests.helpers.fakes import FakeModel, text_of
from tests.helpers.mp4 import make_mp4
from tests.helpers.tmp import temp_dir
from tests.helpers.zip_writer import ZipFile, make_zip
from tests.test_transcript import utc

ROOT = skill_root({})
ANSWER = {
    "summary": "真人口播，演员喊玩无尽冬日。", "kind": "真人实拍",
    "lines": [{"time": "00:06", "source": "台词", "text": "三分钟上头"}],
    "issues": [
        {"level": "要改", "time": "00:06–00:07", "where": "台词", "problem": "「三分钟上头」，上头是防沉迷禁用词", "rule": "字幕和口播 · 防沉迷", "fix": "换成「三分钟就会」"},
        {"level": "红线", "time": "00:00–00:03", "where": "画面中下（皮带扣）", "problem": "露出古驰双 G 标志", "rule": "红线 · 侵权", "fix": "打码"},
        {"level": "需确认", "time": "全片", "where": "全片画面", "problem": "明星林子聪出镜", "rule": "明星素材", "fix": "投前确认授权"},
        {"level": "要改", "time": "全片", "where": "文件名", "problem": "产出方不对", "rule": "命名", "fix": "改成 DR"},
        {"level": "要改", "time": "00:01", "where": "", "problem": "", "rule": "", "fix": ""},
    ],
}


def video(directory: str, name: str, seconds: float = 11) -> str:
    at = os.path.join(directory, name)
    with open(at, "wb") as handle:
        handle.write(make_mp4(seconds, 1080, 1920))
    return at


def context(model: FakeModel, shared: str | None = None, root: str = ROOT) -> ReviewContext:
    return ReviewContext(model=model, root=root, shared=shared or temp_dir("shared-"), scratch=temp_dir("scratch-"), log=lambda _l: None)


class ReviewTest(unittest.TestCase):
    def test_the_answer_is_tidied_empty_problems_dropped_a_file_name_has_no_time_then_by_time(self) -> None:
        findings = normalize({**ANSWER, "issues": [*ANSWER["issues"], {"level": "严重", "time": "00:09", "where": "字幕", "problem": "x", "rule": "", "fix": ""}]})
        self.assertEqual([(i["level"], i["time"], i["where"]) for i in findings["issues"]], [
            ("需确认", "全片", "全片画面"), ("要改", "", "文件名"), ("红线", "00:00–00:03", "画面中下（皮带扣）"), ("要改", "00:06–00:07", "台词"), ("要改", "00:09", "字幕"),
        ])
        self.assertEqual(normalize(None), {"summary": "", "kind": "", "lines": [], "issues": []})
        same = normalize({"issues": [{"level": "要改", "time": "00:02–00:02、00:12-00:13", "where": "台词", "problem": "x", "rule": "", "fix": ""}]})
        self.assertEqual(same["issues"][0]["time"], "00:02、00:12-00:13")
        self.assertEqual(verdict_of(findings["issues"]), "改完再送审")
        self.assertEqual(verdict_of([i for i in findings["issues"] if i["level"] == "需确认"]), "确认后再送审")
        self.assertEqual(verdict_of([]), "可以送审")

    def test_a_reviewed_file_reads_as_a_table_of_level_place_problem_fix_in_time_order(self) -> None:
        findings = normalize(ANSWER)
        text = render_file({"name": "GOFCN1.mp4", "status": "reviewed", "media": "video", "duration": 10.77, "orientation": "竖版", "findings": findings,
                            "verdict": verdict_of(findings["issues"])})
        self.assertEqual(text, "\n".join([
            "**GOFCN1.mp4**（竖版 · 11 秒 · 真人实拍）",
            "真人口播，演员喊玩无尽冬日。",
            "结论：改完再送审（红线 1 · 要改 2 · 需确认 1）",
            "",
            "| 级别 | 位置 | 问题（原文） | 怎么改 |",
            "|---|---|---|---|",
            "| 需确认 | 全片画面 | 明星林子聪出镜〔明星素材〕 | 投前确认授权 |",
            "| 要改 | 文件名 | 产出方不对〔命名〕 | 改成 DR |",
            "| 红线 | 00:00–00:03 画面中下（皮带扣） | 露出古驰双 G 标志〔红线 · 侵权〕 | 打码 |",
            "| 要改 | 00:06–00:07 台词 | 「三分钟上头」，上头是防沉迷禁用词〔字幕和口播 · 防沉迷〕 | 换成「三分钟就会」 |",
        ]))
        clean = render_file({"name": "海报.png", "status": "reviewed", "media": "image", "findings": {"summary": "", "kind": "其他", "lines": [], "issues": []},
                             "verdict": "可以送审"})
        self.assertEqual(clean, "**海报.png**（图片）\n结论：可以送审，没有发现问题")
        piped = normalize({"issues": [{"level": "要改", "time": "00:01", "where": "字幕", "problem": "「A|B」", "rule": "", "fix": ""}]})
        self.assertTrue(render_file({"name": "b.mp4", "status": "reviewed", "media": "video", "findings": piped, "verdict": "改完再送审"})
                        .endswith("\n| 要改 | 00:01 字幕 | 「A\\|B」 | — |"))
        self.assertEqual(render_file({"name": "a.mp4", "status": "failed", "reason": "视频太大"}), "**a.mp4**\n没审成：视频太大")
        self.assertEqual(render_file({"name": "c.mp4", "status": "reviewed", "media": "video", "duration": 2.25, "findings": piped, "verdict": "改完再送审"})
                         .split("\n")[0], "**c.mp4**（2.3 秒）", "rounded as toFixed does")

    def test_the_system_instruction_carries_the_fixed_and_the_dynamic_spec_the_dynamic_one_said_to_win(self) -> None:
        system = review_system(PROJECTS["GOF"], "# 固定\n- 不写上头", "- D1〔字幕和口播〕「赚米」不能用")
        self.assertIn("GOF（无尽冬日）", system)
        self.assertIn("## Fixed spec\n# 固定\n- 不写上头", system)
        self.assertIn("## Dynamic spec\n- D1〔字幕和口播〕「赚米」不能用", system)
        self.assertIn("the dynamic rule wins", system)
        self.assertIn("Chinese ad platforms", system)
        self.assertIn("the section of the fixed spec whose title says 红线", system)
        got = review_system(PROJECTS["GOT"], "", "")
        self.assertRegex(got, r"no fixed spec yet[\s\S]*No dynamic rules yet")
        self.assertIn("not settled yet", got)
        self.assertNotIn("Chinese ad platforms", got, "GOT material is not said to go to Chinese platforms")

    def test_a_gof_review_sends_each_video_inline_with_the_gof_spec_at_the_frame_rate_its_length_calls_for(self) -> None:
        directory = temp_dir("input-")
        with open(os.path.join(directory, "说明.txt"), "w", encoding="utf-8") as handle:
            handle.write("x")
        model = FakeModel(lambda request, _n: Blocked("AI 网关暂时不可用（HTTP 503），稍后再试") if "坏的" in text_of(request) else ANSWER)
        outcome = review(PROJECTS["GOF"], [video(directory, "GOFCN1_竖版.mp4"), video(directory, "坏的.mp4", 100), os.path.join(directory, "说明.txt"),
                                           os.path.join(directory, "missing.mp4")], "这是真人实拍", context(model))
        self.assertEqual(len(model.calls), 2)
        # Two at a time: the calls come in whichever order the files are ready.
        first = next(c for c in model.calls if "GOFCN1" in text_of(c))
        long = next(c for c in model.calls if "坏的" in text_of(c))
        self.assertIn("广告创意仅供参考", first.system, "the fixed spec of GOF")
        self.assertEqual(first.parts[0]["inlineData"]["mimeType"], "video/mp4")
        self.assertEqual(first.parts[0]["videoMetadata"]["fps"], 4)
        self.assertEqual(long.parts[0]["videoMetadata"]["fps"], 1)
        self.assertEqual(first.media_resolution, "MEDIA_RESOLUTION_HIGH")
        self.assertRegex(text_of(first), r"File name: GOFCN1_竖版\.mp4[\s\S]*Video: 11 s, 竖版[\s\S]*Notes from the user: 这是真人实拍")
        self.assertEqual([r["status"] for r in outcome.results], ["reviewed", "failed"])
        self.assertIn("**GOFCN1_竖版.mp4**（竖版 · 11 秒 · 真人实拍）", outcome.markdown)
        self.assertIn("**坏的.mp4**\n没审成：AI 网关暂时不可用", outcome.markdown)
        self.assertIn("没有审的文件：\n- 说明.txt：不是视频或图片，没有审\n- missing.mp4：找不到这个文件", outcome.markdown)
        self.assertEqual(outcome.rules, {"fixed": True, "dynamic": 0})

    def test_got_is_reviewed_against_its_fixed_spec_and_its_dynamic_spec(self) -> None:
        directory = temp_dir("input-")
        shared = temp_dir("shared-")
        model = FakeModel(lambda _r, _n: ANSWER)
        plain = review(PROJECTS["GOT"], [video(directory, "got.mp4")], None, context(model, shared))
        self.assertEqual(plain.results[0]["status"], "reviewed")
        self.assertIn("## Fixed spec\n# GOT 广告素材审片规范", model.calls[0].system)
        self.assertIn("No dynamic rules yet", model.calls[0].system)
        self.assertEqual(plain.rules, {"fixed": True, "dynamic": 0})
        at = utc("2026-10-30T06:00:00Z")
        save_book(shared, {"version": 1, "project": "GOT", "next": 2, "removed": [], "updated": 1, "rules": [
            {"id": "D1", "category": "字幕和口播", "rule": "「通关」不能写成「通guan」", "fix": "写汉字", "sources": [{"chat": "oc_1", "message": "om_1", "at": at}],
             "created": 1, "updated": at}]})
        outcome = review(PROJECTS["GOT"], [video(directory, "got.mp4")], None, context(model, shared))
        self.assertIn("- D1〔字幕和口播〕「通关」不能写成「通guan」 怎么改：写汉字（2026-10-30）", model.calls[1].system)
        self.assertEqual(outcome.rules, {"fixed": True, "dynamic": 1})

    def test_a_placeholder_fixed_spec_uses_the_dynamic_spec_alone_and_with_none_the_review_is_refused(self) -> None:
        placeholder = temp_dir("skill-")
        os.makedirs(os.path.join(placeholder, "references"))
        with open(os.path.join(placeholder, "references", "got-rules.md"), "w", encoding="utf-8") as handle:
            handle.write("\n".join([PLACEHOLDER, "<!-- 还没定 -->", "", "# GOT 广告素材审片规范", "", "还没有固定规范。", ""]))
        directory = temp_dir("input-")
        shared = temp_dir("shared-")
        model = FakeModel(lambda _r, _n: ANSWER)
        with self.assertRaisesRegex(Blocked, "监听本群更新审片规范"):
            review(PROJECTS["GOT"], [video(directory, "got.mp4")], None, context(model, shared, placeholder))
        self.assertEqual(len(model.calls), 0)
        save_book(shared, {"version": 1, "project": "GOT", "next": 2, "removed": [], "updated": 1, "rules": [
            {"id": "D1", "category": "字幕和口播", "rule": "「通关」不能写成「通guan」", "sources": [{"chat": "oc_1", "message": "om_1", "at": 1}], "created": 1, "updated": 1}]})
        outcome = review(PROJECTS["GOT"], [video(directory, "got.mp4")], None, context(model, shared, placeholder))
        self.assertEqual(outcome.results[0]["status"], "reviewed")
        self.assertIn("no fixed spec yet", model.calls[0].system)
        self.assertNotIn("还没有固定规范", model.calls[0].system)
        self.assertEqual(outcome.rules, {"fixed": False, "dynamic": 1})

    def test_a_file_sent_again_saved_as_name_2_is_reviewed_under_the_name_it_was_sent_with(self) -> None:
        model = FakeModel(lambda _r, _n: ANSWER)
        outcome = review(PROJECTS["GOF"], [video(temp_dir("input-"), "GOFCN1_竖版 (2).mp4")], None, context(model))
        self.assertIn("File name: GOFCN1_竖版.mp4\n", text_of(model.calls[0]))
        self.assertEqual(outcome.results[0]["name"], "GOFCN1_竖版.mp4")

    def test_a_zip_is_unpacked_into_the_scratch_folder_and_each_material_reviewed_under_its_path(self) -> None:
        directory = temp_dir("input-")
        archive = os.path.join(directory, "素材.zip")
        with open(archive, "wb") as handle:
            handle.write(make_zip([
                ZipFile("横版/GOFCN2.mp4", make_mp4(15, 1920, 1080)),
                ZipFile("竖版/GOFCN2.mp4", make_mp4(15, 1080, 1920)),
                ZipFile("脚本.docx", "x"),
                ZipFile("__MACOSX/._GOFCN2.mp4", "x"),
            ]))
        model = FakeModel(lambda _r, _n: {"summary": "", "kind": "", "lines": [], "issues": []})
        ctx = context(model)
        outcome = review(PROJECTS["GOF"], [archive], None, ctx)
        self.assertEqual([(r["name"], r["orientation"]) for r in outcome.results], [("素材.zip › 横版/GOFCN2.mp4", "横版"), ("素材.zip › 竖版/GOFCN2.mp4", "竖版")])
        self.assertEqual(outcome.skipped, [{"file": "素材.zip › 脚本.docx", "reason": "不是视频或图片，没有审"}])
        self.assertTrue(os.listdir(ctx.scratch), "unpacked in the scratch folder")
        self.assertEqual(len(os.listdir(directory)), 1, "nothing written next to the user's files")

    def test_the_command_line_review_prints_the_markdown_and_keeps_the_details_outside_the_workspace(self) -> None:
        user = temp_dir("user-")
        workspace = os.path.join(user, "workspace")
        os.makedirs(os.path.join(workspace, "input"))
        clip = video(os.path.join(workspace, "input"), "GOFCN3.mp4")
        env = {"LARK_AGENT_USER_DIR": user, "LARK_AGENT_SHARED_DIR": temp_dir("shared-")}
        model = FakeModel(lambda _r, _n: ANSWER)
        lines: list[str] = []
        result, code = main(["review", "--project", "gof", "--file", clip], Context(env=env, model=model, ffmpeg=None, log=lines.append))
        self.assertEqual(code, 0)
        self.assertEqual(result["status"], "done")
        self.assertEqual(list(result)[:2], ["status", "result_file"], "what finds the whole result goes first")
        self.assertEqual(list(result)[-1], "markdown", "the Markdown last")
        self.assertTrue(result["markdown"].startswith("**GOFCN3.mp4**"))
        self.assertEqual(result["files"], [{"name": "GOFCN3.mp4", "status": "reviewed", "verdict": "改完再送审", "issues": 4}])
        kept = result["result_file"]
        self.assertTrue(kept.startswith(os.path.join(user, "audit-auto", "reviews")), kept)
        with open(kept, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), result["markdown"])
        with open(re.sub(r"\.md$", ".json", kept), encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["files"][0]["findings"]["lines"][0]["text"], "三分钟上头")
        self.assertEqual(os.listdir(workspace), ["input"], "nothing new in the workspace: the host would send it to the user")
        self.assertEqual(os.listdir(os.path.join(user, "audit-auto", "tmp")), [], "the scratch folder of the run is gone")
        self.assertRegex("\n".join(lines), r"审片 1/1：GOFCN3\.mp4[\s\S]*完成 1/1：GOFCN3\.mp4，4 处问题")
        # A note that starts with a dash is still the note, not an option.
        dashed, code = main(["review", "--project", "GOF", "--file", clip, "--note", "-B 面从 5 秒开始"], Context(env=env, model=model, ffmpeg=None, log=lambda _l: None))
        self.assertEqual(code, 0, dashed.get("reason"))
        self.assertIn("Notes from the user: -B 面从 5 秒开始", text_of(model.calls[-1]))

        for args, reason in [
            (["review", "--file", clip], "缺少 --project"),
            (["review", "--project", "GOX", "--file", clip], "只有 GOF 和 GOT"),
            (["review", "--project", "GOF"], "缺少 --file"),
            (["review", "--project", "GOF", "--file", clip, "--bogus"], "bogus"),
            (["nonsense"], "用法"),
        ]:
            refused, code = main(args, Context(env=env, model=model, ffmpeg=None, log=lambda _l: None))
            self.assertEqual(code, 2, " ".join(args))
            self.assertEqual(refused["status"], "blocked")
            self.assertIn(reason, refused["reason"])
        none, code = main(["review", "--project", "GOF", "--file", os.path.join(user, "nope.mp4")], Context(env=env, model=model, ffmpeg=None, log=lambda _l: None))
        self.assertEqual(code, 2)
        self.assertEqual(none["status"], "failed")
        self.assertIn("nope.mp4：找不到这个文件", none["markdown"])


if __name__ == "__main__":
    unittest.main()
