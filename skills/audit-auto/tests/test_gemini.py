import socket
import unittest
import urllib.error
from typing import Any, Callable

from audit_sdk.config import Gateway
from audit_sdk.errors import Blocked
from audit_sdk.gemini import GatewayDown, GatewayModel, GatewayRefused, JsonRequest, explain, retryable

from tests.helpers.fake_server import FakeServer, Reply, json_reply


def answer(text: str, finish: str = "STOP") -> Reply:
    return json_reply({"candidates": [{"content": {"role": "model", "parts": [{"text": "thinking…", "thought": True}, {"text": text}]},
                                       "finishReason": finish}],
                       "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 2, "thoughtsTokenCount": 5}})


class Gemini:
    """A gateway that answers from a list of replies, and a model on it that waits as little as the test says."""

    def __init__(self, replies: list[Callable[[], Reply]], timeout: float = 5) -> None:
        self.replies = list(replies)
        self.server = FakeServer(self._handle)
        self.waits: list[float] = []
        gw = Gateway(base_url=f"{self.server.url}/winky/gemini", api_version="v1", model="gemini-3.8-flash", thinking_level="MEDIUM",
                     key="sk-test-key-123")
        self.model = GatewayModel(gw, delays=[0.005, 0.02], sleep=self.waits.append, timeout=timeout)

    def _handle(self, _request: Any) -> Reply:
        return self.replies.pop(0)() if self.replies else Reply(status=599, body=b"no more replies")

    @property
    def sent(self) -> list[Any]:
        return self.server.requests

    def ask(self) -> Any:
        return self.model.json(JsonRequest(system="", parts=[{"text": "x"}]))

    def error(self) -> BaseException:
        try:
            self.ask()
        except BaseException as error:  # noqa: BLE001
            return error
        raise AssertionError("not refused")

    def __enter__(self) -> "Gemini":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.server.close()


class GeminiTest(unittest.TestCase):
    def test_a_call_goes_to_generate_content_with_the_key_medium_thinking_json_mode_and_no_safety_filtering(self) -> None:
        with Gemini([lambda: answer('{"ok":true}')]) as g:
            reply = g.model.json(JsonRequest(system="Review.", parts=[{"inlineData": {"mimeType": "video/mp4", "data": "AAAA"}, "videoMetadata": {"fps": 4}},
                                                                     {"text": "File name: a.mp4"}], media_resolution="MEDIA_RESOLUTION_HIGH"))
            self.assertEqual(reply.data, {"ok": True}, "the thought is not part of the answer")
            self.assertEqual(reply.usage.as_dict(), {"input": 10, "output": 2, "thoughts": 5})
            call = g.sent[0]
            self.assertEqual(call.path, "/winky/gemini/v1/models/gemini-3.8-flash:generateContent")
            self.assertEqual(call.headers["x-goog-api-key"], "sk-test-key-123")
            body = call.json()
            self.assertEqual(body["systemInstruction"]["parts"], [{"text": "Review."}])
            self.assertEqual(body["contents"][0]["parts"][0]["videoMetadata"], {"fps": 4})
            config = body["generationConfig"]
            self.assertEqual(config["thinkingConfig"]["thinkingLevel"], "MEDIUM")
            self.assertEqual(config["responseMimeType"], "application/json")
            self.assertNotIn("responseJsonSchema", config, "JSON mode, no schema (see gemini.py)")
            self.assertEqual(config["mediaResolution"], "MEDIA_RESOLUTION_HIGH")
            self.assertEqual(len(body["safetySettings"]), 4)
            self.assertTrue(all(s["threshold"] == "OFF" for s in body["safetySettings"]))

    def test_a_busy_gateway_a_broken_answer_and_the_network_are_tried_again_the_rest_is_told_plainly(self) -> None:
        with Gemini([lambda: json_reply({"error": {"code": 503, "message": "overloaded"}}, 503), lambda: answer('{"ok":tru'),
                     lambda: answer('{"ok":true}')]) as busy:
            self.assertEqual(busy.ask().attempts, 3)
            self.assertEqual(busy.waits, [0.005, 0.02])
        with Gemini([lambda: json_reply({}, 503)] * 3) as tired:
            self.assertRegex(str(tired.error()), r"AI 网关暂时不可用（HTTP 503）")
        with Gemini([lambda: json_reply({"error": {"code": 400, "message": "Request payload size exceeds the limit (key sk-test-key-123)"}}, 400)]) as refused:
            error = refused.error()
            self.assertRegex(str(error), r"AI 网关拒绝了这次请求（HTTP 400）")
            self.assertNotIn("sk-test-key-123", str(error))
            self.assertEqual(len(refused.sent), 1, "a refusal is not sent again")
        with Gemini([lambda: json_reply({}, 401)]) as g:
            self.assertRegex(str(g.error()), "不接受这个 key")
        with Gemini([lambda: json_reply({}, 404)]) as g:
            self.assertRegex(str(g.error()), r"找不到模型 gemini-3\.8-flash")
        # A key or model the gateway refuses is how it is set up, not this request; a gateway that timed out the request is only slow now.
        with Gemini([lambda: json_reply({}, 403)]) as g:
            self.assertIsInstance(g.error(), GatewayRefused)
        with Gemini([lambda: json_reply({}, 408)] * 3) as g:
            late = g.error()
            self.assertIsInstance(late, GatewayDown)
            self.assertIn("HTTP 408", str(late))
        with Gemini([lambda: answer("", "PROHIBITED_CONTENT")]) as g:
            self.assertRegex(str(g.error()), "安全策略拦下了这次回答")
        with Gemini([lambda: json_reply({"promptFeedback": {"blockReason": "OTHER"}})]) as g:
            self.assertRegex(str(g.error()), "安全策略拦下了这次请求")
        with Gemini([lambda: answer("x", "MAX_TOKENS")] * 3) as g:
            self.assertRegex(str(g.error()), "没有给出完整的结果")
        with Gemini([lambda: Reply(drop=True), lambda: answer('{"ok":true}')]) as offline:
            self.assertEqual(offline.ask().data, {"ok": True}, "a dropped connection is tried again")
        with Gemini([lambda: Reply(delay=1.0, body=b"{}"), lambda: answer('{"ok":true}')], timeout=0.2) as silent:
            error = silent.error()
            self.assertIsInstance(error, GatewayDown)
            self.assertRegex(str(error), "10 分钟没有回应")
            self.assertEqual(len(silent.sent), 1, "a gateway that did not answer in time is not asked again")
        with Gemini([lambda: answer('```json\n[{"op": "add"}]\n```')]) as fenced:
            self.assertEqual(fenced.ask().data, [{"op": "add"}], "a fenced answer is taken too")

    def test_network_failures_are_told_apart_from_a_silent_gateway(self) -> None:
        gw = Gateway(base_url="http://127.0.0.1:9", api_version="v1", model="m", thinking_level="MEDIUM", key="k")
        refused = urllib.error.URLError(ConnectionRefusedError(10061, "No connection could be made because the target machine actively refused it"))
        self.assertTrue(retryable(refused))
        self.assertIsInstance(explain(refused, gw), GatewayDown)
        self.assertEqual(explain(refused, gw).message, "连不上 AI 网关，稍后再试")
        unknown = urllib.error.URLError(socket.gaierror(11001, "getaddrinfo failed"))
        self.assertEqual(explain(unknown, gw).message, "连不上 AI 网关，稍后再试")
        silent = urllib.error.URLError(TimeoutError("timed out"))
        self.assertFalse(retryable(silent))
        self.assertRegex(explain(silent, gw).message, "10 分钟没有回应")
        self.assertRegex(explain(ValueError("odd"), gw).message, "^调用 AI 网关失败：odd")
        self.assertNotIsInstance(explain(ValueError("odd"), gw), GatewayDown)
        self.assertIsInstance(explain(ValueError("odd"), gw), Blocked)


if __name__ == "__main__":
    unittest.main()
