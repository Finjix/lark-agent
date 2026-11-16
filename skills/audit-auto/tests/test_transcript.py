import unittest
from datetime import datetime, timezone

from audit_sdk.transcript import EntryContext, chunks, content_of, entries_of, render_chunk, stamp

from tests.helpers.fakes import message


def utc(text: str) -> int:
    return int(datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc).timestamp() * 1000)


AT = utc("2026-10-30T06:05:00Z")  # 14:05 in China
NAMES = {"ou_alice": "王审核", "ou_bob": "李剪辑"}


def ctx(forwarded: dict | None = None) -> EntryContext:
    return EntryContext(app_id="cli_bot", name=lambda i: NAMES.get(i) or f"成员{i[-4:]}", forwarded=forwarded or {})


class TranscriptTest(unittest.TestCase):
    def test_what_a_message_says_text_with_mentions_rich_text_in_either_shape_pictures_files_cards(self) -> None:
        self.assertEqual(content_of(message(AT, {"text": "@_user_1 这条被拒了 @_all"}, mentions=[{"key": "@_user_1", "name": "李剪辑"}])),
                         ("@李剪辑 这条被拒了 @所有人", []))
        post = {"title": "拒审", "content": [[{"tag": "text", "text": "巨量说"}, {"tag": "at", "user_id": "@_user_1"}, {"tag": "text", "text": " 上头不能用"}],
                                           [{"tag": "img", "image_key": "img_1"}]]}
        self.assertEqual(content_of(message(AT, post, type="post", mentions=[{"key": "@_user_1", "name": "王审核"}])), ("拒审\n巨量说@王审核 上头不能用", ["img_1"]))
        self.assertEqual(content_of(message(AT, {"zh_cn": post}, type="post"))[1], ["img_1"], "a post under a language key")
        self.assertEqual(content_of(message(AT, {"image_key": "img_2"}, type="image")), ("", ["img_2"]))
        self.assertEqual(content_of(message(AT, {"file_key": "f", "file_name": "拒审截图.zip"}, type="file"))[0], "[文件：拒审截图.zip]")
        self.assertEqual(content_of(message(AT, {"title": "素材审核结果", "elements": [[{"tag": "text", "text": "拒绝：含防沉迷词"}]]}, type="interactive"))[0],
                         "素材审核结果 拒绝：含防沉迷词")
        self.assertEqual(content_of(message(AT, {}, type="sticker"))[0], "")
        self.assertEqual(content_of({**message(AT, {}), "body": {"content": "not json"}})[0], "")

    def test_the_bots_own_messages_system_notices_recalled_and_empty_ones_are_left_out_forwarded_ones_follow(self) -> None:
        forward = message(AT + 3000, {"content": "Merged and Forwarded Message"}, type="merge_forward")
        inner = [
            {**message(AT - 86_400_000, {"text": "这条被字节拒了：「赚米」"}, sender="ou_carol"), "upper_message_id": forward["message_id"]},
            {**message(AT - 86_000_000, {"text": "unrelated"}), "upper_message_id": "om_other"},
        ]
        listed = [
            message(AT, {"text": "「上头」不能用了"}),
            message(AT + 1000, {"text": "审片结果……"}, sender="cli_bot", type="interactive"),
            message(AT + 1500, {"text": "审核通知：已拒绝"}, sender="cli_other"),
            message(AT + 2000, {}, type="system"),
            {**message(AT + 2500, {"text": "撤回了"}), "deleted": True},
            message(AT + 2600, {}, type="sticker"),
            forward,
            message(AT + 4000, {"text": "收到"}, sender="ou_bob", parent_id="om_x"),
        ]
        entries = entries_of(listed, ctx({forward["message_id"]: [forward, *inner]}))
        self.assertEqual([(e["by"], e["text"], e.get("forwarded", False)) for e in entries], [
            ("王审核", "「上头」不能用了", False),
            ("机器人", "审核通知：已拒绝", False),
            ("王审核", "[转发了一段聊天记录：]", False),
            ("成员arol", "这条被字节拒了：「赚米」", True),
            ("李剪辑", "收到", False),
        ])
        self.assertEqual(entries[-1]["reply"], "om_x")

    def test_the_transcript_numbers_messages_says_which_one_a_reply_answers_and_labels_pictures_in_china_time(self) -> None:
        entries = [
            {"id": "a", "at": AT, "by": "王审核", "text": "这条被拒了", "pictures": ["img_1", "img_2"]},
            {"id": "b", "at": AT + 60_000, "by": "李剪辑", "text": "为啥", "pictures": [], "reply": "a"},
            {"id": "c", "at": AT + 120_000, "by": "王审核", "text": "见上", "pictures": [], "reply": "old"},
            {"id": "d", "at": AT + 180_000, "by": "王审核", "text": "再说一次", "pictures": [], "reply": "gone"},
            {"id": "e", "at": AT + 240_000, "by": "成员", "text": "转来的", "pictures": [], "forwarded": True},
        ]
        self.assertEqual(render_chunk(entries, {"old": "昨天说的：BOSS 要写成怪"}), "\n".join([
            "#1 [2026-10-30 14:05] 王审核: 这条被拒了 [图片 #1-1] [图片 #1-2]",
            "#2 [2026-10-30 14:06] 李剪辑（回复 #1）: 为啥",
            "#3 [2026-10-30 14:07] 王审核（回复：「昨天说的：BOSS 要写成怪」）: 见上",
            "#4 [2026-10-30 14:08] 王审核（回复更早的消息）: 再说一次",
            "    ↳ #5 [2026-10-30 14:09] 成员: 转来的",
        ]))
        # Told which pictures go with it, the transcript says of the others that they did not come along.
        self.assertRegex(render_chunk(entries, {}, set(), {"1-2"}), r"(?m)^#1 .*: 这条被拒了 \[图片（没能附上）\] \[图片 #1-2\]$")
        self.assertEqual(stamp(utc("2026-11-02T16:30:00Z")), "2026-11-03 00:30")

    def test_chunks_hold_bounded_text_and_pictures_and_never_split_a_forward_from_what_it_carries(self) -> None:
        def entry(i: str, size: int, pictures: int = 0, forwarded: bool = False) -> dict:
            e = {"id": i, "at": AT, "by": "x", "text": "x" * size, "pictures": ["k"] * pictures}
            if forwarded:
                e["forwarded"] = True
            return e

        parts = chunks([entry("1", 500), entry("2", 500), entry("3", 500), entry("4", 500, 0, True), entry("5", 10, 3), entry("6", 10, 2)], 1200, 4)
        self.assertEqual([[e["id"] for e in p] for p in parts], [["1", "2"], ["3", "4", "5"], ["6"]])
        self.assertEqual(chunks([]), [])


if __name__ == "__main__":
    unittest.main()
