"""A1 references: column letters, coordinates, rectangles, and moving a formula's references when it is filled down."""

from __future__ import annotations

import functools
import re

from .util import need


@functools.lru_cache(maxsize=4096)
def column_letter(index: int) -> str:
    need(isinstance(index, int) and not isinstance(index, bool) and 1 <= index <= 16384, "column number out of range")
    n = index
    out = ""
    while n > 0:
        rest = (n - 1) % 26
        out = chr(65 + rest) + out
        n = (n - 1) // 26
    return out


@functools.lru_cache(maxsize=4096)
def column_index(letters: str) -> int:
    need(re.fullmatch(r"[A-Za-z]{1,3}", letters or ""), f"not a column name: {letters}")
    n = 0
    for ch in letters.upper():
        n = n * 26 + (ord(ch) - 64)
    need(1 <= n <= 16384, f"column out of range: {letters}")
    return n


_COORD = re.compile(r"\$?([A-Za-z]{1,3})\$?([0-9]{1,7})")


def parse_coord(coord: str) -> tuple[int, int]:
    """(col, row) of a coordinate like B12 or $B$12."""
    m = _COORD.fullmatch(coord) if isinstance(coord, str) else None
    need(m, f"not a cell reference: {coord}")
    row = int(m.group(2))
    need(1 <= row <= 1048576, f"row out of range: {coord}")
    return column_index(m.group(1)), row


def coord_of(col: int, row: int) -> str:
    return f"{column_letter(col)}{row}"


class CellRange:
    """A rectangle of cells, written "A1:B2" or "A1" like in the workbook files."""

    __slots__ = ("min_col", "max_col", "min_row", "max_row")

    def __init__(self, spec: "str | dict[str, int] | None" = None, *, min_col: int = 0, max_col: int = 0, min_row: int = 0, max_row: int = 0) -> None:
        if isinstance(spec, str):
            parts = spec.split(":")
            need(len(parts) <= 2, f"not a cell range: {spec}")
            a = parse_coord(parts[0])
            b = a if len(parts) == 1 else parse_coord(parts[1])
            self.min_col, self.max_col = min(a[0], b[0]), max(a[0], b[0])
            self.min_row, self.max_row = min(a[1], b[1]), max(a[1], b[1])
        else:
            self.min_col, self.max_col, self.min_row, self.max_row = min_col, max_col, min_row, max_row

    def contains(self, coord: str) -> bool:
        col, row = parse_coord(coord)
        return self.min_col <= col <= self.max_col and self.min_row <= row <= self.max_row

    def __str__(self) -> str:
        a = coord_of(self.min_col, self.min_row)
        if self.min_col == self.max_col and self.min_row == self.max_row:
            return a
        return f"{a}:{coord_of(self.max_col, self.max_row)}"

    __repr__ = __str__

    def to_json(self) -> dict[str, int]:
        return {"minCol": self.min_col, "maxCol": self.max_col, "minRow": self.min_row, "maxRow": self.max_row}


_CELL_REF = re.compile(r"(\$?)([A-Za-z]{1,3})(\$?)([0-9]{1,7})")


_INTERESTING = re.compile(r"[\"']|(?<![A-Za-z0-9_.])[A-Za-z$]")
_REF_END = re.compile(r"[A-Za-z0-9_(.]")


def translate_formula(formula: str, origin: str, dest: str) -> str:
    """
    Moves the relative references of a formula as if it had been copied from one cell to another, which is what filling a formula down
    does. Strings, quoted sheet names and function names are left alone.
    """
    from_col, from_row = parse_coord(origin)
    to_col, to_row = parse_coord(dest)
    d_row = to_row - from_row
    d_col = to_col - from_col
    out: list[str] = []
    i = 0
    length = len(formula)
    while i < length:
        found = _INTERESTING.search(formula, i)
        if not found:
            out.append(formula[i:])
            break
        start = found.start()
        out.append(formula[i:start])
        ch = formula[start]
        if ch in ('"', "'"):
            j = start + 1
            while j < length:
                if formula[j] == ch:
                    if j + 1 < length and formula[j + 1] == ch:
                        j += 2
                        continue
                    break
                j += 1
            out.append(formula[start:j + 1])
            i = j + 1
            continue
        # A reference starts where the character before it cannot belong to a name (the source's is the output's: both end the same).
        m = _CELL_REF.match(formula, start)
        after = start + len(m.group(0)) if m else start
        if m and not (after < length and _REF_END.match(formula[after])):
            abs_col, letters, abs_row, digits = m.group(1), m.group(2), m.group(3), m.group(4)
            col = column_index(letters) if abs_col else column_index(letters) + d_col
            row = int(digits) if abs_row else int(digits) + d_row
            need(1 <= col <= 16384 and 1 <= row <= 1048576, "a formula reference moves out of the sheet when shifted")
            out.append(f"{abs_col}{column_letter(col)}{abs_row}{row}")
            i = after
            continue
        out.append(ch)
        i = start + 1
    return "".join(out)
