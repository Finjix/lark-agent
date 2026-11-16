"""Seals on records, and the refusal of any record that carries a credential (seal.ts)."""

from __future__ import annotations

import copy
import re
from typing import Any

from .util import digest, need

_SECRET_KEYS = re.compile(r"authorization|cookie|password|secret|(?:access|refresh|session|id)[_-]?token", re.IGNORECASE)
# JavaScript's \b is an ASCII word boundary: a Chinese character in front still starts a word.
_BEARER = re.compile(r"(?<![A-Za-z0-9_])Bearer\s+\S+", re.IGNORECASE)


def reject_secrets(value: Any) -> None:
    """Records are kept on disk and shown to people: they must never carry a credential."""
    if isinstance(value, list):
        for v in value:
            reject_secrets(v)
    elif isinstance(value, dict):
        need(not any(_SECRET_KEYS.search(str(k)) for k in value.keys()), "记录中禁止包含认证凭据")
        for v in value.values():
            reject_secrets(v)
    elif isinstance(value, str):
        need(not _BEARER.search(value), "记录中禁止包含认证凭据")


def seal(value: dict, key: str) -> dict:
    """Adds the digest of the record under `key`, so a hand-edited record is detected when it is read back."""
    result = copy.deepcopy(value)
    reject_secrets(result)
    result[key] = digest(result)
    return result


def check_seal(value: dict, key: str) -> None:
    need(isinstance(value, dict), "接口记录摘要不匹配")
    rest = {k: v for k, v in value.items() if k != key}
    need(value.get(key) == digest(rest), "接口记录摘要不匹配")
    reject_secrets(value)
