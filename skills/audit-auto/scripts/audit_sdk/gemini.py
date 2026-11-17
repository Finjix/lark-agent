"""Gemini through the internal AI gateway.

Over its REST API with plain HTTP (one endpoint is all it takes): generateContent under /v1/models, the key as x-goog-api-key. The
gateway has no Files API, so media travels inline. Every call here asks for one JSON answer in JSON mode, its shape described in the
prompt. Not with a response schema: held to one, gemini-3.8-flash got stuck repeating itself inside a long Chinese string and dropped
most of the answer (tried 2026-11-02; the gateway also refuses maxItems in a schema), while JSON mode gave the whole answer. So callers
read the answer leniently. Safety filters are off: what is reviewed is exactly the material that may break the rules (blood, gambling
machines, revealing clothes), and a filtered answer would hide the very problem.
"""

from __future__ import annotations

import http.client
import json
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, Sequence

from .config import Gateway
from .errors import Blocked, message_of, redact

#: A part of the Gemini API's Content: {"text": …}, {"inlineData": {"mimeType", "data"}, "videoMetadata": {"fps"}}.
Part = dict[str, Any]


@dataclass
class JsonRequest:
    """maxOutputTokens bounds an answer that runs away."""
    system: str
    parts: list[Part]
    media_resolution: str | None = None
    max_output_tokens: int | None = None


@dataclass
class Usage:
    input: int = 0
    output: int = 0
    thoughts: int = 0

    def as_dict(self) -> dict[str, int]:
        return {"input": self.input, "output": self.output, "thoughts": self.thoughts}


@dataclass
class JsonReply:
    data: Any
    usage: Usage = field(default_factory=Usage)
    ms: int = 0
    attempts: int = 1


class Model(Protocol):
    """What the skill needs from a model: one answer, as JSON. Tests stand in for it."""
    name: str

    def json(self, request: JsonRequest) -> JsonReply: ...


class InvalidAnswer(Exception):
    """An answer that came back unusable (cut off, not JSON, no candidate): asking again usually helps."""


class HttpError(Exception):
    """The gateway answered with an HTTP error; the message is the gateway's own."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class GatewayDown(Blocked):
    """The gateway is down, busy or silent right now: the same request may well go through later (401, 400, a filtered answer will not)."""


class GatewayRefused(Blocked):
    """The gateway refuses every request as it is set up now (the key, the model's name): nothing in this request is at fault."""


RETRY_STATUS = {408, 429, 500, 502, 503, 504}
NETWORK = re.compile(r"fetch failed|ECONNRESET|ECONNREFUSED|ENOTFOUND|EAI_AGAIN|EPIPE|socket hang up|other side closed|"
                     r"Connection(Reset|Refused|Aborted)|RemoteDisconnected|getaddrinfo|Name or service|Remote end closed|"
                     r"BrokenPipe|IncompleteRead|forcibly closed|actively refused", re.IGNORECASE)
#: No answer within the timeout: asked again, it would only make the user wait as long once more.
SILENT = re.compile(r"abort|time ?out|timed out|ETIMEDOUT", re.IGNORECASE)
FILTERED = {"SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "IMAGE_SAFETY", "IMAGE_PROHIBITED_CONTENT"}
SAFETY_OFF = [{"category": c, "threshold": "OFF"} for c in
              ("HARM_CATEGORY_HARASSMENT", "HARM_CATEGORY_HATE_SPEECH", "HARM_CATEGORY_SEXUALLY_EXPLICIT", "HARM_CATEGORY_DANGEROUS_CONTENT")]


class GatewayModel:
    """Gemini on the gateway. `delays` and `sleep` are the test seams for the waits between attempts."""

    def __init__(self, gw: Gateway, delays: Sequence[float] = (5.0, 20.0), sleep: Callable[[float], None] = time.sleep,
                 timeout: float = 10 * 60) -> None:
        self.gateway = gw
        self.name = gw.model
        self.url = f"{gw.base_url}/{gw.api_version}/models/{urllib.parse.quote(gw.model, safe='')}:generateContent"
        self.delays = list(delays)
        self.sleep = sleep
        self.timeout = timeout

    def json(self, request: JsonRequest) -> JsonReply:
        started = time.monotonic()
        config: dict[str, Any] = {"thinkingConfig": {"thinkingLevel": self.gateway.thinking_level}}
        if request.media_resolution:
            config["mediaResolution"] = request.media_resolution
        if request.max_output_tokens:
            config["maxOutputTokens"] = request.max_output_tokens
        config["responseMimeType"] = "application/json"
        body = json.dumps({
            "contents": [{"role": "user", "parts": request.parts}],
            "systemInstruction": {"parts": [{"text": request.system}]},
            "generationConfig": config,
            "safetySettings": SAFETY_OFF,
        }, ensure_ascii=False).encode("utf-8")
        attempt = 0
        while True:
            attempt += 1
            try:
                response = self._call(body)
                return JsonReply(data=answer_of(response), usage=usage_of(response), ms=int((time.monotonic() - started) * 1000), attempts=attempt)
            except Blocked:
                raise
            except Exception as error:  # noqa: BLE001 - every other failure is classified below
                if not retryable(error) or attempt > len(self.delays):
                    raise explain(error, self.gateway) from None
                self.sleep(self.delays[attempt - 1])

    def _call(self, body: bytes) -> dict[str, Any]:
        """One generateContent call; the body is read within the same timeout, since a large answer can be slow to arrive."""
        request = urllib.request.Request(self.url, data=body, method="POST",
                                         headers={"content-type": "application/json", "x-goog-api-key": self.gateway.key})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                text = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as error:
            text = error.read().decode("utf-8", errors="replace") if error.fp else ""
            message = text
            try:
                parsed = json.loads(text)
                found = parsed.get("error", {}).get("message") if isinstance(parsed, dict) and isinstance(parsed.get("error"), dict) else None
                message = found if found is not None else text
            except ValueError:
                pass
            raise HttpError(error.code, message or str(error.reason or "")) from None
        try:
            parsed = json.loads(text)
        except ValueError:
            raise InvalidAnswer("the reply is not JSON") from None
        return parsed if isinstance(parsed, dict) else {}


_FENCED = re.compile(r"^```(?:json)?\s*([\s\S]*?)\s*```$")


def answer_of(response: dict[str, Any]) -> Any:
    """The JSON of an answer; a filtered one is a refusal the user is told about, a broken one is asked for again."""
    block = (response.get("promptFeedback") or {}).get("blockReason")
    if block:
        raise Blocked(f"The AI's safety filter blocked the request ({block}): such content will most likely not pass review; check it by hand")
    candidates = response.get("candidates") or []
    if not candidates or not isinstance(candidates[0], dict):
        raise InvalidAnswer("the answer has no candidate")
    candidate = candidates[0]
    finish = str(candidate.get("finishReason") or "")
    if finish in FILTERED:
        raise Blocked(f"The AI's safety filter blocked the answer ({finish}): such content will most likely not pass review; check it by hand")
    if finish == "MAX_TOKENS":
        raise InvalidAnswer("the answer was cut off")
    parts = (candidate.get("content") or {}).get("parts") or []
    text = "".join(p["text"] for p in parts if isinstance(p, dict) and not p.get("thought") and isinstance(p.get("text"), str)).strip()
    # JSON mode answers bare JSON; a fenced block is taken too.
    fenced = _FENCED.match(text)
    bare = fenced.group(1) if fenced else text
    try:
        return json.loads(bare)
    except ValueError:
        raise InvalidAnswer("the answer is not JSON") from None


def usage_of(response: dict[str, Any]) -> Usage:
    meta = response.get("usageMetadata") or {}
    return Usage(input=meta.get("promptTokenCount") or 0, output=meta.get("candidatesTokenCount") or 0, thoughts=meta.get("thoughtsTokenCount") or 0)


def _describe(error: BaseException) -> str:
    """The error, its cause and its type, the way the patterns above look for them."""
    reason = getattr(error, "reason", None)
    cause = error.__cause__ or error.__context__
    return " ".join(str(x) for x in (type(error).__name__, message_of(error), reason or "", type(reason).__name__ if reason else "",
                                     type(cause).__name__ if cause else "", message_of(cause) if cause else ""))


def _silent(error: BaseException) -> bool:
    return isinstance(error, (TimeoutError, socket.timeout)) or isinstance(getattr(error, "reason", None), (TimeoutError, socket.timeout)) \
        or bool(SILENT.search(_describe(error)))


def retryable(error: BaseException) -> bool:
    if isinstance(error, InvalidAnswer):
        return True
    if isinstance(error, HttpError):
        return error.status in RETRY_STATUS
    if _silent(error):
        return False
    if isinstance(error, (ConnectionError, http.client.RemoteDisconnected, http.client.IncompleteRead, http.client.BadStatusLine)):
        return True
    if isinstance(error, urllib.error.URLError) and isinstance(error.reason, (ConnectionError, socket.gaierror, OSError)):
        return True
    return bool(NETWORK.search(_describe(error)))


def explain(error: BaseException, gw: Gateway) -> Blocked:
    """A failure the user (or the administrator) can act on, without the key."""
    if isinstance(error, InvalidAnswer):
        return Blocked("The AI did not give a complete answer: try again")
    if isinstance(error, HttpError):
        detail = redact(message_of(error), [gw.key])[:300]
        status = error.status
        if status in (401, 403):
            return GatewayRefused(f"The AI gateway does not accept the key (HTTP {status}): ask the administrator")
        if status == 404:
            return GatewayRefused(f"The AI gateway has no model {gw.model} (HTTP 404): ask the administrator")
        if status == 408:
            return GatewayDown("The AI gateway timed out (HTTP 408): try again later")
        if status == 413:
            return Blocked("The file is too large for the AI gateway (HTTP 413): compress it and send it again")
        if status == 429:
            return GatewayDown("The AI gateway is rate-limiting (HTTP 429): try again in a few minutes")
        if status >= 500:
            return GatewayDown(f"The AI gateway is unavailable (HTTP {status}): try again later")
        return Blocked(f"The AI gateway refused the request (HTTP {status}): {detail}")
    if _silent(error):
        return GatewayDown("The AI gateway did not answer for 10 minutes: try again later (compress a very large video first)")
    if retryable(error):
        return GatewayDown("Cannot reach the AI gateway: try again later")
    return Blocked(f"Calling the AI gateway failed: {redact(message_of(error), [gw.key])[:300]}")
