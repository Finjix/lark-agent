"""`prune`: removes what this module keeps that is expendable, for the host's periodic housekeeping (lark-agent runs it per user).

- Temporary files a save left behind when the process died halfway (`<data root>/*.<pid>.<12 hex>.tmp`, see fsutil.write_json_atomic),
  once untouched for FEISHU_RETENTION_DAYS (default 14) days.
- The caches of the hidden browser's profile (HTTP, code, GPU and shader caches, service-worker caches, crash reports): the browser rebuilds
  every one of them, while the cookies and local storage that hold the Feishu web session are not among them. They go whole, whatever
  their age, and only while no command holds the profile and no browser runs on it.

The login (auth.json), a pending authorization and the browser's sign-in always stay. Links are removed as links, never followed.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import time
from typing import Mapping

from .browser import answers, lock_profile, read_active_port
from .errors import Blocked, need
from .paths import resolve_data_paths

RETENTION_ENV = "FEISHU_RETENTION_DAYS"
_TMP = re.compile(r"\.\d+\.[0-9a-f]{12}\.tmp\Z")
#: Caches of the whole browser, then of each profile inside it (Default, Profile 1, …).
BROWSER_CACHES = ("GrShaderCache", "ShaderCache", "GraphiteDawnCache", "component_crx_cache", "extensions_crx_cache", "Crashpad", "BrowserMetrics")
PROFILE_CACHES = ("Cache", "Code Cache", "GPUCache", "DawnCache", "DawnGraphiteCache", "DawnWebGPUCache",
                  os.path.join("Service Worker", "CacheStorage"), os.path.join("Service Worker", "ScriptCache"))
_PROFILE = re.compile(r"(Default|Profile \d+|Guest Profile|System Profile)\Z")


def retention_days(env: Mapping[str, str]) -> int:
    raw = (env.get(RETENTION_ENV) or "").strip()
    if not raw:
        return 14
    need(raw.isdigit() and int(raw) >= 1, f"{RETENTION_ENV} must be a whole number of days, at least 1")
    return int(raw)


def _plain_dir(path: str) -> bool:
    try:
        return stat.S_ISDIR(os.lstat(path).st_mode)
    except OSError:
        return False


def _remove_whole(target: str) -> tuple[int, int]:
    """Removes a folder whole (a link as a link) and says how many files and bytes went; (0, 0) when it could not go."""
    files = size = 0
    for root, dirs, names in os.walk(target, followlinks=False):
        for name in names:
            try:
                info = os.lstat(os.path.join(root, name))
            except OSError:
                continue
            files += 1
            size += info.st_size if stat.S_ISREG(info.st_mode) else 0
        # Links to folders are counted as one entry and not entered.
        for name in list(dirs):
            if os.path.islink(os.path.join(root, name)):
                files += 1
                dirs.remove(name)
    try:
        if os.path.islink(target) or not os.path.isdir(target):
            os.remove(target)
        else:
            shutil.rmtree(target)
    except OSError:
        return 0, 0
    return files, size


def _browser_in_use(profile: str) -> bool:
    """A browser runs on the profile: the port it wrote answers, or (Chrome and Edge on Windows) it holds its lockfile open."""
    active = read_active_port(profile)
    if active and answers(active[0], 1_000):
        return True
    lockfile = os.path.join(profile, "lockfile")
    if os.name == "nt" and os.path.exists(lockfile):
        try:
            with open(lockfile, "r+b"):
                pass
        except PermissionError:
            return True
        except OSError:
            pass
    return False


def _browser_caches(profile: str) -> tuple[int, int]:
    if not _plain_dir(profile):
        return 0, 0
    try:
        release = lock_profile(profile, wait_ms=0)
    except Blocked:
        return 0, 0  # a command holds the profile right now: next time
    try:
        if _browser_in_use(profile):
            return 0, 0
        targets = [os.path.join(profile, name) for name in BROWSER_CACHES]
        for entry in os.scandir(profile):
            if _PROFILE.match(entry.name) and entry.is_dir(follow_symlinks=False):
                targets += [os.path.join(profile, entry.name, name) for name in PROFILE_CACHES]
        files = size = 0
        for target in targets:
            if os.path.lexists(target):
                f, s = _remove_whole(target)
                files, size = files + f, size + s
        return files, size
    finally:
        release()


def _stale_tmp(root: str, cutoff: float) -> tuple[int, int]:
    files = size = 0
    try:
        entries = list(os.scandir(root))
    except OSError:
        return 0, 0
    for entry in entries:
        if not _TMP.search(entry.name) or not entry.is_file(follow_symlinks=False):
            continue
        try:
            info = entry.stat(follow_symlinks=False)
            # The latest of created, modified and (on Windows) moved: a file still in use is never old.
            if max(info.st_mtime, info.st_ctime) >= cutoff:
                continue
            os.remove(entry.path)
        except OSError:
            continue
        files, size = files + 1, size + info.st_size
    return files, size


def prune(env: Mapping[str, str], now: float | None = None) -> dict[str, object]:
    paths = resolve_data_paths(env)
    if not _plain_dir(paths.root):
        return {"status": "ok", "files": 0, "bytes": 0}
    cutoff = (time.time() if now is None else now) - retention_days(env) * 86_400
    tmp_files, tmp_bytes = _stale_tmp(paths.root, cutoff)
    cache_files, cache_bytes = _browser_caches(paths.browser_profile)
    return {"status": "ok", "files": tmp_files + cache_files, "bytes": tmp_bytes + cache_bytes}
