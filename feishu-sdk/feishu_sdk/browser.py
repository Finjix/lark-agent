"""A minimal Chromium driver (browser.ts).

Find the Chrome or Edge that is already installed, start it with its own profile folder and speak the DevTools protocol to it over a
WebSocket. No download, no browser automation framework: it exists so that a person can log in to a web site in an ordinary browser window
and the login can then be observed.

requirement-auto drives the same profile (its CLS pages sign in through this Feishu web session) with its own copy of this logic. The two
share the profile through the lock below (a file beside the profile) and the DevToolsActivePort file in it, so those two must stay the same
in both: change them together or not at all.
"""

from __future__ import annotations

import json
import os
import random
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from .errors import Blocked
from .fsutil import pid_alive
from .transport import opener_for

Obj = dict[str, Any]
Handler = Callable[[Obj, "str | None"], None]

#: Environment variable that names the browser executable to use instead of searching for one. requirement-auto's older name is read too:
#: both must start the same browser on the profile they share, or the Feishu web session would not carry over.
BROWSER_ENV = "FEISHU_BROWSER"


def browser_candidates(platform: str | None = None, env: Mapping[str, str] | None = None) -> list[str]:
    """The browsers searched, in order, when the environment does not name one. Edge ships with Windows 10 and 11, so it is the usual find there."""
    platform = platform or sys.platform
    env = env if env is not None else os.environ
    if platform == "win32":
        # The usual folders are listed too: a process whose environment was cut down (a shell started by an agent, say) may not carry these.
        roots = [v for v in (env.get("ProgramFiles(x86)"), env.get("ProgramFiles"), env.get("LOCALAPPDATA"), "C:\\Program Files (x86)", "C:\\Program Files") if v]
        relative = [("Microsoft", "Edge", "Application", "msedge.exe"), ("Google", "Chrome", "Application", "chrome.exe"), ("Chromium", "Application", "chrome.exe")]
        return [os.path.join(root, *parts) for parts in relative for root in roots]
    if platform == "darwin":
        return ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
                "/Applications/Chromium.app/Contents/MacOS/Chromium"]
    return ["/usr/bin/google-chrome", "/usr/bin/google-chrome-stable", "/usr/bin/microsoft-edge", "/usr/bin/chromium", "/usr/bin/chromium-browser", "/snap/bin/chromium"]


def find_browser(env: Mapping[str, str] | None = None, platform: str | None = None) -> str | None:
    env = env if env is not None else os.environ
    named = (env.get(BROWSER_ENV) or "").strip()
    if named:
        return named if os.path.isabs(named) and os.path.isfile(named) else None
    return next((file for file in browser_candidates(platform, env) if os.path.isfile(file)), None)


class CdpError(Exception):
    pass


def _websockets() -> Any:
    try:
        from websockets.sync import client
    except ImportError:
        raise Blocked("缺少 Python 包 websockets（驱动浏览器要用）：运行 start.cmd setup（装 feishu-sdk/requirements.txt）") from None
    return client


class Cdp:
    """A DevTools protocol connection: commands with replies, events, and flattened per-target sessions."""

    def __init__(self, ws: Any) -> None:
        self._ws = ws
        self._next_id = 1
        self._lock = threading.Lock()
        self._pending: dict[int, list[Any]] = {}
        self._handlers: dict[str, list[Handler]] = {}
        self._closers: list[Callable[[], None]] = []
        self.closed = False
        self._reader = threading.Thread(target=self._read, name="cdp-reader", daemon=True)
        self._reader.start()

    @staticmethod
    def connect(url: str, timeout_ms: int = 10_000) -> "Cdp":
        client = _websockets()
        try:
            ws = client.connect(url, open_timeout=timeout_ms / 1000, max_size=None, proxy=None, compression=None,
                                ping_interval=None, close_timeout=2, user_agent_header=None)
        except TimeoutError:
            raise CdpError("DevTools 连接超时") from None
        except Exception:  # noqa: BLE001 - refused, not a WebSocket, a handshake error: all the same to the caller
            raise CdpError("DevTools 连接失败") from None
        return Cdp(ws)

    def _read(self) -> None:
        try:
            while True:
                text = self._ws.recv()
                self._receive(text if isinstance(text, str) else text.decode("utf-8", errors="replace"))
        except Exception:  # noqa: BLE001 - the connection ended, whichever way
            pass
        self._finish()

    def _receive(self, text: str) -> None:
        try:
            message = json.loads(text)
        except ValueError:
            return
        if not isinstance(message, dict):
            return
        if isinstance(message.get("id"), int):
            with self._lock:
                waiting = self._pending.pop(message["id"], None)
            if not waiting:
                return
            error = message.get("error")
            if error:
                waiting[1] = CdpError(str((error or {}).get("message") or "DevTools 命令失败") if isinstance(error, dict) else "DevTools 命令失败")
            else:
                waiting[1] = message.get("result") or {}
            waiting[0].set()
        elif isinstance(message.get("method"), str):
            for handler in list(self._handlers.get(message["method"], [])):
                try:
                    handler(message.get("params") or {}, message.get("sessionId"))
                except Exception:  # noqa: BLE001 - one handler's failure does not stop the connection
                    pass

    def _finish(self) -> None:
        with self._lock:
            if self.closed:
                return
            self.closed = True
            pending = list(self._pending.values())
            self._pending.clear()
            closers = list(self._closers)
        for waiting in pending:
            waiting[1] = CdpError("DevTools 连接已关闭")
            waiting[0].set()
        for closer in closers:
            try:
                closer()
            except Exception:  # noqa: BLE001
                pass

    def send(self, method: str, params: Obj | None = None, session_id: str | None = None, timeout_ms: int = 30_000) -> Obj:
        with self._lock:
            if self.closed:
                raise CdpError("DevTools 连接已关闭")
            ident = self._next_id
            self._next_id += 1
            waiting: list[Any] = [threading.Event(), None]
            self._pending[ident] = waiting
        message: Obj = {"id": ident, "method": method, "params": params or {}}
        if session_id:
            message["sessionId"] = session_id
        try:
            self._ws.send(json.dumps(message))
        except Exception:  # noqa: BLE001
            with self._lock:
                self._pending.pop(ident, None)
            raise CdpError("DevTools 连接已关闭") from None
        if not waiting[0].wait(timeout_ms / 1000):
            with self._lock:
                self._pending.pop(ident, None)
            raise CdpError(f"DevTools 命令超时：{method}")
        if isinstance(waiting[1], Exception):
            raise waiting[1]
        return waiting[1]

    def on(self, method: str, handler: Handler) -> Callable[[], None]:
        self._handlers.setdefault(method, []).append(handler)
        return lambda: self._handlers.get(method, []).remove(handler) if handler in self._handlers.get(method, []) else None

    def on_close(self, handler: Callable[[], None]) -> None:
        """Called once when the connection ends, whichever side ended it (it is called at once if it already has)."""
        with self._lock:
            if not self.closed:
                self._closers.append(handler)
                return
        handler()

    def close(self) -> None:
        try:
            self._ws.close()
        except Exception:  # noqa: BLE001 - already closed
            pass
        self._finish()


@dataclass
class BrowserHandle:
    cdp: Cdp
    #: True when this call started the browser; false when it joined one that was already running on the profile.
    launched: bool
    #: Closes the browser the graceful way, so cookies and local storage are written to the profile; ends it by force if it will not go.
    close: Callable[[], None] = field(repr=False)


def _active_port_file(profile_dir: str) -> str:
    return os.path.join(profile_dir, "DevToolsActivePort")


def read_active_port(profile_dir: str) -> tuple[int, str] | None:
    try:
        with open(_active_port_file(profile_dir), encoding="utf-8") as handle:
            lines = handle.read().splitlines()
        port = int(lines[0]) if lines and lines[0].strip().isdigit() else 0
        endpoint = lines[1] if len(lines) > 1 else ""
        if port > 0 and endpoint.startswith("/devtools/browser/"):
            return port, endpoint
    except (OSError, ValueError):
        pass  # no browser has been started on this profile, or it is just starting
    return None


def _kill_tree(pid: int) -> None:
    if sys.platform == "win32":
        taskkill = os.path.join(os.environ.get("SystemRoot") or "C:\\Windows", "System32", "taskkill.exe")
        try:
            subprocess.run([taskkill, "/PID", str(pid), "/T", "/F"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=15, check=False)
        except (OSError, subprocess.SubprocessError):
            pass
    else:
        try:
            os.kill(pid, 9)
        except OSError:
            pass  # gone already


def endpoint_on(port: int, timeout_ms: int = 3_000) -> str | None:
    """The path of the browser endpoint that answers on this port (/devtools/browser/<id>, '' when it does not say), or None when none answers."""
    url = f"http://127.0.0.1:{port}/json/version"
    try:
        with opener_for(url).open(url, timeout=timeout_ms / 1000) as response:
            if not 200 <= response.status < 300:
                return None
            raw = response.read()
    except Exception:  # noqa: BLE001 - nothing answering, or not HTTP
        return None
    try:
        debugger = json.loads(raw.decode("utf-8", errors="replace")).get("webSocketDebuggerUrl")
    except (ValueError, AttributeError):
        debugger = None
    if not isinstance(debugger, str):
        return ""
    from urllib.parse import urlsplit

    try:
        return urlsplit(debugger).path or "/"
    except ValueError:
        return ""


def answers(port: int, timeout_ms: int = 3_000) -> bool:
    """Whether a browser's DevTools endpoint answers on this port."""
    return endpoint_on(port, timeout_ms) is not None


def _join_browser(profile_dir: str) -> BrowserHandle | None:
    """Joins a browser that is already running on this profile (a login window left open by an earlier command), if there is one."""
    found = read_active_port(profile_dir)
    if not found:
        return None
    port, endpoint = found
    url = f"ws://127.0.0.1:{port}{endpoint}"
    try:
        cdp = Cdp.connect(url, 2_000)
    except CdpError:
        # The port file of a browser that was killed stays behind, and its port may belong to another browser by now; a running one that
        # is only slow to answer must not be taken for gone: a second browser started on its profile is handed over to it by Chrome and
        # exits, and the port file deleted on the way leaves it unreachable.
        answering = endpoint_on(port)
        if answering is None or (answering and answering != endpoint):
            return None
        try:
            cdp = Cdp.connect(url, 15_000)
        except CdpError:
            raise Blocked("这个用户的浏览器在运行，但调试接口没有响应；请稍后再试") from None

    def close() -> None:
        try:
            cdp.send("Browser.close", {}, None, 5_000)
        except CdpError:
            pass
        waited = 0
        while not cdp.closed and waited < 8_000:
            time.sleep(0.1)
            waited += 100
        cdp.close()
        # The connection drops before the browser has finished going; the next command must not meet one that is still on its way out.
        waited = 0
        while waited < 8_000 and answers(port, 500):
            time.sleep(0.25)
            waited += 250

    return BrowserHandle(cdp=cdp, launched=False, close=close)


# One command at a time drives a profile's browser. Two would share its first tab and one would close the browser under the other. The lock
# is a file beside the profile, held from opening the browser until this process lets go of it: by closing the browser, or only its
# connection when the browser is left running for a scan. The holder renews it every LOCK_RENEW_MS. A holder whose process has ended does
# not count, and neither does one that has not renewed it for LOCK_STALE_MS (Windows soon gives a dead process's id to another one, which
# would otherwise keep the lock alive). Within one process the lock is shared (nested opens of a profile are the caller's own business).
LOCK_WAIT_MS, LOCK_RENEW_MS, LOCK_STALE_MS = 120_000, 30_000, 2 * 60_000


@dataclass
class _Held:
    count: int
    token: str
    stop: threading.Event


_held: dict[str, _Held] = {}
_held_lock = threading.Lock()


def _now() -> float:
    return time.time() * 1000


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _abandoned(file: str, text: str) -> bool:
    """Whether a lock file is of nobody any more: its process ended, it has not been renewed for long, or it was never finished being written."""
    try:
        holder = json.loads(text)
    except ValueError:
        holder = None  # half written
    pid = holder.get("pid") if isinstance(holder, dict) else None
    at = holder.get("at") if isinstance(holder, dict) else None
    if not _number(pid) or not _number(at):
        try:
            mtime = os.stat(file).st_mtime * 1000
        except OSError:
            mtime = 0
        return _now() - mtime > 10_000
    return not pid_alive(int(pid)) or _now() - at > LOCK_STALE_MS


def _owned_by(file: str, token: str) -> bool:
    """Whether the lock file is still the one this holder wrote (it may have been taken over as abandoned meanwhile)."""
    try:
        with open(file, encoding="utf-8") as handle:
            return json.loads(handle.read()).get("token") == token
    except (OSError, ValueError, AttributeError):
        return False


def _read(file: str) -> str:
    with open(file, encoding="utf-8", errors="replace", newline="") as handle:
        return handle.read()


def lock_profile(profile_dir: str, wait_ms: int = LOCK_WAIT_MS) -> Callable[[], None]:
    """Takes the profile's lock (waiting for another command that holds it) and returns the function that lets go of it."""
    file = f"{os.path.abspath(profile_dir)}.lock"
    released = False

    def release() -> None:
        nonlocal released
        if released:
            return
        released = True
        with _held_lock:
            mine = _held.get(file)
            if not mine:
                return
            mine.count -= 1
            if mine.count > 0:
                return
            del _held[file]
        mine.stop.set()
        if _owned_by(file, mine.token):
            try:
                os.remove(file)
            except OSError:
                pass

    with _held_lock:
        mine = _held.get(file)
        if mine:
            mine.count += 1
            return release
    os.makedirs(os.path.dirname(file), exist_ok=True)
    token = str(uuid.uuid4())

    def content() -> str:
        return json.dumps({"pid": os.getpid(), "at": int(_now()), "token": token}, separators=(",", ":"))

    deadline = _now() + wait_ms
    while True:
        try:
            with open(file, "x", encoding="utf-8", newline="") as handle:
                handle.write(content())
            stop = threading.Event()

            def renew() -> None:
                while not stop.wait(LOCK_RENEW_MS / 1000):
                    if _owned_by(file, token):
                        try:
                            with open(file, "w", encoding="utf-8", newline="") as handle:
                                handle.write(content())
                        except OSError:
                            pass  # renewed at the next beat

            threading.Thread(target=renew, name="browser-lock-renew", daemon=True).start()
            with _held_lock:
                _held[file] = _Held(count=1, token=token, stop=stop)
            return release
        except FileExistsError:
            pass
        except PermissionError:
            # Windows refuses a file that is being deleted, or a path that is a folder, with this instead of "exists".
            if not os.path.exists(file):
                raise
        text: str | None
        try:
            text = _read(file)
        except OSError:
            text = None  # let go of a moment ago, or not a file that can be read: waited on like a held lock
        if text is not None and _abandoned(file, text):
            # Taken away under a name of its own, so that of several waiters seeing the same dead lock none removes a lock just made by another.
            aside = f"{file}.{os.getpid()}-{random.getrandbits(52):x}"
            try:
                os.rename(file, aside)
                taken = True
            except OSError:
                taken = False
            if taken:
                try:
                    moved = _read(aside)
                except OSError:
                    moved = ""
                if moved == text:
                    _remove(aside)
                else:
                    try:
                        os.rename(aside, file)
                    except OSError:
                        _remove(aside)
                continue
        if _now() > deadline:
            raise Blocked("这个用户的浏览器正被另一个命令使用：同一时间只能运行一个用到浏览器的命令（登录、检查登录、截图、素材上传），请等它结束后再运行")
        time.sleep(0.25)


def _remove(file: str) -> None:
    try:
        os.remove(file)
    except OSError:
        pass


def _holding(handle: BrowserHandle, release: Callable[[], None]) -> BrowserHandle:
    """The handle with the profile lock: let go of when the browser is closed, or when only its connection is (the browser left running)."""
    closing = False

    def on_close() -> None:
        if not closing:
            release()

    handle.cdp.on_close(on_close)

    def close() -> None:
        nonlocal closing
        closing = True
        try:
            handle.close()
        finally:
            release()

    return BrowserHandle(cdp=handle.cdp, launched=handle.launched, close=close)


def attach_browser(profile_dir: str) -> BrowserHandle | None:
    """Joins a browser that is already running on this profile (a login window left open by an earlier command), if there is one."""
    release = lock_profile(profile_dir)
    try:
        handle = _join_browser(profile_dir)
        if not handle:
            release()
            return None
        return _holding(handle, release)
    except BaseException:
        release()
        raise


# A child that dies with this process: Python's children outlive it by default, where Node (libuv) put them in a job that ended with the
# parent. A hidden browser should end with the command that started it, so a crashed command leaves none behind.
_job_handle: Any = None


def _kill_with_this_process(pid: int) -> None:
    if sys.platform != "win32":
        return
    global _job_handle
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
        kernel32.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        if _job_handle is None:

            class BasicLimits(ctypes.Structure):
                _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64), ("LimitFlags", wintypes.DWORD),
                            ("MinimumWorkingSetSize", ctypes.c_size_t), ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                            ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]

            class IoCounters(ctypes.Structure):
                _fields_ = [(name, ctypes.c_uint64) for name in ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                                                                  "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

            class ExtendedLimits(ctypes.Structure):
                _fields_ = [("BasicLimitInformation", BasicLimits), ("IoInfo", IoCounters), ("ProcessMemoryLimit", ctypes.c_size_t),
                            ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

            job = kernel32.CreateJobObjectW(None, None)
            if not job:
                return
            limits = ExtendedLimits()
            # KILL_ON_JOB_CLOSE | SILENT_BREAKAWAY_OK, as libuv sets them: the browser's own helper processes are not held in the job.
            limits.BasicLimitInformation.LimitFlags = 0x2000 | 0x1000
            kernel32.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits))
            _job_handle = job
        process = kernel32.OpenProcess(0x0100 | 0x0001, False, pid)  # PROCESS_SET_QUOTA | PROCESS_TERMINATE
        if process:
            kernel32.AssignProcessToJobObject(_job_handle, process)
            kernel32.CloseHandle(process)
    except (OSError, AttributeError):
        pass


def launch_browser(exe: str, profile_dir: str, detach: bool = False, env: Mapping[str, str] | None = None) -> BrowserHandle:
    """Starts a hidden browser on the profile.

    `detach` lets the browser outlive this process. By default it does not, which is what a hidden browser should do, so a crashed command
    leaves none behind. A browser that is left waiting for a person to scan a QR code is the opposite: it keeps running when the command
    returns, and the next command joins it.
    """
    os.makedirs(profile_dir, exist_ok=True)
    _remove(_active_port_file(profile_dir))
    args = [
        # CLS signs in through the company's Feishu SSO, whose cookies are session-only. A browser that exits normally forgets those, so
        # every later command would find nobody logged in; "continue where you left off" is what makes it keep them.
        f"--user-data-dir={profile_dir}", "--restore-last-session", "--remote-debugging-port=0", "--no-first-run", "--no-default-browser-check",
        "--disable-sync", "--disable-background-networking", "--disable-component-update", "--password-store=basic", "--disable-features=Translate",
        "--headless=new", "--disable-gpu", "--hide-scrollbars", "--mute-audio",
        "about:blank",
    ]
    options: dict[str, Any] = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
                               "env": dict(env) if env is not None else None, "close_fds": True}
    if sys.platform == "win32":
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = 0  # SW_HIDE
        options["startupinfo"] = startup
        if detach:
            options["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    elif detach:
        options["start_new_session"] = True
    start_failure = ""
    child: subprocess.Popen[bytes] | None = None
    try:
        child = subprocess.Popen([exe, *args], **options)
    except OSError as error:
        start_failure = str(error)
    if child is not None and not detach:
        _kill_with_this_process(child.pid)

    def exited() -> bool:
        return child is None or child.poll() is not None

    found: tuple[int, str] | None = None
    waited = 0
    while waited < 30_000 and not found:
        found = read_active_port(profile_dir)
        if found or exited():
            break
        time.sleep(0.1)
        waited += 100
    if not found:
        if child is not None and not exited():
            _kill_tree(child.pid)
        raise Blocked(f"浏览器没有启动成功{f'（{start_failure}）' if start_failure else ''}；确认已安装 Chrome 或 Edge，或用 {BROWSER_ENV} 指定浏览器路径")
    assert child is not None
    port, endpoint = found
    try:
        cdp = Cdp.connect(f"ws://127.0.0.1:{port}{endpoint}")
    except CdpError as error:
        _kill_tree(child.pid)
        raise Blocked(f"无法连接浏览器的调试接口：{error}") from None
    _close_spare_blank_tabs(cdp)

    def close() -> None:
        try:
            cdp.send("Browser.close", {}, None, 5_000)
        except CdpError:
            pass
        waited = 0
        while not exited() and waited < 8_000:
            time.sleep(0.1)
            waited += 100
        if not exited():
            _kill_tree(child.pid)
        cdp.close()

    return BrowserHandle(cdp=cdp, launched=True, close=close)


def _close_spare_blank_tabs(cdp: Cdp) -> None:
    """Restoring the last session brings back last time's tabs, and the address given on the command line opens one more blank tab on top
    of them, so the blank tabs would add up with every start. One is kept for the command to use."""
    try:
        targets = cdp.send("Target.getTargets").get("targetInfos") or []
        blanks = [t for t in targets if t.get("type") == "page" and t.get("url") == "about:blank"]
        for spare in blanks[1:]:
            try:
                cdp.send("Target.closeTarget", {"targetId": spare.get("targetId")})
            except CdpError:
                pass
    except (CdpError, AttributeError, TypeError):
        pass  # the tabs are only tidied; the browser works either way


def open_browser(exe: str, profile_dir: str, detach: bool = False, env: Mapping[str, str] | None = None) -> BrowserHandle:
    """Joins the browser on this profile or starts one; another command using the profile is waited for."""
    release = lock_profile(profile_dir)
    try:
        handle = _join_browser(profile_dir) or launch_browser(exe, profile_dir, detach, env)
        return _holding(handle, release)
    except BaseException:
        release()
        raise
