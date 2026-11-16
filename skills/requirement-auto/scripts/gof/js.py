"""JavaScript semantics the TypeScript original relied on, kept exact where records depend on them.

Every digest and seal in a batch is computed over canonical JSON as JavaScript writes it (numbers like ``String(n)``, strings like
``JSON.stringify``), and the batches already on disk were written by the TypeScript version: this module is what keeps the two in
agreement byte for byte. It also holds the few other places where JavaScript and Python differ in ways that matter here (``Number()``
of a string, ISO times with milliseconds, UTF-16 lengths, a value that is "undefined").
"""

from __future__ import annotations

import json
import math
import re
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any


class _Undefined:
    """A value that JavaScript would leave out of an object (``undefined``): dropped from JSON objects, ``null`` in arrays."""

    _instance: "_Undefined | None" = None

    def __new__(cls) -> "_Undefined":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "undefined"

    def __bool__(self) -> bool:
        return False


UNDEFINED = _Undefined()


def truthy(value: Any) -> bool:
    """JavaScript's truthiness: an empty object or list is true, 0, NaN, '' and null are not."""
    if value is None or value is UNDEFINED or value is False:
        return False
    if isinstance(value, bool):
        return True
    if isinstance(value, (int, float)):
        return value == value and value != 0
    if isinstance(value, str):
        return value != ""
    return True


def is_num(value: Any) -> bool:
    """``typeof value === 'number'``: a bool is no number here, as it is in JavaScript."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def is_int(value: Any) -> bool:
    """``Number.isInteger``: 2.0 counts, as JSON cannot tell it from 2."""
    if not is_num(value):
        return False
    if isinstance(value, int):
        return True
    return math.isfinite(value) and float(value).is_integer()


MAX_SAFE = 2 ** 53 - 1


def is_safe_int(value: Any) -> bool:
    """``Number.isSafeInteger``."""
    return is_int(value) and abs(value) <= MAX_SAFE


def as_int(value: Any) -> Any:
    """An integral float as an int (2.0 → 2); anything else as it is."""
    if isinstance(value, float) and is_int(value):
        return int(value)
    return value


# The whitespace String.prototype.trim and Number() strip.
JS_SPACE = "\t\n\v\f\r \u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
WS = "[" + re.escape(JS_SPACE) + "]"


def trim(text: str) -> str:
    return text.strip(JS_SPACE)


def trim_start(text: str) -> str:
    return text.lstrip(JS_SPACE)


def trim_end(text: str) -> str:
    return text.rstrip(JS_SPACE)


_DECIMAL = re.compile(r"[+-]?(?:[0-9]+\.?[0-9]*(?:[eE][+-]?[0-9]+)?|\.[0-9]+(?:[eE][+-]?[0-9]+)?)")


def number(value: Any) -> float:
    """``Number(value)`` for the values this code passes: strings, numbers, booleans, null and undefined."""
    if value is None:
        return 0.0
    if value is UNDEFINED:
        return math.nan
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if is_num(value):
        return float(value)
    if not isinstance(value, str):
        return math.nan
    if value.isdigit() and value.isascii():
        return float(value)
    text = trim(value)
    if text == "":
        return 0.0
    if re.fullmatch(r"[+-]?Infinity", text):
        return -math.inf if text.startswith("-") else math.inf
    low = text.lower()
    for prefix, base in (("0x", 16), ("0o", 8), ("0b", 2)):
        if low.startswith(prefix):
            try:
                return float(int(text[2:], base)) if re.fullmatch(r"[0-9a-fA-F]+", text[2:]) else math.nan
            except ValueError:
                return math.nan
    if _DECIMAL.fullmatch(text):
        return float(text)
    return math.nan


def num(value: Any) -> int | float:
    """``Number(value)``, as an int when it is a whole number (for use as a count, a row or an index)."""
    result = number(value)
    return int(result) if math.isfinite(result) and result.is_integer() else result


def index_of(items: list[Any], position: Any) -> Any:
    """``items[position]`` with JavaScript's leniency: anything but a whole number in range is undefined (None)."""
    if not is_int(position):
        return None
    i = int(position)
    return items[i] if 0 <= i < len(items) else None


def num_to_str(value: Any) -> str:
    """``String(n)`` for a number (Number.prototype.toString)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        if abs(value) <= MAX_SAFE:
            return str(value)
        value = float(value)
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "Infinity" if value > 0 else "-Infinity"
    if value == 0:
        return "0"
    sign = "-" if value < 0 else ""
    digits_tuple = Decimal(repr(abs(value))).as_tuple()
    digits = "".join(str(d) for d in digits_tuple.digits).rstrip("0") or "0"
    exponent = int(digits_tuple.exponent) + (len(digits_tuple.digits) - len(digits))
    k = len(digits)
    n = exponent + k
    if k <= n <= 21:
        out = digits + "0" * (n - k)
    elif 0 < n <= 21:
        out = digits[:n] + "." + digits[n:]
    elif -6 < n <= 0:
        out = "0." + "0" * (-n) + digits
    else:
        e = n - 1
        mark = "+" if e >= 0 else "-"
        out = (digits if k == 1 else digits[0] + "." + digits[1:]) + "e" + mark + str(abs(e))
    return sign + out


def to_str(value: Any) -> str:
    """``String(value)``."""
    if value is None:
        return "null"
    if value is UNDEFINED:
        return "undefined"
    if isinstance(value, bool) or is_num(value):
        return num_to_str(value)
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return ",".join("" if v is None or v is UNDEFINED else to_str(v) for v in value)
    if isinstance(value, dict):
        return "[object Object]"
    return str(value)


_ESCAPES = {'"': '\\"', "\\": "\\\\", "\b": "\\b", "\f": "\\f", "\n": "\\n", "\r": "\\r", "\t": "\\t"}
_NEEDS_ESCAPE = re.compile('["\\\\\x00-\x1f\ud800-\udfff]')


def json_string(text: str) -> str:
    """``JSON.stringify`` of a string: only quotes, backslashes, control characters and lone surrogates are escaped."""

    def escape(match: re.Match[str]) -> str:
        ch = match.group(0)
        return _ESCAPES.get(ch) or "\\u%04x" % ord(ch)

    return '"' + _NEEDS_ESCAPE.sub(escape, text) + '"'


def json_number(value: Any) -> str:
    if isinstance(value, float) and not math.isfinite(value):
        return "null"
    return num_to_str(value)


def utf16_key(text: str) -> bytes:
    """Sort key for JavaScript's own string order (``<`` compares UTF-16 code units)."""
    return text.encode("utf-16-be", "surrogatepass")


_LONE_SURROGATE = re.compile("[\ud800-\udfff]")


def _plain(value: Any) -> bool:
    """Whether json.dumps writes this exactly as JSON.stringify would: no floats, no undefined, safe integers, string keys only."""
    stack = [value]
    while stack:
        v = stack.pop()
        t = type(v)
        if t is str or t is bool or v is None:
            continue
        if t is int:
            if -MAX_SAFE <= v <= MAX_SAFE:
                continue
            return False
        if t is dict:
            for k in v:
                if type(k) is not str:
                    return False
            stack.extend(v.values())
            continue
        if t is list or t is tuple:
            stack.extend(v)
            continue
        return False
    return True


def canonical(value: Any) -> str:
    """Sorted-key compact JSON, the form every digest and seal in a batch is computed over (code point order of the keys)."""
    if _plain(value):
        # The common case at C speed: Python sorts keys by code point too, and escapes the same characters.
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if not _LONE_SURROGATE.search(text):
            return text
    return _canonical(value)


def _canonical(value: Any) -> str:
    if value is None or value is UNDEFINED:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if is_num(value):
        return json_number(value)
    if isinstance(value, str):
        return json_string(value)
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_canonical(v) for v in value) + "]"
    if isinstance(value, dict):
        entries = sorted(((str(k), v) for k, v in value.items() if v is not UNDEFINED), key=lambda kv: kv[0])
        return "{" + ",".join(json_string(k) + ":" + _canonical(v) for k, v in entries) + "}"
    if hasattr(value, "to_json"):
        return _canonical(value.to_json())
    raise TypeError(f"cannot serialize {type(value).__name__}")


def stringify(value: Any, indent: int | None = None) -> str:
    """``JSON.stringify(value, null, indent)``: keys in insertion order, undefined left out of objects."""

    def write(v: Any, level: int) -> str:
        if v is None or v is UNDEFINED:
            return "null"
        if v is True:
            return "true"
        if v is False:
            return "false"
        if is_num(v):
            return json_number(v)
        if isinstance(v, str):
            return json_string(v)
        if hasattr(v, "to_json"):
            return write(v.to_json(), level)
        pad = "\n" + " " * (indent * (level + 1)) if indent else ""
        end = "\n" + " " * (indent * level) if indent else ""
        if isinstance(v, (list, tuple)):
            if not v:
                return "[]"
            return "[" + ",".join(pad + write(x, level + 1) for x in v) + end + "]"
        if isinstance(v, dict):
            items = [(str(k), x) for k, x in v.items() if x is not UNDEFINED]
            if not items:
                return "{}"
            colon = ": " if indent else ":"
            return "{" + ",".join(pad + json_string(k) + colon + write(x, level + 1) for k, x in items) + end + "}"
        raise TypeError(f"cannot serialize {type(v).__name__}")

    return write(value, 0)


def iso(ms: float | None = None) -> str:
    """``new Date(ms).toISOString()``: UTC with milliseconds."""
    if ms is None:
        ms = now_ms()
    whole = int(math.floor(ms))
    dt = datetime.fromtimestamp(whole // 1000, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (whole % 1000)


def now_ms() -> int:
    """``Date.now()``."""
    return int(time.time() * 1000)


def u16len(text: str) -> int:
    """``text.length``: UTF-16 code units."""
    return len(text.encode("utf-16-le", "surrogatepass")) // 2


def u16slice(text: str, start: int, end: int | None = None) -> str:
    """``text.slice(start, end)`` counted in UTF-16 code units (a surrogate pair cut in half stays half, as in JavaScript)."""
    data = text.encode("utf-16-le", "surrogatepass")
    units = len(data) // 2
    a = max(0, units + start) if start < 0 else min(start, units)
    b = units if end is None else (max(0, units + end) if end < 0 else min(end, units))
    return data[2 * a: 2 * max(a, b)].decode("utf-16-le", "surrogatepass")


#: The order of the root collation (CLDR) for ASCII punctuation and symbols, which all sort before digits and letters.
_PUNCTUATION_ORDER = "_-,;:!?.'\"()[]{}@*/\\&#%`^+<=>|~$"


def _primary(ch: str) -> tuple[int, int]:
    import unicodedata

    if ch.isspace():
        return (0, ord(ch))
    at = _PUNCTUATION_ORDER.find(ch)
    if at >= 0:
        return (1, at)
    category = unicodedata.category(ch)
    if category.startswith("P"):
        return (1, 100 + ord(ch))
    if category.startswith("S"):
        return (2, ord(ch))
    if category == "Nd":
        return (3, unicodedata.digit(ch, 0))
    folded = ch.casefold()
    if "a" <= folded[:1] <= "z":
        return (4, ord(folded[0]))
    return (5, ord(ch))


def locale_key(text: str) -> tuple:
    """
    ``localeCompare`` (ICU's root collation) as far as file names need it: spaces, then punctuation, symbols, digits, Latin letters
    without regard to case, then everything else (Han by code point); lower case before upper case at a tie; the text itself last.
    """
    return (tuple(_primary(ch) for ch in text), tuple(0 if ch.islower() or not ch.isupper() else 1 for ch in text), text)


def encode_uri_component(text: str) -> str:
    from urllib.parse import quote

    return quote(text, safe="-_.!~*'()")


def encode_uri(text: str) -> str:
    from urllib.parse import quote

    return quote(text, safe="-_.!~*'();/?:@&=+$,#")
