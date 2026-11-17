import unittest
from typing import Any, Callable

from audit_sdk.config import App
from audit_sdk.errors import Blocked
from audit_sdk.lark import BotLark, LarkError, explain_lark

from tests.helpers.fake_server import FakeServer, Reply, json_reply


class Feishu:
    """A Feishu that answers by path (the longest path that ends the request's); it counts token requests."""

    def __init__(self, routes: dict[str, Callable[[Any, int], Reply]], token_reply: Reply | None = None) -> None:
        self.routes = routes
        self.token_reply = token_reply
        self.tokens = 0
        self.counts: dict[str, int] = {}
        self.asked: list[Any] = []
        self.problems: list[str] = []
        self.server = FakeServer(self._handle)
        self.lark = BotLark(App(id="cli_bot", secret="secret-value-1", base=f"{self.server.url}/open-apis"), sleep=lambda _s: None)

    def _handle(self, request: Any) -> Reply:
        if request.path.endswith("/auth/v3/tenant_access_token/internal"):
            if self.token_reply:
                return self.token_reply
            self.tokens += 1
            if request.json() != {"app_id": "cli_bot", "app_secret": "secret-value-1"}:
                self.problems.append(f"token asked with {request.json()}")
            return json_reply({"code": 0, "tenant_access_token": f"t-{self.tokens}", "expire": 7200})
        if request.headers.get("authorization") != f"Bearer t-{self.tokens}":
            self.problems.append(f"authorization {request.headers.get('authorization')}")
        self.asked.append(request)
        route = max((p for p in self.routes if request.path.endswith(p)), key=len, default=None)
        if route is None:
            self.problems.append(f"unexpected {request.path}")
            return json_reply({"code": 404}, 404)
        n = self.counts.get(route, 0) + 1
        self.counts[route] = n
        return self.routes[route](request, n)

    def __enter__(self) -> "Feishu":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.server.close()


class LarkTest(unittest.TestCase):
    def test_the_history_is_read_page_by_page_oldest_first_within_the_seconds_asked_for_with_one_token(self) -> None:
        def messages(_request: Any, n: int) -> Reply:
            if n == 1:
                return json_reply({"code": 0, "data": {"items": [{"message_id": "om_1"}, {"message_id": "om_2"}], "has_more": True, "page_token": "p2"}})
            return json_reply({"code": 0, "data": {"items": [{"message_id": "om_3"}], "has_more": False}})

        with Feishu({"/im/v1/messages": messages}) as api:
            ids = [m["message_id"] for m in api.lark.history("oc_review", 100, 200)]
            self.assertEqual(ids, ["om_1", "om_2", "om_3"])
            self.assertEqual(api.asked[0].query, {"container_id_type": "chat", "container_id": "oc_review", "start_time": "100", "end_time": "200",
                                                  "sort_type": "ByCreateTimeAsc", "page_size": "50"})
            self.assertEqual(api.asked[1].query.get("page_token"), "p2")
            self.assertEqual(api.tokens, 1)
            self.assertEqual(api.problems, [])

    def test_an_expired_token_is_renewed_rate_limits_waited_out_and_refusals_explained(self) -> None:
        def messages(_request: Any, n: int) -> Reply:
            if n == 1:
                return json_reply({"code": 99991663, "msg": "token expired"})
            if n == 2:
                return json_reply({"code": 99991400, "msg": "too many"}, 429)
            return json_reply({"code": 0, "data": {"items": [], "has_more": False}})

        with Feishu({"/im/v1/messages": messages,
                     "/contact/v3/users/ou_1": lambda _r, _n: json_reply({"code": 0, "data": {"user": {"name": "王审核"}}}),
                     "/contact/v3/users/ou_2": lambda _r, _n: json_reply({"code": 41050, "msg": "no permission"})}) as api:
            list(api.lark.history("oc_1", 0, 1))
            self.assertEqual(api.tokens, 2)
            self.assertEqual(api.lark.user_name("ou_1"), "王审核")
            self.assertIsNone(api.lark.user_name("ou_2"), "a name that cannot be looked up is just not known")
            self.assertEqual(api.problems, [])

        denied = lambda _r, _n: json_reply({"code": 99991672, "msg": "Access denied", "error": {"permission_violations": [{"subject": "im:message.group_msg"}]}}, 400)
        with Feishu({"/im/v1/messages": denied}) as api:
            with self.assertRaises(LarkError) as caught:
                list(api.lark.history("oc_1", 0, 1))
            self.assertEqual(explain_lark(caught.exception), "The bot lacks a Feishu permission (im:message.group_msg): an administrator must grant it to the app on the Feishu open platform and publish a new version")
        self.assertRegex(explain_lark(LarkError(230002, "bot not in chat")), "not a member of this chat")

        with Feishu({}, token_reply=json_reply({"code": 10003, "msg": "invalid app"})) as api:
            with self.assertRaisesRegex(Blocked, r"credentials are not valid \(code 10003\)"):
                list(api.lark.history("oc_1", 0, 1))
            with self.assertRaisesRegex(Blocked, "credentials are not valid"):
                api.lark.verify()

    def test_pictures_come_back_as_bytes_a_forward_with_what_it_carries_threads_page_like_the_chat(self) -> None:
        def listing(request: Any, _n: int) -> Reply:
            return json_reply({"code": 0, "data": {"items": [{"message_id": f"reply-in-{request.query.get('container_id')}"}], "has_more": False}})

        with Feishu({
            "/resources/img_1": lambda _r, _n: Reply(body=b"png-bytes", content_type="image/png"),
            "/resources/img_gone": lambda _r, _n: json_reply({"code": 234003, "msg": "resource not found"}, 400),
            "/im/v1/messages/om_fwd": lambda _r, _n: json_reply({"code": 0, "data": {"items": [{"message_id": "om_fwd"}, {"message_id": "om_in", "upper_message_id": "om_fwd"}]}}),
            "/im/v1/messages": listing,
        }) as api:
            picture = api.lark.picture("om_9", "img_1")
            self.assertIsNotNone(picture)
            assert picture is not None
            self.assertEqual((picture.mime, picture.data), ("image/png", b"png-bytes"))
            self.assertEqual(api.asked[-1].query.get("type"), "image")
            self.assertIsNone(api.lark.picture("om_9", "img_gone"))
            self.assertEqual([m["message_id"] for m in api.lark.message("om_fwd")], ["om_fwd", "om_in"])
            self.assertEqual([m["message_id"] for m in api.lark.thread("omt_1")], ["reply-in-omt_1"])
            self.assertEqual(api.asked[-1].query.get("container_id_type"), "thread")
            self.assertEqual(api.problems, [])


if __name__ == "__main__":
    unittest.main()
