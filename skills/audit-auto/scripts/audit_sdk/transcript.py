"""A review group's messages as the model reads them (port of transcript.ts).

One numbered line per message (time, sender, what it says, which message it answers), pictures attached after the transcript with
the number of their message. The bot's own messages are left out (its review results are not rulings), and so are system notices,
recalled messages and messages with nothing in them (a sticker, say).

An entry is a dict: id, at, by, text, pictures (image keys, fetched with the message's own id), and when they apply reply (the message
it replies to), thread (the thread, or topic, it belongs to), forwarded (set on the messages a merged-and-forwarded message carries:
they keep their own sender and time) and earlier (a message from before this reading, shown because a new one replies to it).
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from .jsutil import day, js_number, js_str, stamp  # noqa: F401 - day and stamp are part of this module's surface, as in the original

Entry = dict[str, Any]
FeishuMessage = dict[str, Any]


@dataclass
class EntryContext:
    app_id: str
    name: Callable[[str], str]
    #: The messages each merge-forward message carries, by its id.
    forwarded: dict[str, list[FeishuMessage]] = field(default_factory=dict)


MAX_TEXT = 2000


def clip(text: str, maximum: int) -> str:
    return f"{text[:maximum]}…" if len(text) > maximum else text


def _parse(content: Any) -> Any:
    try:
        parsed = json.loads(content if content is not None else "{}")
    except (ValueError, TypeError):
        return {}
    return parsed if parsed is not None else {}


def _get(node: Any, key: str) -> Any:
    return node.get(key) if isinstance(node, dict) else None


def _post_text(body: Any, mention: Callable[[str], str]) -> tuple[str, list[str]]:
    """The plain text of a rich-text (post) body: either one language's {title, content} or several of them under language keys."""
    if isinstance(_get(body, "content"), list):
        post = body
    else:
        post = next((v for v in (body.values() if isinstance(body, dict) else []) if isinstance(_get(v, "content"), list)), {})
    pictures: list[str] = []

    def element(el: Any) -> str:
        tag = _get(el, "tag")
        if tag in ("text", "md", "code_block"):
            return js_str(el.get("text"))
        if tag == "a":
            return js_str(el.get("text") if el.get("text") is not None else el.get("href"))
        if tag == "at":
            return f"@{el.get('user_name') or mention(js_str(el.get('user_id')))}"
        if tag == "img":
            if el.get("image_key"):
                pictures.append(js_str(el["image_key"]))
            return ""
        if tag == "media":
            return "[视频]"
        return ""

    paragraphs = ["".join(element(el) for el in (line if isinstance(line, list) else [])) for line in (_get(post, "content") or [])]
    title = js_str(post.get("title")) if post.get("title") else ""
    text = "\n".join(s for s in [title, *paragraphs] if s.strip())
    return text, pictures


def _card_text(body: Any) -> str:
    """The words of a card (another bot's notice, say): every text, content and title it holds."""
    words: list[str] = []

    def walk(node: Any, depth: int) -> None:
        if depth > 12 or not isinstance(node, (dict, list)):
            return
        items = node.items() if isinstance(node, dict) else ((str(i), v) for i, v in enumerate(node))
        for key, value in items:
            if isinstance(value, str) and key in ("text", "content", "title") and value.strip():
                words.append(value.strip())
            elif isinstance(value, (dict, list)):
                walk(value, depth + 1)

    walk(body, 0)
    return clip(" ".join(words), 800)


_MENTION = re.compile(r"@_user_\d+|@_all")


def content_of(message: FeishuMessage) -> tuple[str, list[str]]:
    """What a message says and the pictures in it."""
    body = _parse(_get(message.get("body"), "content"))
    if not isinstance(body, (dict, list)):
        body = {}

    def mention(key: str) -> str:
        if key == "@_all":
            return "所有人"
        found = next((m for m in message.get("mentions") or [] if m.get("key") == key), None)
        return found.get("name") if found and found.get("name") is not None else "某人"

    kind = message.get("msg_type")
    if kind == "text":
        return _MENTION.sub(lambda m: f"@{mention(m.group(0))}", js_str(_get(body, "text"))), []
    if kind == "post":
        return _post_text(body, mention)
    if kind == "image":
        key = _get(body, "image_key")
        return "", [js_str(key)] if key else []
    if kind == "file":
        return f"[文件：{js_str(_get(body, 'file_name'))}]", []
    if kind == "media":
        return f"[视频：{js_str(_get(body, 'file_name'))}]", []
    if kind == "audio":
        return "[语音]", []
    if kind == "interactive":
        return _card_text(body), []
    if kind == "merge_forward":
        return "[转发了一段聊天记录：]", []
    return "", []


def entry_of(message: FeishuMessage, ctx: EntryContext, forwarded: bool = False) -> Entry | None:
    """One message as an entry, or None when it is left out."""
    if message.get("deleted") or message.get("msg_type") == "system":
        return None
    sender = message.get("sender") or {}
    if sender.get("sender_type") == "app" and sender.get("id") == ctx.app_id:
        return None
    text, pictures = content_of(message)
    if not text.strip() and not pictures:
        return None
    if sender.get("sender_type") == "user" and sender.get("id"):
        by = ctx.name(sender["id"])
    else:
        by = "机器人" if sender.get("sender_type") == "app" else "成员"
    at = js_number(message.get("create_time"))
    entry: Entry = {"id": message["message_id"], "at": int(at) if math.isfinite(at) and at else 0, "by": by,
                    "text": clip(text.strip(), MAX_TEXT), "pictures": pictures}
    if message.get("parent_id") and not forwarded:
        entry["reply"] = message["parent_id"]
    if message.get("thread_id") and not forwarded:
        entry["thread"] = message["thread_id"]
    if forwarded:
        entry["forwarded"] = True
    return entry


def entries_of(messages: list[FeishuMessage], ctx: EntryContext) -> list[Entry]:
    """The entries of these messages (oldest first), with what each merged-and-forwarded message carries right after it."""
    out: list[Entry] = []
    for message in messages:
        entry = entry_of(message, ctx)
        if not entry:
            continue
        out.append(entry)
        if message.get("msg_type") != "merge_forward":
            continue
        carried = [m for m in ctx.forwarded.get(message["message_id"], []) if m.get("upper_message_id") == message["message_id"]][:100]
        for inner in carried:
            child = entry_of(inner, ctx, True)
            if child:
                out.append(child)
    return out


def chunks(entries: list[Entry], max_chars: int = 40_000, max_pictures: int = 24) -> list[list[Entry]]:
    """Splits entries into what one model call reads: bounded text and pictures, a forwarded message kept with what it carries and an
    earlier message with the reply that brought it in."""
    out: list[list[Entry]] = []
    current: list[Entry] = []
    chars = pictures = 0
    for entry in entries:
        size = len(entry["text"]) + 40
        if current and not entry.get("forwarded") and not current[-1].get("earlier") and (
                chars + size > max_chars or pictures + len(entry["pictures"]) > max_pictures):
            out.append(current)
            current, chars, pictures = [], 0, 0
        current.append(entry)
        chars += size
        pictures += len(entry["pictures"])
    if current:
        out.append(current)
    return out


_SPACES = re.compile(r"\s+")


def render_chunk(entries: list[Entry], earlier: dict[str, str], bot: set[str] | None = None, attached: set[str] | None = None) -> str:
    """The transcript of one chunk: `#n [time] sender（回复 #k）: text`, the pictures of message n labelled [图片 #n-1] …; `earlier`
    holds the text of messages before the chunk, for replies to them, and `bot` the bot's own messages, which are not shown but may be
    replied to. `attached`, when given, holds the labels (`n-j`) of the pictures that go with the transcript; the others are marked as
    not attached."""
    bot = bot or set()
    number = {e["id"]: i + 1 for i, e in enumerate(entries)}
    lines: list[str] = []
    for i, entry in enumerate(entries):
        n = i + 1
        reply = ""
        if entry.get("reply"):
            k = number.get(entry["reply"])
            before = earlier.get(entry["reply"])
            if k and k < n:
                reply = f"（回复 #{k}）"
            elif before:
                reply = f"（回复：「{clip(_SPACES.sub(' ', before), 40)}」）"
            elif entry["reply"] in bot:
                reply = "（回复机器人的消息）"
            else:
                reply = "（回复更早的消息）"
        pictures = "".join(f" [图片 #{n}-{j + 1}]" if attached is None or f"{n}-{j + 1}" in attached else " [图片（没能附上）]"
                           for j in range(len(entry["pictures"])))
        lines.append(f"{'    ↳ ' if entry.get('forwarded') else ''}#{n} [{stamp(entry['at'])}] {entry['by']}"
                     f"{'（之前的消息）' if entry.get('earlier') else ''}{reply}: {entry['text']}{pictures}")
    return "\n".join(lines)
