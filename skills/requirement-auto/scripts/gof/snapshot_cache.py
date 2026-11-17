"""A parsed snapshot kept for the next command that reads the same XLSX.

Parsing the progress workbook's export takes ~15 s, and one task reads the same export in several commands (snapshot, context, plan,
prepare, the batch's baseline copy, reconcile and verify). The parse is kept under <data>/tmp/snapshot-cache/, named by the SHA-256 of the
file's bytes and of this skill's code, so a changed file or changed code never meets an old parse. Only exports inside this user's data
directory are kept. Loading admits nothing but the skill's own workbook classes and plain data; anything unexpected is a miss and the file
is parsed again. Nothing here may stop a command: every failure is a miss.
"""

from __future__ import annotations

import hashlib
import io
import os
import pickle
import zlib
from typing import Any, Optional

#: Parses kept; the oldest beyond this go.
KEEP = 4
DIR_NAME = "snapshot-cache"
_ALLOWED = {("gof.xlsx", "CellData"), ("gof.xlsx", "RuleData"), ("gof.xlsx", "SheetData"), ("gof.xlsx", "WorkbookData"),
            ("gof.a1", "CellRange")}

_code_digest: Optional[str] = None


def _code() -> str:
    """Every source file of the skill's package, so any change to how a workbook is read starts afresh."""
    global _code_digest
    if _code_digest is None:
        here = os.path.dirname(os.path.abspath(__file__))
        h = hashlib.sha256()
        for name in sorted(os.listdir(here)):
            if name.endswith(".py"):
                with open(os.path.join(here, name), "rb") as handle:
                    h.update(name.encode() + b"\0" + handle.read() + b"\0")
        _code_digest = h.hexdigest()
    return _code_digest


class _Loader(pickle.Unpickler):
    def find_class(self, module: str, name: str) -> Any:
        if (module, name) in _ALLOWED:
            return super().find_class(module, name)
        raise pickle.UnpicklingError(f"{module}.{name} is not admitted")


def cache_dir(file: str) -> Optional[str]:
    """Where parses of this file are kept, or None when it is not inside this user's data directory."""
    try:
        from .paths import resolve_data_paths

        paths = resolve_data_paths()
        root = os.path.normcase(os.path.realpath(paths.root))
        real = os.path.normcase(os.path.realpath(file))
        if os.path.commonpath([root, real]) != root:
            return None
        return os.path.join(paths.tmp, DIR_NAME)
    except Exception:
        return None


def key_of(file: str) -> str:
    h = hashlib.sha256()
    with open(file, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            h.update(block)
    return hashlib.sha256((h.hexdigest() + _code()).encode()).hexdigest()


def load(directory: str, key: str) -> Optional[dict]:
    try:
        with open(os.path.join(directory, key + ".pickle.z"), "rb") as handle:
            state = _Loader(io.BytesIO(zlib.decompress(handle.read()))).load()
        os.utime(os.path.join(directory, key + ".pickle.z"))
        return state if isinstance(state, dict) else None
    except Exception:
        return None


def save(directory: str, key: str, state: dict) -> None:
    try:
        os.makedirs(directory, exist_ok=True)
        target = os.path.join(directory, key + ".pickle.z")
        partial = f"{target}.{os.getpid()}.part"
        with open(partial, "wb") as handle:
            handle.write(zlib.compress(pickle.dumps(state, protocol=pickle.HIGHEST_PROTOCOL), 1))
        os.replace(partial, target)
        kept = sorted((os.path.join(directory, n) for n in os.listdir(directory) if n.endswith(".pickle.z")), key=os.path.getmtime, reverse=True)
        for old in kept[KEEP:]:
            try:
                os.remove(old)
            except OSError:
                pass
    except Exception:
        pass
