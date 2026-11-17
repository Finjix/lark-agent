"""Small helpers that keep numbers and text as the skill's original TypeScript version wrote them (the JSON files in the shared
folder keep that format)."""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

#: China time. China has kept UTC+8 without daylight saving since 1991, so a fixed offset is exact for every date this skill sees
#: (and needs no tz database, which Windows Python lacks).
SHANGHAI = timezone(timedelta(hours=8))


def now_ms() -> int:
    """Milliseconds since the epoch, as Date.now()."""
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def js_round(value: float) -> int:
    """Math.round: halves go up."""
    return math.floor(value + 0.5)


def to_fixed(value: float, digits: int) -> str:
    """Number.prototype.toFixed: the exact binary value rounded half up."""
    quantum = Decimal(1).scaleb(-digits)
    return str(Decimal(value).quantize(quantum, rounding=ROUND_HALF_UP))


def num(value: float) -> float | int:
    """A number as JSON.stringify writes it: 11, not 11.0."""
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def js_str(value: Any) -> str:
    """String(value ?? '')."""
    if value is None:
        return ""
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, float):
        return str(num(value))
    return str(value)


def js_number(value: Any) -> float:
    """Number(value): NaN when it is not a number."""
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    if value is None:
        return math.nan
    try:
        text = str(value).strip()
        return 0.0 if text == "" else float(text)
    except ValueError:
        return math.nan


def stamp(ms: float) -> str:
    """China time: 2026-11-02 16:05."""
    return datetime.fromtimestamp(ms / 1000, SHANGHAI).strftime("%Y-%m-%d %H:%M")


def day(ms: float) -> str:
    return stamp(ms)[:10]


def iso_now_name() -> str:
    """new Date().toISOString() with ':' and '.' replaced by '-' (a file name that sorts by time)."""
    at = datetime.now(timezone.utc)
    return at.strftime("%Y-%m-%dT%H-%M-%S-") + f"{at.microsecond // 1000:03d}Z"
