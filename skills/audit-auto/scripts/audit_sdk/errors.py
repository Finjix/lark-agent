"""Refusals and redaction shared by every module."""

from __future__ import annotations

import re
from typing import Iterable


class Blocked(Exception):
    """A refusal: the command cannot do what was asked, for a reason the agent can pass on to the user. The command line prints it as
    {"status":"blocked","reason":…} and exits with 2."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.message = reason


def need(condition: object, reason: str) -> None:
    """Raises Blocked(reason) unless `condition` holds."""
    if not condition:
        raise Blocked(reason)


_BEARER = re.compile(r"(Bearer\s+)[A-Za-z0-9._~+/=-]+", re.IGNORECASE)
_KEYED = re.compile(r"((?:x-goog-api-key|api[_-]?key|app_secret|tenant_access_token)[\"']?\s*[:=]\s*[\"']?)[^\"'\s,}]+", re.IGNORECASE)


def redact(text: str, secrets: Iterable[str] = ()) -> str:
    """Removes credentials from text that is about to leave the process (stdout, a host's logs)."""
    out = _KEYED.sub(r"\1[REDACTED]", _BEARER.sub(r"\1[REDACTED]", text))
    for secret in secrets:
        if secret and len(secret) >= 6:
            out = out.replace(secret, "[REDACTED]")
    return out


def message_of(error: object) -> str:
    if isinstance(error, Blocked):
        return error.message
    if isinstance(error, BaseException):
        return str(error) or type(error).__name__
    return str(error)
