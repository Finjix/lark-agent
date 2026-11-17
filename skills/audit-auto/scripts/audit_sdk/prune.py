"""Housekeeping (`audit prune`, declared as `metadata.prune` in SKILL.md): deletes this skill's expendable per-user data.

A host runs it periodically, once for each user directory, with that user's AGENT_USER_DIR set (cwd = the skill's folder, no chat).
It deletes the files in <user>/audit-auto/tmp/ and <user>/audit-auto/reviews/ that nobody has created or modified for
AUDIT_AUTO_RETENTION_DAYS days (default 14), and the folders that leaves empty. Symbolic links and junctions are never followed:
a stale link is deleted itself, never what it points to. What cannot be deleted (in use, read-only) is skipped. The shared folder
(review groups, dynamic specs) is never touched.

Older versions put the scratch of runs without a user directory in <system temp>/audit-auto/run-*: stale ones are swept too.
"""

from __future__ import annotations

import os
import stat
import tempfile
import time
from dataclasses import dataclass
from typing import Callable, Mapping

from .config import reviews_dir, retention_days, scratch_root, text
from .errors import need

DAY = 24 * 60 * 60
_REPARSE_POINT = 0x400  # FILE_ATTRIBUTE_REPARSE_POINT: a junction, or a symbolic link, on Windows


@dataclass
class Pruned:
    files: int = 0
    bytes: int = 0


def _is_link(st: os.stat_result) -> bool:
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, "st_file_attributes", 0) & _REPARSE_POINT)


def _touched(st: os.stat_result) -> float:
    """The later of creation and modification (st_ctime is the creation time on Windows, the change time elsewhere)."""
    return max(st.st_mtime, st.st_ctime, getattr(st, "st_birthtime", 0))


def _remove_link(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        os.rmdir(path)  # a directory junction or symbolic link on Windows: removes the link, not its target


def _prune_dir(directory: str, cutoff: float, out: Pruned, keep: Callable[[str], bool] | None = None) -> bool:
    """Prunes inside `directory`; whether it is empty afterwards. `keep(name)` spares top-level entries it is true for."""
    try:
        entries = list(os.scandir(directory))
    except OSError:
        return False
    left = 0
    for entry in entries:
        if keep and keep(entry.name):
            left += 1
            continue
        try:
            st = entry.stat(follow_symlinks=False)
            if _is_link(st) or not stat.S_ISDIR(st.st_mode):
                if _touched(st) >= cutoff:
                    left += 1
                    continue
                if _is_link(st):
                    _remove_link(entry.path)
                else:
                    os.remove(entry.path)
                out.files += 1
                out.bytes += st.st_size if stat.S_ISREG(st.st_mode) else 0
                continue
            before = out.files
            # A folder goes when pruning emptied it, or when it was empty and stale already (a run that died before writing).
            if _prune_dir(entry.path, cutoff, out) and (out.files > before or _touched(st) < cutoff):
                os.rmdir(entry.path)
                continue
        except OSError:
            pass  # in use or not ours to delete: skipped
        left += 1
    return left == 0


def prune_tree(root: str, cutoff: float, out: Pruned, keep: Callable[[str], bool] | None = None) -> None:
    """Deletes what under `root` was last created or modified before `cutoff` (seconds since the epoch), and the folders that leaves
    empty; `root` itself stays, and is left alone when it is a link."""
    try:
        if _is_link(os.lstat(root)):
            return
    except OSError:
        return
    _prune_dir(root, cutoff, out, keep)


def legacy_scratch() -> str:
    return os.path.join(tempfile.gettempdir(), "audit-auto")


def prune(env: Mapping[str, str], home: str | None = None, now: float | None = None, legacy: str | None = None) -> dict[str, object]:
    """The `prune` command: {"status": "ok", "files": <deleted>, "bytes": <freed>}."""
    days = retention_days(env)
    given = text(env, "AGENT_USER_DIR")
    need(not given or os.path.isabs(given), f"AGENT_USER_DIR must be an absolute path (it is {given!r})")
    cutoff = (time.time() if now is None else now) - days * DAY
    out = Pruned()
    for root in (scratch_root(env, home), reviews_dir(env, home)):
        prune_tree(root, cutoff, out)
    prune_tree(legacy or legacy_scratch(), cutoff, out, keep=lambda name: not name.startswith("run-"))
    return {"status": "ok", "files": out.files, "bytes": out.bytes}
