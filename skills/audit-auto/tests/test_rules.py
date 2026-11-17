import json
import os
import unittest

from audit_sdk.cli import Context, main
from audit_sdk.config import Turn, skill_root
from audit_sdk.errors import Blocked
from audit_sdk.jsutil import now_ms
from audit_sdk.lark import LarkError, Picture
from audit_sdk.projects import PROJECTS
from audit_sdk.rules import RulesContext, apply, category_of, load_book, operations_of, render_book, sync_all, watch, with_lock

from tests.helpers.fakes import FakeLark, FakeModel, message, text_of
from tests.helpers.tmp import temp_dir
from tests.test_transcript import utc

DAY = 86_400_000
NOW = utc("2026-11-02T08:00:00Z")
ROOT = skill_root({})
GROUP = Turn(chat="oc_review", chat_type="group", user="ou_alice")
REFS = {1: {"chat": "oc_review", "message": "om_a", "at": NOW - DAY, "by": "王审核"}, 2: {"chat": "oc_review", "message": "om_b", "at": NOW, "by": "李剪辑"}}


def empty_book(project: str = "GOF") -> dict:
    return {"version": 1, "project": project, "next": 1, "rules": [], "removed": [], "updated": 0}


def setup() -> tuple[str, FakeLark]:
    # As the host lays it out: <AGENT_SHARED_DIR>/audit-auto.
    shared = os.path.join(temp_dir("host-"), "audit-auto")
    lark = FakeLark()
    lark.messages += [
        message(NOW - 400 * DAY, {"text": "很久以前：BOSS 可以写"}),
        message(NOW - 3 * DAY, {"text": "这条被巨量拒了，理由是「上头」算防沉迷"}),
        message(NOW - 3 * DAY + 60_000, {"text": "以后「上头」都别写了"}, sender="ou_bob"),
        message(NOW - 3 * DAY + 90_000, {"text": "审片结果：……"}, sender="cli_bot", type="interactive"),
        message(NOW - 2 * DAY, {"image_key": "img_reject"}, type="image"),
    ]
    lark.pictures["img_reject"] = Picture(data=b"png", mime="image/png")
    return shared, lark


def ctx_of(lark: FakeLark, model: FakeModel, shared: str, **extra) -> RulesContext:
    return RulesContext(lark=lark, model=model, root=ROOT, shared=shared, log=lambda _l: None, **extra)


def read_json(path: str):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


class RulesTest(unittest.TestCase):
    def test_operations_change_the_spec_only_when_they_name_a_message_of_the_chunk_and_an_existing_rule(self) -> None:
        book = empty_book()
        first = apply(book, [
            {"op": "add", "category": "字幕和口播", "rule": "「上头」不能用", "fix": "换成「就会」", "messages": [1]},
            {"op": "add", "category": "乱写", "rule": "不出现日本元素的樱花", "messages": [2]},
            {"op": "add", "rule": "没有依据的规则", "messages": [9]},
            {"op": "add", "messages": [1]},
            {"op": "update", "id": "D9", "rule": "不存在", "messages": [1]},
        ], REFS, NOW)
        self.assertEqual(first, {"added": ["D1", "D2"], "updated": [], "removed": []})
        self.assertEqual(book["rules"][1]["category"], "其他", "an unknown category")
        self.assertEqual(book["updated"], NOW)
        # The same rule again (punctuation aside) is the same rule: it gains the message, not a twin.
        again = apply(book, [{"op": "add", "rule": "“上头”不能用。", "messages": [2]}, {"op": "update", "id": "D2", "example": "樱花树背景被拒", "messages": [2]}], REFS, NOW + 1)
        self.assertEqual(again, {"added": [], "updated": ["D1", "D2"], "removed": []})
        self.assertEqual([s["message"] for s in book["rules"][0]["sources"]], ["om_a", "om_b"])
        self.assertEqual(book["rules"][1]["example"], "樱花树背景被拒")
        removed = apply(book, [{"op": "remove", "id": "D2", "reason": "群里说可以了", "messages": [2]}], REFS, NOW + 2)
        self.assertEqual(removed["removed"], ["D2"])
        self.assertEqual([r["id"] for r in book["rules"]], ["D1"])
        self.assertEqual((book["removed"][0]["id"], book["removed"][0]["reason"], book["removed"][0]["by"]), ("D2", "群里说可以了", "chat"))
        self.assertEqual(book["next"], 3, "ids are never reused")
        md = render_book(book)
        self.assertTrue(md.startswith("# GOF 动态审片规范"))
        self.assertIn("## 字幕和口播\n\n**D1** 「上头」不能用\n- 怎么改：换成「就会」\n- 依据：2026-11-01 王审核、2026-11-02 李剪辑", md)

    def test_the_models_operations_are_read_defensively_in_either_shape(self) -> None:
        self.assertEqual(operations_of({"operations": [
            {"op": "add", "rule": "  多 空 格  ", "messages": [1, "2", "x"]}, {"op": "drop", "messages": [1]}, None, {"op": "remove", "id": " d3 ", "messages": []},
        ]}), [{"op": "add", "rule": "多 空 格", "messages": [1, 2]}, {"op": "remove", "id": "D3", "messages": []}])
        self.assertEqual(operations_of([{"op": "add", "rule": "裸数组也行", "messages": [1]}]), [{"op": "add", "rule": "裸数组也行", "messages": [1]}])
        self.assertEqual(operations_of("nonsense"), [])
        self.assertEqual(operations_of([{"op": "add", "rule": "r", "messages": ["#3", "3-1", 4.0, True, None]}])[0]["messages"], [3, 3, 4])

    def test_an_id_the_answer_gives_a_rule_it_adds_stands_for_its_real_id_further_down(self) -> None:
        book = empty_book()
        changes = apply(book, operations_of([
            {"op": "add", "id": "logo_end_card", "category": "成片质量", "rule": "结尾加 3 秒 logo 定版", "messages": [1]},
            {"op": "update", "id": "logo_end_card", "rule": "结尾加 2 秒 logo 定版", "messages": [2]},
            {"op": "add", "id": "D1", "category": "真人实拍", "rule": "女演员不穿吊带", "messages": [2]},
        ]), REFS, NOW)
        self.assertEqual(changes, {"added": ["D1", "D2"], "updated": [], "removed": []})
        self.assertEqual([(r["id"], r["category"], r["rule"]) for r in book["rules"]],
                         [("D1", "成片质量", "结尾加 2 秒 logo 定版"), ("D2", "按素材类型", "女演员不穿吊带")])
        self.assertEqual([category_of("红线 · 侵权"), category_of("3D 玩法"), category_of("字幕"), category_of(None), category_of("别的")],
                         ["红线", "按素材类型", "字幕和口播", "其他", "其他"])

    def test_listening_to_a_group_refused_outside_a_group_the_last_year_read_rulings_become_rules(self) -> None:
        shared, lark = setup()
        model = FakeModel(lambda _r, _n: {"operations": [{"op": "add", "category": "字幕和口播", "rule": "「上头」算防沉迷，字幕口播都不能写", "fix": "换个说法", "messages": [1, 2]}]})
        ctx = ctx_of(lark, model, shared, now=lambda: NOW)
        with self.assertRaisesRegex(Blocked, "private chat"):
            watch(PROJECTS["GOF"], None, Turn(chat="oc_p2p", chat_type="p2p"), ctx)
        with self.assertRaisesRegex(Blocked, "No chat to listen to: pass --chat"):
            watch(PROJECTS["GOF"], None, Turn(), ctx)

        result = watch(PROJECTS["GOF"], None, GROUP, ctx)
        self.assertEqual(result, {"status": "watching", "project": "GOF", "read": 4, "considered": 3, "added": 1, "updated": 0, "removed": 0, "pending": False,
                                  "already": False, "since": "2025-11-02", "rules": 1})
        self.assertEqual(lark.asked[0]["start"], (NOW - 365 * DAY) // 1000)
        self.assertEqual(len(model.calls), 1)
        call = model.calls[0]
        transcript = text_of(call)
        self.assertIn("#1 [2026-10-30 16:00] 王审核: 这条被巨量拒了，理由是「上头」算防沉迷\n#2 [2026-10-30 16:01] 李剪辑: 以后「上头」都别写了\n#3 [2026-10-31 16:00] 王审核:  [picture #3-1]", transcript)
        self.assertNotRegex(transcript, "很久以前|审片结果", "older than the start, and the bot's own message, are not read")
        self.assertEqual(call.parts[-1]["inlineData"]["mimeType"], "image/png", "the picture goes with the transcript")
        self.assertIn("Today is 2026-11-02", call.system)
        self.assertIn("# Fixed spec\n# GOF (无尽冬日) ad material review spec", call.system)
        self.assertRegex(call.system, r"# Dynamic spec now\nEach rule .*\n\(empty\)")

        book = load_book(shared, "GOF")
        self.assertEqual([(r["id"], r["rule"], [s["by"] for s in r["sources"]]) for r in book["rules"]], [("D1", "「上头」算防沉迷，字幕口播都不能写", ["王审核", "李剪辑"])])
        with open(os.path.join(shared, "rules", "GOF.md"), encoding="utf-8") as handle:
            self.assertIn("**D1** 「上头」算防沉迷", handle.read())
        saved = read_json(os.path.join(shared, "groups.json"))["groups"][0]
        self.assertEqual(saved["cursor"], NOW - 2 * DAY, "read up to the newest message")
        self.assertEqual(saved["watchedBy"], "ou_alice")
        self.assertEqual(read_json(os.path.join(shared, "names.json")), {"ou_alice": "王审核", "ou_bob": "李剪辑"})

        # Then at the next sync: only what is new is read, and it may change the rules made before.
        lark.messages.append(message(NOW + 60_000, {"text": "更正：「上头」在剧情台词里也不行，另外 BOSS 可以写成首领"}, parent_id=lark.messages[1]["message_id"]))
        model.answer = lambda _r, _n: {"operations": [
            {"op": "update", "id": "D1", "rule": "「上头」算防沉迷，字幕、口播、剧情台词都不能写", "messages": ["#4", 1]},
            {"op": "add", "category": "字幕和口播", "rule": "BOSS 写成首领", "messages": [4]},
            {"op": "add", "rule": "截图里的东西不能用", "messages": [3]},
        ]}
        hourly = sync_all(ctx_of(lark, model, shared, now=lambda: NOW + 3_600_000))
        self.assertEqual(hourly["groups"], [{"chat": "oc_review", "project": "GOF", "read": 1, "considered": 1, "added": 1, "updated": 1, "removed": 0, "pending": False}])
        second = text_of(model.calls[1])
        # The messages read before come along (the one it replies to among them), so that the new one makes sense; their pictures stay behind.
        self.assertTrue(second.startswith("New messages from the review group (1), with 3 from earlier readings (marked (earlier)):\n\n"
                                          "#1 [2026-10-30 16:00] 王审核 (earlier): 这条被巨量拒了，理由是「上头」算防沉迷\n"
                                          "#2 [2026-10-30 16:01] 李剪辑 (earlier): 以后「上头」都别写了\n"
                                          "#3 [2026-10-31 16:00] 王审核 (earlier): [picture]\n"
                                          "#4 [2026-11-02 16:01] 王审核 (reply to #1): 更正"), second)
        self.assertNotIn("审片结果", second)
        self.assertFalse(any("inlineData" in p for p in model.calls[1].parts), "no pictures from before")
        # The rules come with the words they rest on.
        self.assertIn("- D1 [字幕和口播] 「上头」算防沉迷，字幕口播都不能写 Fix: 换个说法 (2026-11-02; based on: 王审核「这条被巨量拒了，理由是「上头」算防沉迷」; 李剪辑「以后「上头」都别写了」)",
                      model.calls[1].system)
        # An operation that rests only on what was read before is not taken: its ruling is in the spec already.
        self.assertEqual([r["rule"] for r in load_book(shared, "GOF")["rules"]], ["「上头」算防沉迷，字幕、口播、剧情台词都不能写", "BOSS 写成首领"])
        self.assertEqual([s.get("text") for s in load_book(shared, "GOF")["rules"][1]["sources"]], ["更正：「上头」在剧情台词里也不行，另外 BOSS 可以写成首领"])
        # Nothing new: nobody is asked.
        sync_all(ctx_of(lark, model, shared, now=lambda: NOW + 7_200_000))
        self.assertEqual(len(model.calls), 2)

        with self.assertRaisesRegex(Blocked, "already listened to for GOF"):
            watch(PROJECTS["GOT"], None, GROUP, ctx)
        self.assertTrue(watch(PROJECTS["GOF"], None, GROUP, ctx)["already"])

    def test_the_command_line_watch_status_show_remove_and_unwatch_as_the_host_runs_them(self) -> None:
        shared, lark = setup()
        model = FakeModel(lambda _r, _n: {"operations": [
            {"op": "add", "category": "字幕和口播", "rule": "「上头」不能写", "messages": [1]}, {"op": "add", "category": "画面和界面", "rule": "拒审截图里的箭头不能用", "messages": [3]},
        ]})
        workspace = temp_dir("workspace-")
        env = {"AGENT_SHARED_DIR": os.path.dirname(shared), "AGENT_CHAT_ID": "oc_review", "AGENT_CHAT_TYPE": "group", "AGENT_USER_OPEN_ID": "ou_bob"}

        def run(*args: str) -> dict:
            return main(list(args), Context(env=env, lark=lark, model=model, now=lambda: NOW, log=lambda _l: None))[0]

        self.assertEqual(run("rules", "watch")["reason"], "--project is missing: ask the user whether the group reviews GOF or GOT material")
        self.assertIn("YYYY-MM-DD", run("rules", "watch", "--project", "GOF", "--since", "2026/09/01")["reason"])
        self.assertIn("not a valid date", run("rules", "watch", "--project", "GOF", "--since", "2026-02-30")["reason"], "not taken for 03-02")
        watched, code = main(["rules", "watch", "--project", "GOF", "--since", "2026-10-29"], Context(env=env, lark=lark, model=model, now=lambda: NOW, log=lambda _l: None))
        self.assertEqual(code, 0)
        self.assertEqual(watched["since"], "2026-10-29")
        self.assertEqual(watched["read"], 4, "from the date given, not the last year")
        with open(os.path.join(shared, "jobs.json"), encoding="utf-8") as handle:
            job = json.load(handle)["jobs"][0]
        self.assertEqual((job["name"], job["everyMinutes"], job["command"], job["args"][1:]), ("audit-rules-sync", 4320, "python", ["rules", "sync"]),
                         "listening to a group asks a host for the periodic sync")
        self.assertTrue(os.path.isfile(os.path.join(ROOT, job["args"][0])), "run in the skill's folder, the script is there")

        listed = run("rules", "status")
        self.assertEqual(listed["here"], "GOF")
        self.assertEqual(listed["rules"], {"GOF": 2, "GOT": 0})

        target = os.path.join(workspace, "动态审片规范-GOF.md")
        self.assertEqual(run("rules", "show", "--project", "GOF", "--output", target)["file"], target)
        self.assertEqual(run("rules", "show", "--project", "GOF", "--output", target)["file"], os.path.join(workspace, "动态审片规范-GOF (2).md"),
                         "an existing file is not overwritten")

        self.assertIn("has no D7", run("rules", "remove", "--project", "GOF", "--id", "D7")["reason"])
        removed = run("rules", "remove", "--project", "GOF", "--id", "d2")
        self.assertEqual(removed["removed"], [{"id": "D2", "rule": "拒审截图里的箭头不能用"}])
        self.assertEqual(load_book(shared, "GOF")["removed"][0]["reason"], "deleted by a user (ou_bob)")

        self.assertEqual(run("rules", "unwatch"), {"status": "stopped", "project": "GOF", "rules": 1})
        self.assertFalse(os.path.exists(os.path.join(shared, "jobs.json")), "with no group left, there is nothing to sync")
        self.assertIn("not listened to", run("rules", "unwatch")["reason"])
        self.assertIn("not listened to", run("rules", "sync", "--here")["reason"])
        nowhere = main(["rules", "sync", "--here"], Context(env={"AGENT_SHARED_DIR": os.path.dirname(shared)}, lark=lark, model=model, now=lambda: NOW, log=lambda _l: None))[0]
        self.assertIn("--here needs the chat", nowhere["reason"], 'without a chat, "here" is not every group')
        self.assertIn("rules needs", run("rules", "nope")["reason"])

    def test_without_a_chat_from_the_host_every_chat_command_takes_one_with_chat(self) -> None:
        shared, lark = setup()
        model = FakeModel(lambda _r, _n: {"operations": [{"op": "add", "category": "字幕和口播", "rule": "「上头」不能写", "messages": [1]}]})
        host = os.path.dirname(shared)

        def run(*args: str, env: dict | None = None) -> dict:
            return main(list(args), Context(env={"AGENT_SHARED_DIR": host, **(env or {})}, lark=lark, model=model, now=lambda: NOW, log=lambda _l: None))[0]

        self.assertIn("No chat to listen to: pass --chat", run("rules", "watch", "--project", "GOF")["reason"])
        self.assertIn("Feishu chat id", run("rules", "watch", "--project", "GOF", "--chat", "review group")["reason"])
        watched = run("rules", "watch", "--project", "GOF", "--chat", "oc_review")
        self.assertEqual((watched["status"], watched["added"]), ("watching", 1))
        self.assertEqual(lark.asked[0]["chat"], "oc_review")
        self.assertEqual(read_json(os.path.join(shared, "groups.json"))["groups"][0]["chat"], "oc_review")
        self.assertEqual(run("rules", "status")["here"], None, "no chat: none is this one")
        listed = run("rules", "status", "--chat", "oc_review")
        self.assertEqual((listed["here"], listed["groups"][0].get("here")), ("GOF", True))
        lark.messages.append(message(NOW + 60_000, {"text": "「赚米」也不能写"}))
        synced = run("rules", "sync", "--chat", "oc_review")
        self.assertEqual([(g["chat"], g.get("here")) for g in synced["groups"]], [("oc_review", True)])
        self.assertIn("not listened to", run("rules", "sync", "--chat", "oc_other")["reason"])
        # The host's chat is a private one: refused for it, but a group named with --chat is that group.
        private = {"AGENT_CHAT_ID": "oc_private", "AGENT_CHAT_TYPE": "p2p", "AGENT_USER_OPEN_ID": "ou_bob"}
        self.assertIn("private chat", run("rules", "watch", "--project", "GOF", env=private)["reason"])
        self.assertIn("private chat", run("rules", "watch", "--project", "GOF", "--chat", "oc_private", env=private)["reason"])
        self.assertTrue(run("rules", "watch", "--project", "GOF", "--chat", "oc_review", env=private)["already"])
        self.assertEqual(run("rules", "unwatch", "--chat", "oc_review"), {"status": "stopped", "project": "GOF", "rules": 1})
        self.assertFalse(os.path.exists(os.path.join(shared, "jobs.json")))
        self.assertIn("No chat to stop listening to", run("rules", "unwatch")["reason"])

    def test_one_writer_at_a_time_the_periodic_job_gives_way_a_dead_holders_lock_is_taken_over(self) -> None:
        shared, lark = setup()
        model = FakeModel(lambda _r, _n: {"operations": []})
        env = {"AGENT_SHARED_DIR": os.path.dirname(shared)}
        inside = []

        def held() -> None:
            inside.append(True)
            job, code = main(["rules", "sync"], Context(env=env, lark=lark, model=model, log=lambda _l: None))
            self.assertEqual((code, job["status"]), (0, "busy"), "the job (no chat) does not wait and does not fail")
            with self.assertRaisesRegex(Blocked, "being updated"):
                sync_all(ctx_of(lark, model, shared))

        with_lock(shared, held)
        self.assertTrue(inside)
        with open(os.path.join(shared, "lock"), "w", encoding="utf-8") as handle:
            json.dump({"pid": 999_999_999, "at": now_ms()}, handle)
        taken = sync_all(ctx_of(lark, model, shared))
        self.assertEqual(taken["status"], "synced")
        self.assertFalse(os.path.exists(os.path.join(shared, "lock")), "released")

    def test_a_long_history_is_read_over_several_syncs_and_a_failure_keeps_what_was_done_before_it(self) -> None:
        shared, lark = setup()
        # 70 messages of 2000 characters (as long as one gets): 19 to a model call.
        for i in range(70):
            lark.messages.append(message(NOW - DAY + i * 60_000, {"text": f"第 {i} 条：{'很长的讨论' * 400}"}))
        failure = Blocked("The AI gateway is unavailable (HTTP 503): try again later")
        model = FakeModel(lambda _r, n: failure if n == 2 else {"operations": []})
        ctx = ctx_of(lark, model, shared, now=lambda: NOW, max_chunks=2)
        first = watch(PROJECTS["GOF"], None, GROUP, ctx)
        self.assertEqual(first["error"], failure.message)
        after = read_json(os.path.join(shared, "groups.json"))["groups"][0]
        self.assertEqual(after["lastError"], failure.message)
        self.assertEqual(after["cursor"], NOW - DAY + 18 * 60_000, "the first chunk (19 long messages) counts, the failed one does not")
        model.answer = lambda _r, _n: {"operations": []}
        self.assertTrue(sync_all(ctx)["groups"][0]["pending"], "two model calls this time, more to read")
        self.assertFalse(sync_all(ctx)["groups"][0]["pending"])
        done = read_json(os.path.join(shared, "groups.json"))["groups"][0]
        self.assertEqual(done["cursor"], NOW - DAY + 69 * 60_000)
        self.assertEqual(done["read"], 74, "every message counted once, however many readings it took")
        self.assertNotIn("lastError", done)

    def test_a_group_the_bot_cannot_read_says_why(self) -> None:
        shared, lark = setup()
        model = FakeModel(lambda _r, _n: {"operations": []})
        lark.fail_with = LarkError(99991672, "Access denied", ["im:message.group_msg"])
        result = watch(PROJECTS["GOT"], None, GROUP, ctx_of(lark, model, shared, now=lambda: NOW))
        self.assertEqual(result["error"], "The bot lacks a Feishu permission (im:message.group_msg): an administrator must grant it to the app on the Feishu "
                                          "open platform and publish a new version")
        lark.fail_with = LarkError(230002, "Bot is not in the chat")
        synced = sync_all(ctx_of(lark, model, shared))
        self.assertIn("not a member of this chat", synced["groups"][0]["error"])


if __name__ == "__main__":
    unittest.main()
