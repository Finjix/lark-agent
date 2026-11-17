"""Files written whole or not at all, and an exclusive lock file."""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import time
from typing import Any, Callable, TypeVar

from .errors import BridgeError

T = TypeVar("T")


def read_json(file: str) -> Any:
    """The parsed file; None when it is not there or is not JSON. Any other failure to read it is raised."""
    try:
        with open(file, encoding="utf-8") as handle:
            text = handle.read()
    except FileNotFoundError:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def pid_alive(pid: int) -> bool:
    """Whether a process with this id is running (`process.kill(pid, 0)` without the signal).

    On Windows os.kill(pid, 0) would send CTRL_C_EVENT, so the process is looked up instead; one that exists but may not be opened is alive.
    """
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        process_query_limited_information = 0x1000
        if pid > 0xFFFFFFFF:
            return False
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            return ctypes.get_last_error() == 5  # ERROR_ACCESS_DENIED: there, but not ours to look at
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except (ProcessLookupError, OverflowError, OSError):
        return False


def _restrict_to_current_account(file: str) -> None:
    """On Windows the file mode means nothing: limit the file to the current account and SYSTEM (best effort)."""
    if sys.platform != "win32":
        return
    try:
        # The account comes from the environment: whoami prints in the console's code page, which garbles a non-ASCII name.
        user = os.environ.get("USERNAME")
        if not user:
            return
        domain = os.environ.get("USERDOMAIN")
        account = f"{domain}\\{user}" if domain else user
        subprocess.run(
            ["icacls.exe", file, "/inheritance:r", "/grant:r", f"{account}:(F)", "*S-1-5-18:(F)"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=False,
        )
    except (OSError, subprocess.SubprocessError):
        pass  # the directory's own permissions still apply


def write_json_atomic(file: str, value: Any) -> None:
    """Writes through a temporary file in the same directory and renames it, so a reader never sees half a file."""
    os.makedirs(os.path.dirname(file), exist_ok=True)
    temp = f"{file}.{os.getpid()}.{secrets.token_hex(6)}.tmp"
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8"))
        _restrict_to_current_account(temp)
        attempt = 0
        while True:
            try:
                os.replace(temp, file)
                return
            except PermissionError:
                # Windows refuses to replace a file another process has open for a moment.
                if attempt >= 20:
                    raise
                time.sleep(0.025 * (attempt + 1))
                attempt += 1
    finally:
        try:
            os.remove(temp)
        except OSError:
            pass


def remove_file(file: str) -> None:
    try:
        os.remove(file)
    except FileNotFoundError:
        pass


def _holder_gone(text: str) -> bool:
    """Whether the process that took this lock has ended (one still writing it, without a pid yet, has not)."""
    try:
        pid = json.loads(text).get("pid")
    except (ValueError, AttributeError):
        return False
    return isinstance(pid, int) and not isinstance(pid, bool) and pid > 0 and not pid_alive(pid)


def _read_text(file: str) -> str:
    with open(file, encoding="utf-8", errors="replace") as handle:
        return handle.read()


def with_lock(file: str, body: Callable[[], T], *, timeout_ms: int = 90_000, stale_ms: int = 60_000, poll_ms: int = 40) -> T:
    """Runs `body` holding a lock file created exclusively.

    A lock whose process has ended, or that is older than `stale_ms` (its process id may have gone to another process by then), is taken
    over; the takeover goes through a rename so that of several waiters seeing the same dead lock only one wins it. A waiter waits longer
    than a lock takes to go stale, so that it can take over a dead one.
    """
    os.makedirs(os.path.dirname(file), exist_ok=True)
    deadline = time.time() * 1000 + timeout_ms
    owner = secrets.token_hex(8)
    while True:
        try:
            fd = os.open(file, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0))
            with os.fdopen(fd, "wb") as handle:
                handle.write(json.dumps({"pid": os.getpid(), "at": int(time.time() * 1000), "owner": owner}, separators=(",", ":")).encode())
            break
        except FileExistsError:
            pass
        except PermissionError:
            # On Windows a file that is just being deleted or is open elsewhere refuses with this instead of "exists": somebody holds it.
            pass
        try:
            age = time.time() * 1000 - os.stat(file).st_mtime * 1000
            text = _read_text(file)
            if age > stale_ms or _holder_gone(text):
                claimed = f"{file}.stale-{os.getpid()}-{secrets.token_hex(4)}"
                try:
                    os.rename(file, claimed)
                    taken = True
                except OSError:
                    taken = False
                if taken:
                    # A lock taken afresh between the check and the rename is not the one found dead: it goes back.
                    try:
                        moved = _read_text(claimed)
                    except OSError:
                        moved = ""
                    if moved != text:
                        try:
                            os.rename(claimed, file)
                        except OSError:
                            _quiet_remove(claimed)
                    else:
                        _quiet_remove(claimed)
                    continue
        except OSError:
            # Released between the checks: taken at once. One that is there but cannot be read is waited on like a held lock.
            if not os.path.exists(file):
                continue
        if time.time() * 1000 > deadline:
            raise BridgeError("network", "Another process is renewing the Feishu login and did not finish in time: try again later")
        time.sleep(poll_ms / 1000)
    try:
        return body()
    finally:
        # Only the lock this call made: one that was taken over as stale belongs to somebody else by now.
        try:
            mine = json.loads(_read_text(file)).get("owner") == owner
        except (OSError, ValueError, AttributeError):
            mine = False
        if mine:
            _quiet_remove(file)


def _quiet_remove(file: str) -> None:
    try:
        os.remove(file)
    except OSError:
        pass
