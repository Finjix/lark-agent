import json
import os
import threading
import time
import unittest
from dataclasses import replace

from audit_sdk.config import Turn, skill_root
from audit_sdk.errors import Blocked
from audit_sdk.gemini import GatewayDown, GatewayRefused
from audit_sdk.jsutil import now_ms
from audit_sdk.lark import LarkError, Picture
from audit_sdk.projects import PROJECTS
from audit_sdk.rules import RulesContext, load_book, show, status, sync_all, watch, with_lock

from tests.helpers.fakes import FakeLark, FakeModel, message, text_of
from tests.helpers.tmp import temp_dir
from tests.test_transcript import utc

DAY = 86_400_000
NOW = utc("2026-11-02T08:00:00Z")
ROOT = skill_root({})
GROUP = Turn(chat="oc_review", chat_type="group", user="ou_alice")


def groups_of(shared: str) -> list[dict]:
    with open(os.path.join(shared, "groups.json"), encoding="utf-8") as handle:
        return json.load(handle)["groups"]


def setup() -> tuple[str, FakeLark, FakeModel, RulesContext]:
    shared = os.path.join(temp_dir("host-"), "audit-auto")
    lark = FakeLark()
    model = FakeModel(lambda _r, _n: {"operations": []})
    return shared, lark, model, RulesContext(lark=lark, model=model, root=ROOT, shared=shared, log=lambda _l: None, now=lambda: NOW)


def busy_thread(*_args: object) -> list:
    raise LarkError(500, "busy")


class SyncTest(unittest.TestCase):
    def test_a_thread_is_followed_replies_after_its_first_message_are_read_at_the_next_sync(self) -> None:
        shared, lark, model, ctx = setup()
        root = message(NOW - DAY, {"text": "这条拒审截图大家看下"}, thread_id="omt_1")
        lark.messages.append(root)
        lark.threads["omt_1"] = [root, message(NOW - DAY + 60_000, {"text": "是因为右下角的箭头"}, thread_id="omt_1", parent_id=root["message_id"])]
        watch(PROJECTS["GOF"], None, GROUP, ctx)
        self.assertRegex(text_of(model.calls[0]), r"#1 .*这条拒审截图大家看下\n#2 .*（回复 #1）: 是因为右下角的箭头")
        self.assertEqual(groups_of(shared)[0]["threads"]["omt_1"], NOW - DAY + 60_000)

        # An hour later the reviewer answers in the thread: the chat's own list has nothing new, the thread has.
        lark.threads["omt_1"].append(message(NOW + 30 * 60_000, {"text": "以后这种箭头都不行"}, thread_id="omt_1", parent_id=root["message_id"]))
        later = replace(ctx, now=lambda: NOW + 3_600_000)
        reading = lark.thread
        lark.thread = busy_thread  # type: ignore[method-assign]
        sync_all(later)
        self.assertEqual(len(model.calls), 1, "the thread could not be read: nothing to read")
        self.assertEqual(groups_of(shared)[0]["threads"]["omt_1"], NOW - DAY + 60_000, "and it stays where it was")
        lark.thread = reading  # type: ignore[method-assign]
        sync_all(later)
        self.assertEqual(len(model.calls), 2)
        self.assertRegex(text_of(model.calls[1]), r"#1 .*王审核（之前的消息）: 这条拒审截图大家看下\n#2 .*（回复 #1）: 以后这种箭头都不行")
        sync_all(later)
        self.assertEqual(len(model.calls), 2, "read once")
        # Two weeks without a reply: the thread is no longer followed.
        sync_all(replace(ctx, now=lambda: NOW + 15 * DAY))
        self.assertEqual(groups_of(shared)[0]["threads"], {})

    def test_an_old_thread_is_let_go_only_once_all_of_it_has_been_read(self) -> None:
        # A thread 100 days old with 25 long replies: more than one model call holds, and the first reading makes only one.
        shared, lark, model, ctx = setup()
        start = NOW - 100 * DAY
        root = message(start, {"text": "这条拒审了，大家看下原因"}, thread_id="omt_old")
        lark.messages.append(root)
        replies = [message(start + (i + 1) * 60_000, {"text": f"第 {i + 1} 条：{'很长的讨论' * 400}"}, thread_id="omt_old", parent_id=root["message_id"]) for i in range(25)]
        lark.threads["omt_old"] = [root, *replies]
        first = watch(PROJECTS["GOF"], None, GROUP, replace(ctx, max_chunks=1))
        self.assertTrue(first["pending"])
        self.assertIn("omt_old", groups_of(shared)[0]["threads"], "still followed: the rest of it is unread")
        second = sync_all(replace(ctx, max_chunks=1))
        self.assertFalse(second["groups"][0]["pending"])
        self.assertIn("第 25 条", text_of(model.calls[1]), "the rest is read at the next sync")
        self.assertEqual(groups_of(shared)[0]["threads"], {}, "read to its end, and older than two weeks: let go")

        # A thread that could not be read at the first reading is read at the next, old as it is.
        shared2, lark2, model2, ctx2 = setup()
        root2 = message(start, {"text": "这条拒审了，大家看下原因"}, thread_id="omt_2")
        lark2.messages.append(root2)
        lark2.threads["omt_2"] = [root2, message(start + 60_000, {"text": "以后这种箭头都不行"}, thread_id="omt_2", parent_id=root2["message_id"])]
        reading = lark2.thread
        lark2.thread = busy_thread  # type: ignore[method-assign]
        watch(PROJECTS["GOF"], None, GROUP, ctx2)
        self.assertEqual(groups_of(shared2)[0]["threads"]["omt_2"], start, "kept where it was")
        lark2.thread = reading  # type: ignore[method-assign]
        sync_all(ctx2)
        self.assertIn("以后这种箭头都不行", text_of(model2.calls[-1]))

    def test_a_chunk_the_model_keeps_refusing_goes_once_more_without_pictures_and_after_three_readings_is_skipped(self) -> None:
        shared, lark, _model, ctx = setup()
        lark.messages += [message(NOW - DAY, {"image_key": "img_bad"}, type="image"), message(NOW - DAY + 1000, {"text": "这张图被拒了"})]
        lark.pictures["img_bad"] = Picture(data=b"x", mime="image/png")
        asked: list[bool] = []

        def refuse(request, _n):
            asked.append(any("inlineData" in p for p in request.parts))
            return Blocked("AI 网关拒绝了这次请求（HTTP 400）：invalid argument")

        run = replace(ctx, model=FakeModel(refuse))
        first = watch(PROJECTS["GOF"], None, GROUP, run)
        self.assertIn("HTTP 400", first["error"])
        self.assertEqual(asked, [True, False], "with the picture, then without")
        self.assertEqual(groups_of(shared)[0]["stuck"]["tries"], 1)
        sync_all(run)
        self.assertEqual(groups_of(shared)[0]["stuck"]["tries"], 2)
        third = sync_all(run)
        self.assertNotIn("error", third["groups"][0], "skipped on the third reading")
        after = groups_of(shared)[0]
        self.assertNotIn("stuck", after)
        self.assertEqual(len(after["skipped"]), 1)
        self.assertIn("HTTP 400", after["skipped"][0]["reason"])
        self.assertEqual(after["cursor"], NOW - DAY + 1000, "read past it")
        listed = status(shared, Turn(chat="oc_review"))["groups"][0]
        self.assertTrue(listed["here"], "the current chat is marked, so the skill can say 本群 without showing ids")
        self.assertEqual(len(listed["skipped"]), 1)

        # A gateway that is down does not count: the chunk is tried again, as it is, however often.
        shared2, lark2, _m2, ctx2 = setup()
        lark2.messages.append(message(NOW - DAY, {"text": "「搞钱」不能写"}))
        down = FakeModel(lambda _r, _n: GatewayDown("AI 网关暂时不可用（HTTP 503），稍后再试"))
        for i in range(4):
            (sync_all(replace(ctx2, model=down)) if i else watch(PROJECTS["GOF"], None, GROUP, replace(ctx2, model=down)))
        g = groups_of(shared2)[0]
        self.assertNotIn("stuck", g)
        self.assertNotIn("skipped", g)
        self.assertEqual(g["cursor"], 0)
        self.assertEqual(len(down.calls), 4, "one call a reading: no picture to leave out")

        # Nor does a key the gateway no longer takes: once it is fixed, the chunk is read as if nothing happened.
        shared3, lark3, _m3, ctx3 = setup()
        lark3.messages.append(message(NOW - DAY, {"text": "「搞钱」不能写"}))
        refusing = FakeModel(lambda _r, _n: GatewayRefused("AI 网关不接受这个 key（HTTP 401），请联系管理员"))
        for i in range(4):
            (sync_all(replace(ctx3, model=refusing)) if i else watch(PROJECTS["GOF"], None, GROUP, replace(ctx3, model=refusing)))
        g = groups_of(shared3)[0]
        self.assertNotIn("stuck", g)
        self.assertNotIn("skipped", g)
        self.assertEqual(g["lastError"], "AI 网关不接受这个 key（HTTP 401），请联系管理员")

    def test_a_skipped_stretch_is_dated_by_its_own_new_messages(self) -> None:
        shared, lark, model, ctx = setup()
        lark.messages.append(message(NOW - 3 * DAY, {"text": "「上头」不能写"}))
        watch(PROJECTS["GOF"], None, GROUP, ctx)
        lark.messages.append(message(NOW + 60_000, {"text": "这张也被拒了"}))
        model.answer = lambda _r, _n: Blocked("AI 网关拒绝了这次请求（HTTP 400）：invalid argument")
        for _ in range(3):
            sync_all(replace(ctx, now=lambda: NOW + 3_600_000))
        self.assertEqual([(s["from"], s["to"]) for s in groups_of(shared)[0]["skipped"]], [("2026-11-02 16:01", "2026-11-02 16:01")])

    def test_when_the_only_new_messages_are_left_out_nobody_is_asked_a_reply_to_the_bot_says_so(self) -> None:
        shared, lark, model, ctx = setup()
        result = message(NOW - DAY, {"text": "审片结果：……"}, sender="cli_bot")
        lark.messages += [result, message(NOW - DAY + 60_000, {"text": "这个判错了，「赚米」可以写"}, parent_id=result["message_id"])]
        watch(PROJECTS["GOF"], None, GROUP, ctx)
        self.assertRegex(text_of(model.calls[0]), r"#1 .*王审核（回复机器人的消息）: 这个判错了")
        lark.messages += [message(NOW + 60_000, {"text": "已经更新"}, sender="cli_bot"), message(NOW + 120_000, {"file_key": "x"}, type="sticker")]
        sync_all(replace(ctx, now=lambda: NOW + 3_600_000))
        self.assertEqual(len(model.calls), 1)
        self.assertEqual(groups_of(shared)[0]["cursor"], NOW + 120_000, "read past them")

    def test_the_daily_sync_reads_the_group_read_longest_ago_first_and_a_group_the_time_ran_out_on_keeps_its_place(self) -> None:
        shared, lark, model, ctx = setup()
        lark.messages.append(message(NOW - DAY, {"text": "「上头」不能写"}))
        watch(PROJECTS["GOF"], None, GROUP, ctx)
        watch(PROJECTS["GOT"], None, replace(GROUP, chat="oc_other"), ctx)
        path = os.path.join(shared, "groups.json")
        with open(path, encoding="utf-8") as handle:
            groups = json.load(handle)
        groups["groups"][0]["lastSync"] = NOW
        groups["groups"][1]["lastSync"] = NOW - DAY
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(groups, handle)
        lark.messages.append(message(NOW + 1000, {"text": "「赚米」也不行"}))
        asked: list[str] = []

        def history(chat: str, _start: int, _end: int):
            asked.append(chat)
            for m in list(lark.messages):
                if int(m["create_time"]) >= NOW:
                    yield m

        lark.history = history  # type: ignore[method-assign]
        late = sync_all(replace(ctx, now=lambda: NOW + 2000, deadline=now_ms() - 1))
        self.assertEqual(asked, [], "out of time: nothing is read")
        self.assertEqual([g["pending"] for g in late["groups"]], [True, True], "all left for next time")
        self.assertEqual(len(model.calls), 2, "only the two watches")

        # The first group takes the time up: the other is not read at all, and so it comes first the next time.
        def slow_answer(_r, _n):
            time.sleep(0.3)
            return {"operations": []}

        short = sync_all(replace(ctx, model=FakeModel(slow_answer), now=lambda: NOW + 3000, deadline=now_ms() + 100))
        self.assertEqual(asked, ["oc_other"])
        self.assertEqual([(g["chat"], g["pending"]) for g in short["groups"]], [("oc_other", False), ("oc_review", True)])
        asked.clear()
        sync_all(replace(ctx, now=lambda: NOW + 4000))
        self.assertEqual(asked, ["oc_review", "oc_other"])

        def broken(*_args):
            raise LarkError(230002, "bot not in chat")
            yield  # noqa: unreachable - a generator

        lark.history = broken  # type: ignore[method-assign]
        self.assertEqual(sync_all(ctx)["status"], "failed")

    def test_the_lock_is_released_and_kept_alive_only_by_its_holder_a_dead_holders_lock_is_taken_over_once(self) -> None:
        shared, _lark, _model, _ctx = setup()
        os.makedirs(shared, exist_ok=True)
        lock = os.path.join(shared, "lock")

        def replace_it() -> None:
            # Somebody (wrongly) replaced the lock meanwhile: releasing must not delete theirs.
            with open(lock, "w", encoding="utf-8") as handle:
                json.dump({"pid": os.getpid(), "token": "theirs", "at": now_ms()}, handle)

        with_lock(shared, replace_it)
        with open(lock, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["token"], "theirs")
        with open(lock, "w", encoding="utf-8") as handle:
            json.dump({"pid": 999_999_999, "token": "dead", "at": now_ms()}, handle)
        order: list[str] = []

        def inside(n: int) -> None:
            order.append(f"in {n}")
            time.sleep(0.05)
            order.append(f"out {n}")

        workers = [threading.Thread(target=with_lock, args=(shared, lambda n=n: inside(n), 10_000)) for n in (1, 2)]
        for w in workers:
            w.start()
        for w in workers:
            w.join()
        self.assertEqual(len(order), 4)
        self.assertTrue(order[0].startswith("in") and order[1].startswith("out") and order[2].startswith("in"), f"one at a time: {order}")
        self.assertFalse(os.path.exists(lock))

    def test_the_dynamic_spec_can_be_read_without_leaving_a_file_behind(self) -> None:
        shared, *_ = setup()
        answer = show(shared, PROJECTS["GOT"])
        self.assertEqual(answer["status"], "ok")
        self.assertRegex(answer["markdown"], r"^# GOT 动态审片规范[\s\S]*还没有整理出规范")
        self.assertFalse(os.path.exists(os.path.join(shared, "rules")))
        self.assertEqual(len(load_book(shared, "GOT")["rules"]), 0)


if __name__ == "__main__":
    unittest.main()
