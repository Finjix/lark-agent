"""Reading a review group as the bot (port of lark.ts).

The app's tenant token; the bot is a member of the group. That needs the app permission to read all messages of the groups it is in
(im:message.group_msg), not only the ones that @-mention it, plus im:message:readonly for pictures and contact:user.base:readonly for
names. Only reads: nothing is ever sent from here.

A Feishu message is kept as the dict Feishu returns: message_id, root_id, parent_id, thread_id, upper_message_id, msg_type,
create_time (milliseconds since the epoch, as a string), deleted, sender {id, id_type, sender_type}, body {content}, mentions.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Iterator, Protocol

from .config import App
from .errors import Blocked, message_of, redact
from .jsutil import now_ms

FeishuMessage = dict[str, Any]


@dataclass
class Picture:
    data: bytes
    mime: str


class Lark(Protocol):
    #: The bot's app id: the bot's own messages are left out of what is read.
    app_id: str

    def history(self, chat: str, start: int, end: int) -> Iterator[FeishuMessage]:
        """The messages of a chat from `start` to `end` (seconds since the epoch), oldest first."""
        ...

    def recent(self, chat: str, end: int, count: int) -> list[FeishuMessage]:
        """The last `count` messages of a chat up to `end` (seconds since the epoch, at most 50), oldest first."""
        ...

    def thread(self, thread: str) -> list[FeishuMessage]:
        """The replies in a thread (topic), oldest first."""
        ...

    def message(self, message_id: str) -> list[FeishuMessage]:
        """A message by its id: the message, and for a merged-and-forwarded one the messages it carries (their upper_message_id is its id)."""
        ...

    def picture(self, message: str, key: str) -> Picture | None: ...

    def user_name(self, open_id: str) -> str | None: ...

    def verify(self) -> None:
        """Whether Feishu takes the app's credentials (doctor)."""
        ...


class LarkError(Exception):
    """An error code Feishu answered with."""

    def __init__(self, code: int, message: str, scopes: list[str] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.scopes = scopes or []


NO_SCOPE = {99991672, 99991679}
NOT_IN_CHAT = {230002, 230027, 232011}
PAGE = 50


def explain_lark(error: BaseException) -> str:
    """What a failure to read a group means for the user, or for the administrator."""
    if isinstance(error, Blocked):
        return error.message
    if isinstance(error, LarkError):
        if error.code in NO_SCOPE:
            scopes = f"（{' / '.join(error.scopes)}）" if error.scopes else ""
            return f"机器人缺少飞书权限{scopes}：请管理员在飞书开放平台给应用开通并发布新版本"
        if error.code in NOT_IN_CHAT:
            return "机器人不在这个群里（可能已被移出群），读不到群消息"
        return f"读取飞书消息失败（错误码 {error.code}）：{error.message[:200]}"
    return f"读取群消息失败：{message_of(error)[:200]}"


def _quote(value: str) -> str:
    """encodeURIComponent."""
    return urllib.parse.quote(value, safe="-_.!~*'()")


class _Reply:
    def __init__(self, status: int, content_type: str, body: bytes) -> None:
        self.status = status
        self.content_type = content_type
        self.body = body

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8"))


def _send(url: str, data: bytes | None = None, headers: dict[str, str] | None = None, timeout: float = 120) -> _Reply:
    request = urllib.request.Request(url, data=data, headers=headers or {}, method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return _Reply(response.status, response.headers.get("content-type") or "", response.read())
    except urllib.error.HTTPError as error:
        body = error.read() if error.fp else b""
        return _Reply(error.code, (error.headers.get("content-type") if error.headers else "") or "", body)


class BotLark:
    """Feishu's open API with the app's tenant token."""

    def __init__(self, app: App, sleep: Callable[[float], None] = time.sleep) -> None:
        self.app = app
        self.app_id = app.id
        self.sleep = sleep
        self._token: tuple[str, int] | None = None

    def _tenant_token(self) -> str:
        if self._token and self._token[1] > now_ms():
            return self._token[0]
        reply = _send(f"{self.app.base}/auth/v3/tenant_access_token/internal",
                      json.dumps({"app_id": self.app.id, "app_secret": self.app.secret}).encode("utf-8"),
                      {"content-type": "application/json; charset=utf-8"})
        try:
            body = reply.json()
        except ValueError:
            body = {}
        body = body if isinstance(body, dict) else {}
        if body.get("code") != 0 or not body.get("tenant_access_token"):
            code = body.get("code") if body.get("code") is not None else reply.status
            raise Blocked(f"飞书应用凭证无效（错误码 {code}），请联系管理员")
        expire = body.get("expire") or 3600
        self._token = (body["tenant_access_token"], now_ms() + max(60, expire - 300) * 1000)
        return self._token[0]

    def _call(self, path: str, params: dict[str, Any] | None = None, raw: bool = False) -> Any:
        """One GET; an expired token is renewed once, rate limits and network trouble are tried again twice."""
        query = urllib.parse.urlencode([(k, str(v)) for k, v in (params or {}).items() if v is not None])
        url = f"{self.app.base}{path}{'?' + query if query else ''}"
        attempt = 0
        while True:
            attempt += 1
            try:
                reply = _send(url, headers={"authorization": f"Bearer {self._tenant_token()}"})
            except Blocked:
                raise
            except Exception as error:  # noqa: BLE001 - the network: tried again
                if attempt >= 3:
                    raise RuntimeError(redact(message_of(error), [self.app.secret])) from None
                self.sleep(2 * attempt)
                continue
            if raw and reply.ok and "application/json" not in reply.content_type:
                return Picture(data=reply.body, mime=reply.content_type.split(";")[0].strip())
            try:
                body = reply.json()
            except ValueError:
                body = {"code": reply.status, "msg": f"HTTP {reply.status}"}
            body = body if isinstance(body, dict) else {"code": reply.status, "msg": f"HTTP {reply.status}"}
            code = body.get("code")
            if code == 0:
                return body.get("data") or {}
            if code in (99991663, 99991661) and attempt < 3:
                self._token = None
                continue
            if (code == 99991400 or reply.status == 429 or reply.status >= 500) and attempt < 3:
                self.sleep(2 * attempt)
                continue
            violations = ((body.get("error") or {}).get("permission_violations") or []) if isinstance(body.get("error"), dict) else []
            scopes = [v.get("subject") for v in violations if isinstance(v, dict) and v.get("subject")]
            raise LarkError(int(code if code is not None else reply.status), str(body.get("msg") or ""), scopes)

    def _pages(self, params: dict[str, Any]) -> Iterator[FeishuMessage]:
        page_token: str | None = None
        while True:
            data = self._call("/im/v1/messages", {**params, "sort_type": "ByCreateTimeAsc", "page_size": PAGE, "page_token": page_token})
            yield from data.get("items") or []
            page_token = data.get("page_token") if data.get("has_more") else None
            if not page_token:
                return

    def history(self, chat: str, start: int, end: int) -> Iterator[FeishuMessage]:
        return self._pages({"container_id_type": "chat", "container_id": chat, "start_time": start, "end_time": end})

    def recent(self, chat: str, end: int, count: int) -> list[FeishuMessage]:
        data = self._call("/im/v1/messages", {"container_id_type": "chat", "container_id": chat, "end_time": end,
                                              "sort_type": "ByCreateTimeDesc", "page_size": min(count, PAGE)})
        return list(reversed(data.get("items") or []))

    def thread(self, thread: str) -> list[FeishuMessage]:
        return list(self._pages({"container_id_type": "thread", "container_id": thread}))

    def message(self, message_id: str) -> list[FeishuMessage]:
        return self._call(f"/im/v1/messages/{_quote(message_id)}").get("items") or []

    def picture(self, message: str, key: str) -> Picture | None:
        try:
            got = self._call(f"/im/v1/messages/{_quote(message)}/resources/{_quote(key)}", {"type": "image"}, raw=True)
        except Exception:  # noqa: BLE001 - a picture that cannot be had is marked as missing
            return None
        return got if isinstance(got, Picture) else None

    def verify(self) -> None:
        self._tenant_token()

    def user_name(self, open_id: str) -> str | None:
        try:
            data = self._call(f"/contact/v3/users/{_quote(open_id)}", {"user_id_type": "open_id"})
            return ((data.get("user") or {}).get("name")) or None
        except Exception:  # noqa: BLE001 - a name that cannot be looked up is just not known
            return None


def bot_lark(app: App, sleep: Callable[[float], None] = time.sleep) -> BotLark:
    return BotLark(app, sleep)
