"""Failure categories, the redaction of credentials, and the refusal of a command (errors.ts)."""

from __future__ import annotations

import re
from typing import Iterable, Literal, NoReturn, TypedDict

# Failure categories the callers (the bridge and requirement-auto's command line) branch on; the message is for people.
ErrorKind = Literal[
    "config",  # app credentials or data directory missing / invalid
    "auth_required",  # not logged in, or the login expired
    "login_denied",  # the user refused the authorization
    "login_expired",  # the pending device authorization ran out
    "identity_mismatch",  # the authorizing account is not the user this process serves
    "scope_missing",  # the token lacks a permission the call needs
    "invalid_request",  # refused locally before anything was sent
    "occupied",  # the server refused to overwrite non-empty cells
    "unknown_result",  # a write may or may not have been applied: read back, never resend
    "network",  # nothing reached the server (or no answer came back from a read)
    "api",  # the server answered with an error
]


class BridgeError(Exception):
    """A failure with its category; `code` is Feishu's error code when Feishu gave one."""

    def __init__(self, kind: ErrorKind, message: str, code: int | None = None) -> None:
        super().__init__(message)
        self.kind: ErrorKind = kind
        self.message = message
        self.code = code


class _Payload(TypedDict):
    kind: ErrorKind
    message: str


class ErrorPayload(_Payload, total=False):
    code: int


_BEARER = re.compile(r"(Bearer\s+)[A-Za-z0-9._~+/=-]+", re.IGNORECASE)
_USER_TOKEN = re.compile(r"\b([uc]-[A-Za-z0-9_-]{20,})\b")


def redact(text: str, secrets: Iterable[str] = ()) -> str:
    """Removes credentials from text that is about to leave the process (tool results, stdout, logs)."""
    out = _BEARER.sub(r"\1[REDACTED]", text)
    out = _USER_TOKEN.sub("[REDACTED]", out)
    for secret in secrets:
        if secret and len(secret) >= 6:
            out = out.replace(secret, "[REDACTED]")
    return out


def to_payload(error: BaseException, secrets: Iterable[str] = ()) -> ErrorPayload:
    secrets = list(secrets)
    if isinstance(error, BridgeError):
        payload: ErrorPayload = {"kind": error.kind, "message": redact(error.message, secrets)}
        if error.code is not None:
            payload["code"] = error.code
        return payload
    message = str(error) if isinstance(error, Exception) else "未知错误"
    return {"kind": "api", "message": redact(message, secrets)}


class Blocked(Exception):
    """A command of the command line refused for a reason that is told as it is: it prints {"status":"blocked","reason":…} and exits with 2."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def need(condition: object, message: str) -> None:
    if not condition:
        raise Blocked(message)


def blocked(message: str) -> NoReturn:
    raise Blocked(message)
