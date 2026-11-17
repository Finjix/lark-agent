"""A scripted model and an in-memory Feishu group, for the rules and review tests."""

from __future__ import annotations

import itertools
import json
from typing import Any, Callable, Iterator

from audit_sdk.gemini import JsonReply, JsonRequest, Usage
from audit_sdk.lark import FeishuMessage, Picture


class FakeModel:
    """A model that answers from a script and remembers what it was asked; an exception as the answer is raised."""

    name = "fake-gemini"

    def __init__(self, answer: Callable[[JsonRequest, int], Any]) -> None:
        self.answer = answer
        self.calls: list[JsonRequest] = []

    def json(self, request: JsonRequest) -> JsonReply:
        self.calls.append(request)
        data = self.answer(request, len(self.calls))
        if isinstance(data, BaseException):
            raise data
        return JsonReply(data=data, usage=Usage(1, 1, 0), ms=1, attempts=1)


def text_of(request: JsonRequest) -> str:
    """The text parts of a request, joined."""
    return "\n".join(p.get("text", "") for p in request.parts)


_counter = itertools.count(1)


def message(at: int, content: Any, sender: str = "ou_alice", type: str = "text", **extra: Any) -> FeishuMessage:
    """A group message the way Feishu's message list returns it."""
    if sender.startswith("cli_"):
        who = {"id": sender, "id_type": "app_id", "sender_type": "app"}
    else:
        who = {"id": sender, "id_type": "open_id", "sender_type": "user"}
    return {"message_id": f"om_{next(_counter)}", "msg_type": type, "create_time": str(at), "sender": who,
            "body": {"content": json.dumps(content, ensure_ascii=False)}, **extra}


class FakeLark:
    """A group: its messages (the list grows as a test adds to it), what each call asked for, members' names."""

    app_id = "cli_bot"

    def __init__(self) -> None:
        self.messages: list[FeishuMessage] = []
        self.threads: dict[str, list[FeishuMessage]] = {}
        self.forwards: dict[str, list[FeishuMessage]] = {}
        self.pictures: dict[str, Picture] = {}
        self.names: dict[str, str] = {"ou_alice": "王审核", "ou_bob": "李剪辑"}
        self.asked: list[dict[str, Any]] = []
        self.fail_with: BaseException | None = None

    def history(self, chat: str, start: int, end: int) -> Iterator[FeishuMessage]:
        self.asked.append({"chat": chat, "start": start, "end": end})
        if self.fail_with:
            raise self.fail_with
        for m in list(self.messages):
            seconds = int(m["create_time"]) // 1000
            if start <= seconds <= end:
                yield m

    def recent(self, chat: str, end: int, count: int) -> list[FeishuMessage]:
        if self.fail_with:
            raise self.fail_with
        found = sorted((m for m in self.messages if int(m["create_time"]) // 1000 <= end), key=lambda m: int(m["create_time"]))
        return found[-count:]

    def thread(self, thread: str) -> list[FeishuMessage]:
        return list(self.threads.get(thread, []))

    def message(self, message_id: str) -> list[FeishuMessage]:
        """A message by id: a forward with what it carries, or any message of the group (a reply's parent from an earlier reading)."""
        if message_id in self.forwards:
            return self.forwards[message_id]
        everything = [*self.messages, *(m for ms in self.threads.values() for m in ms)]
        return [m for m in everything if m["message_id"] == message_id]

    def picture(self, message: str, key: str) -> Picture | None:
        return self.pictures.get(key)

    def user_name(self, open_id: str) -> str | None:
        return self.names.get(open_id)

    def verify(self) -> None:
        if self.fail_with:
            raise self.fail_with
