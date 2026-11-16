"""Removes this user's batches and temporary files nobody has touched for a while (retention.ts)."""

from __future__ import annotations

import math
import os
import shutil
import stat as stat_module
import sys
import time
from typing import Callable, Mapping

from .paths import DataPaths

RETENTION_DAYS_ENV = "REQUIREMENT_AUTO_RETENTION_DAYS"
#: The folder of tmp that holds one folder per unpacked zip.
UNPACKED = "unpacked"
DAY = 86_400_000


def retention_days(env: Mapping[str, str] | None = None) -> int:
    """Days to keep untouched batches and temporary files: 7 like lark-agent's workspace, a whole number of at least 1 from the environment."""
    env = os.environ if env is None else env
    raw = env.get(RETENTION_DAYS_ENV)
    from .js import number

    given = number(raw) if raw is not None else math.nan
    return int(given) if math.isfinite(given) and given.is_integer() and given >= 1 else 7


def _change_time_ms(target: str) -> float:
    """The time a file's metadata last changed (a move, a rename). Windows keeps it (ChangeTime) but os.stat does not show it."""
    if sys.platform != "win32":
        try:
            return os.lstat(target).st_ctime * 1000
        except OSError:
            return 0.0
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel32.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    FILE_READ_ATTRIBUTES = 0x80
    SHARE_ALL = 0x7
    OPEN_EXISTING = 3
    FLAGS = 0x02000000 | 0x00200000  # FILE_FLAG_BACKUP_SEMANTICS (folders) | FILE_FLAG_OPEN_REPARSE_POINT (a link is looked at as a link)
    handle = create(target, FILE_READ_ATTRIBUTES, SHARE_ALL, None, OPEN_EXISTING, FLAGS, None)
    if handle in (None, wintypes.HANDLE(-1).value):
        return 0.0

    class BasicInfo(ctypes.Structure):
        _fields_ = [("CreationTime", ctypes.c_longlong), ("LastAccessTime", ctypes.c_longlong), ("LastWriteTime", ctypes.c_longlong),
                    ("ChangeTime", ctypes.c_longlong), ("FileAttributes", wintypes.DWORD)]

    try:
        info = BasicInfo()
        get = kernel32.GetFileInformationByHandleEx
        get.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        if not get(handle, 0, ctypes.byref(info), ctypes.sizeof(info)):
            return 0.0
        # 100-ns intervals since 1601 → ms since 1970
        return info.ChangeTime / 10_000 - 11_644_473_600_000
    finally:
        kernel32.CloseHandle(handle)


def _touched(target: str) -> tuple[float, bool] | None:
    """The most recent creation, change or move of a file; a link is looked at as a link, never followed."""
    try:
        st = os.lstat(target)
    except OSError:
        return None
    birth = getattr(st, "st_birthtime", st.st_ctime if sys.platform == "win32" else 0)
    at = max(birth * 1000, st.st_mtime * 1000, _change_time_ms(target))
    link = bool(getattr(st, "st_file_attributes", 0) & getattr(stat_module, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return at, stat_module.S_ISDIR(st.st_mode) and not link


def _newest(target: str) -> float:
    """The newest `touched` of a file or of anything below a directory. A directory that cannot be read counts as new: nothing is removed on a guess."""
    info = _touched(target)
    if not info:
        return 0
    at, directory = info
    if not directory:
        return at
    latest = at
    try:
        for entry in os.listdir(target):
            latest = max(latest, _newest(os.path.join(target, entry)))
    except OSError:
        return math.inf
    return latest


def _is_real_dir(path: str) -> bool:
    try:
        st = os.lstat(path)
    except OSError:
        return False
    if not stat_module.S_ISDIR(st.st_mode):
        return False
    # A junction is a directory to lstat on Windows: it is a link all the same.
    return not (getattr(st, "st_file_attributes", 0) & getattr(stat_module, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _remove(target: str) -> None:
    if _is_real_dir(target):
        shutil.rmtree(target)
    elif os.path.isdir(target) and not os.path.islink(target):
        os.rmdir(target)  # a junction: the link goes, never what it points at
    else:
        os.remove(target)


def prune_expired(paths: DataPaths, now: float | None = None, days: int | None = None) -> int:
    """
    Deletes what nobody has touched for `days` from this user's batches and temporary files, and nothing else: the Feishu login, the CLS
    login and its account record live next to them and stay. A batch goes whole, and only when nothing in it is newer than the cutoff, so a
    batch that is still being worked on is never cut in half. What cannot be deleted now is left for the next run. Returns how many went.
    """
    now = time.time() * 1000 if now is None else now
    days = retention_days() if days is None else days
    cutoff = now - days * DAY
    removed = 0

    def sweep(root: str, keep_entry: Callable[[str], bool] = lambda name: False) -> None:
        """Each entry of a real folder (a link in its place would lead the sweep out of the user's directory) goes whole once nothing in it is recent."""
        nonlocal removed
        if not _is_real_dir(root):
            return
        try:
            names = os.listdir(root)
        except OSError:
            names = []
        for name in names:
            target = os.path.join(root, name)
            try:
                if keep_entry(name) or _newest(target) >= cutoff:
                    continue
                _remove(target)
                removed += 1
            except OSError:
                pass  # open in a program or no permission: next time

    sweep(paths.batches)

    # Every zip unpacked for an upload is a folder of its own under tmp/unpacked, and is judged on its own: one unpacked last week must not
    # keep every older one (each up to 20 GB) for as long as the user goes on uploading.
    def unpacked(name: str) -> bool:
        if name != UNPACKED:
            return False
        sweep(os.path.join(paths.tmp, UNPACKED))
        return True

    sweep(paths.tmp, unpacked)
    return removed
