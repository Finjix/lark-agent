"""HTTP to Feishu, standing in for the @larksuiteoapi/node-sdk client of the original.

Only what the SDK was used for: JSON requests with a user access token, form posts to the accounts host, and the refresh of a user token at
the unified OAuth endpoint. Nothing is logged (requests carry credentials, and stdout carries the command's JSON result), and a tenant token
is never asked for.
"""

from __future__ import annotations

import http.client
import json
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import quote, urlencode, urlsplit

from .jsutil import get, is_number, js_string

DEFAULT_TIMEOUT_S = 30.0
USER_AGENT = "feishu-skill/1.0 (python)"
_LOOPBACK = {"127.0.0.1", "localhost", "::1"}


class NoResponse(Exception):
    """Nothing came back: the connection failed, was cut, or timed out."""


class HttpError(Exception):
    """The server answered with a status outside 2xx; `body` is its parsed JSON, or its text, or {} for a stream."""

    def __init__(self, status: int, body: Any) -> None:
        super().__init__(f"HTTP {status}")
        self.status = status
        self.body = body


_direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_default = urllib.request.build_opener()


def opener_for(url: str) -> urllib.request.OpenerDirector:
    """The browser's debugging port and a local test server are reached directly, whatever proxy the environment names."""
    host = (urlsplit(url).hostname or "").lower()
    return _direct if host in _LOOPBACK else _default


def query_string(params: Mapping[str, Any]) -> str:
    return urlencode({key: js_string(value) for key, value in params.items()}, quote_via=quote, safe=":,!$")


def _parse(raw: bytes) -> Any:
    text = raw.decode("utf-8", errors="replace")
    try:
        return json.loads(text)
    except ValueError:
        return text


@dataclass
class Response:
    status: int
    body: Any


def request(method: str, url: str, *, params: Mapping[str, Any] | None = None, json_body: Any = None, form: Mapping[str, str] | None = None,
            headers: Mapping[str, str] | None = None, timeout: float = DEFAULT_TIMEOUT_S, stream: bool = False) -> Any:
    """One request. A 2xx answer returns its parsed body (or, with `stream`, the open response to read and close);
    any other status raises HttpError; no answer raises NoResponse."""
    if params:
        url = f"{url}{'&' if '?' in url else '?'}{query_string(params)}"
    data: bytes | None = None
    sent = {"User-Agent": USER_AGENT, **(headers or {})}
    if form is not None:
        data = urlencode(form).encode("utf-8")
        sent.setdefault("Content-Type", "application/x-www-form-urlencoded")
    elif json_body is not None:
        data = json.dumps(json_body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        sent.setdefault("Content-Type", "application/json; charset=utf-8")
    req = urllib.request.Request(url, data=data, method=method.upper(), headers=sent)
    try:
        response = opener_for(url).open(req, timeout=timeout)
    except urllib.error.HTTPError as error:
        try:
            raw = b"" if stream else error.read()
        except (OSError, http.client.HTTPException):
            raw = b""
        finally:
            error.close()
        raise HttpError(error.code, {} if stream else _parse(raw)) from None
    except (urllib.error.URLError, OSError, http.client.HTTPException, socket.timeout, ValueError) as error:
        raise NoResponse(str(error)) from None
    if stream:
        return response
    try:
        with response:
            raw = response.read()
    except (OSError, http.client.HTTPException) as error:
        raise NoResponse(str(error)) from None
    return _parse(raw)


class AccessTokenError(Exception):
    """A non-200 (or token-less) answer of /oauth/v3/token, as the SDK reported it; status 0 when nothing came back."""

    def __init__(self, status: int, code: int, error: str, description: str) -> None:
        super().__init__(description or error or "access token request failed")
        self.status = status
        self.code = code
        self.error = error
        self.description = description


def _is_error_response(body: Any) -> bool:
    if not body:
        return True
    if get(body, "error"):
        return True
    code = get(body, "code")
    return code is not None and code != 0 and code != "0" and code != ""


def _nonempty(value: Any) -> Any:
    return None if value is None or value == "" else value


def refresh_user_token(accounts: str, app_id: str, app_secret: str, refresh_token: str) -> dict[str, Any]:
    """Renews a user access token the way the SDK's accessToken.refresh does: one JSON post to the accounts host."""
    body = {"client_id": app_id, "grant_type": "refresh_token", "refresh_token": refresh_token, "client_secret": app_secret}
    status = 200
    try:
        reply = request("POST", f"{accounts}/oauth/v3/token", json_body=body)
    except HttpError as error:
        status, reply = error.status, error.body
    except NoResponse as error:
        # Never the raw error: it is about a request that carried the client secret and the refresh token.
        raise AccessTokenError(0, 0, "", str(error) or "oauth token request failed") from None

    def fail(fallback: str = "") -> AccessTokenError:
        code = get(reply, "code")
        return AccessTokenError(status, code if is_number(code) and code else 0, js_string(get(reply, "error") or ""),
                                js_string(get(reply, "error_description") or get(reply, "msg") or fallback))

    if status != 200 or _is_error_response(reply):
        raise fail()
    access = _nonempty(get(reply, "access_token"))
    if not access:
        raise fail("oauth token response missing access_token")
    return {
        "access_token": access, "token_type": _nonempty(get(reply, "token_type")), "expires_in": _nonempty(get(reply, "expires_in")),
        "refresh_token": _nonempty(get(reply, "refresh_token")), "refresh_token_expires_in": _nonempty(get(reply, "refresh_token_expires_in")),
        "scope": _nonempty(get(reply, "scope")),
    }
