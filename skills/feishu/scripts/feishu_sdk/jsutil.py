"""Small helpers that keep the port's behaviour where the TypeScript original leaned on JavaScript semantics.

The bridge and the command line take JSON from other programs and pass it on as JavaScript would have: `Number(x)`, `String(x)`,
`typeof x === 'number'`, `JSON.stringify`, `String.prototype.trim`. Doing the same here keeps the answers the callers already expect.
"""

from __future__ import annotations

import json
import math
import re
import time
from typing import Any
from urllib.parse import SplitResult, urlsplit

# What JavaScript's String.prototype.trim removes (WhiteSpace and LineTerminator, including the byte order mark).
_JS_SPACE = "\u0009\u000a\u000b\u000c\u000d\u0020\u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
_DECIMAL = re.compile(r"[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?")
_RADIX = re.compile(r"0([xXoObB])([0-9a-fA-F]+)")
MAX_SAFE_INTEGER = 2**53 - 1


def now_ms() -> int:
    """Milliseconds since the epoch, as Date.now() gives them (an integer: the stored login keeps integers)."""
    return int(time.time() * 1000)


def js_trim(text: str) -> str:
    return text.strip(_JS_SPACE)


def is_number(value: Any) -> bool:
    """`typeof value === 'number'`: a bool is not a number here, although Python counts it as one."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def num(value: Any) -> float:
    """A stored number as it is, anything else as NaN, so that comparisons with it are false as they were in JavaScript."""
    return float(value) if is_number(value) else math.nan


def js_number(value: Any) -> float:
    """`Number(value)` for a JSON value or a command line string."""
    if value is None:
        return 0.0
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = js_trim(value)
        if text == "":
            return 0.0
        if _DECIMAL.fullmatch(text):
            return float(text)
        if text in ("Infinity", "+Infinity"):
            return math.inf
        if text == "-Infinity":
            return -math.inf
        radix = _RADIX.fullmatch(text)
        if radix:
            base = {"x": 16, "o": 8, "b": 2}[radix.group(1).lower()]
            try:
                return float(int(radix.group(2), base))
            except ValueError:
                return math.nan
        return math.nan
    if isinstance(value, list):
        if not value:
            return 0.0
        if len(value) == 1:
            return js_number(js_string(value[0]) if not isinstance(value[0], list) else value[0])
        return math.nan
    return math.nan


def is_integer(value: float) -> bool:
    """`Number.isInteger`."""
    return math.isfinite(value) and float(value).is_integer()


def is_safe_integer(value: Any) -> bool:
    """`Number.isSafeInteger` of a value that is already a number (a bool or a string is not)."""
    return is_number(value) and is_integer(float(value)) and abs(value) <= MAX_SAFE_INTEGER


def js_string(value: Any) -> str:
    """`String(value)`."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if math.isnan(value):
            return "NaN"
        if math.isinf(value):
            return "Infinity" if value > 0 else "-Infinity"
        if value.is_integer() and abs(value) < 1e21:
            return str(int(value))
        return repr(value)
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return ",".join("" if item is None else js_string(item) for item in value)
    if isinstance(value, dict):
        return "[object Object]"
    return str(value)


def as_int_if_whole(value: float) -> int | float:
    """A computed number written to disk or to JSON as JavaScript would print it: 3000, not 3000.0."""
    return int(value) if is_integer(value) else value


def dumps(value: Any) -> str:
    """`JSON.stringify(value)`: compact, non-ASCII text kept as it is."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _no_constants(name: str) -> Any:
    raise ValueError(f"not JSON: {name}")


def loads(text: str) -> Any:
    """`JSON.parse(text)`: NaN and Infinity are not JSON."""
    return json.loads(text, parse_constant=_no_constants)


def get(value: Any, key: str) -> Any:
    """`value?.[key]` for a parsed JSON value that may not be an object."""
    return value.get(key) if isinstance(value, dict) else None


_SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*")
_SPECIAL = {"http", "https", "ws", "wss", "ftp", "file"}


def parse_url(raw: str) -> SplitResult | None:
    """`new URL(raw)`, roughly: None where the WHATWG parser would throw (no scheme, or a web address without a host)."""
    try:
        parts = urlsplit(raw.strip())
        if not parts.scheme or not _SCHEME.fullmatch(parts.scheme):
            return None
        if parts.scheme.lower() in _SPECIAL and parts.scheme.lower() != "file" and not parts.hostname:
            return None
        _ = parts.port  # raises for a port that is not a number
        return parts
    except ValueError:
        return None


def host_of(parts: SplitResult) -> str:
    """`url.host`: the host name with its port, without user information."""
    host = parts.hostname or ""
    try:
        port = parts.port
    except ValueError:
        port = None
    return f"{host}:{port}" if port is not None else host


def truthy(value: Any) -> bool:
    """JavaScript truthiness: an empty object or list is true there, unlike in Python."""
    if value is None or value is False:
        return False
    if isinstance(value, (dict, list)):
        return True
    if is_number(value):
        return value != 0 and not math.isnan(value)
    if isinstance(value, str):
        return value != ""
    return bool(value)
