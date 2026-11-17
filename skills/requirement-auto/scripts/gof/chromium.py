"""A minimal Chromium driver.

Find the Chrome or Edge that is already installed, start it with its own profile folder and speak the DevTools protocol to it over a
WebSocket. No download, no third-party package, no UI automation: it exists so that a person can log in to a web site in an ordinary
browser and the login can then be observed.

The agent's Feishu module drives the same profile (its Feishu web session is what CLS signs in with) with its own copy of this code. The two share
the profile through the lock below (a file beside the profile, `browser-profile.lock`: {"pid","at","token"}) and the DevToolsActivePort file
in it, so those two must stay the same in both: change them together or not at all.
"""

from __future__ import annotations

import base64
import json
import os
import queue
import secrets
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
from typing import Any, Callable, Mapping, Optional
from urllib.parse import urlsplit

from .util import Blocked

#: Environment variable that names the browser executable to use instead of searching for one, the Feishu module's too: both must
#: start the same browser on the profile they share, or the Feishu web session would not carry over.
BROWSER_ENV = "FEISHU_BROWSER"


def browser_candidates(platform: str = sys.platform, env: Optional[Mapping[str, str]] = None) -> list[str]:
    """The browsers searched, in order, when the environment does not name one. Edge ships with Windows 10 and 11, so it is the usual find there."""
    env = os.environ if env is None else env
    if platform == "win32":
        # The usual folders are listed too: a process whose environment was cut down (a shell started by an agent, say) may not carry these.
        roots = [r for r in (env.get("ProgramFiles(x86)"), env.get("ProgramFiles"), env.get("LOCALAPPDATA"), "C:\\Program Files (x86)", "C:\\Program Files") if r]
        relative = [["Microsoft", "Edge", "Application", "msedge.exe"], ["Google", "Chrome", "Application", "chrome.exe"], ["Chromium", "Application", "chrome.exe"]]
        return [os.path.join(root, *parts) for parts in relative for root in roots]
    if platform == "darwin":
        return ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
                "/Applications/Chromium.app/Contents/MacOS/Chromium"]
    return ["/usr/bin/google-chrome", "/usr/bin/google-chrome-stable", "/usr/bin/microsoft-edge", "/usr/bin/chromium", "/usr/bin/chromium-browser", "/snap/bin/chromium"]


def find_browser(env: Optional[Mapping[str, str]] = None, platform: str = sys.platform) -> Optional[str]:
    env = os.environ if env is None else env
    named = (env.get(BROWSER_ENV) or "").strip()
    if named:
        return named if os.path.isabs(named) and os.path.isfile(named) else None
    return next((f for f in browser_candidates(platform, env) if os.path.isfile(f)), None)


# ---- a WebSocket client, just enough for DevTools on 127.0.0.1 --------------------------------------------------------------------


class _WebSocket:
    def __init__(self, url: str, timeout: float) -> None:
        parts = urlsplit(url)
        self._sock = socket.create_connection((parts.hostname or "127.0.0.1", parts.port or 80), timeout=timeout)
        try:
            self._handshake(parts)
        except BaseException:
            self._sock.close()
            raise
        self._sock.settimeout(None)
        self._send_lock = threading.Lock()

    def _handshake(self, parts) -> None:
        key = base64.b64encode(secrets.token_bytes(16)).decode()
        path = parts.path + (f"?{parts.query}" if parts.query else "")
        request = (f"GET {path or '/'} HTTP/1.1\r\nHost: {parts.hostname}:{parts.port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                   f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
        self._sock.sendall(request.encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise ConnectionError("DevTools connection failed")
            head += chunk
        status, _, rest = head.partition(b"\r\n\r\n")
        if b" 101 " not in status.split(b"\r\n", 1)[0] + b" ":
            raise ConnectionError("DevTools connection failed")
        self._buffer = rest

    def _read(self, n: int) -> bytes:
        while len(self._buffer) < n:
            chunk = self._sock.recv(max(65536, n - len(self._buffer)))
            if not chunk:
                raise ConnectionError("closed")
            self._buffer += chunk
        data, self._buffer = self._buffer[:n], self._buffer[n:]
        return data

    def receive(self) -> Optional[str]:
        """The next text message, or None when the connection closed."""
        message = bytearray()
        while True:
            try:
                first, second = self._read(2)
            except (ConnectionError, OSError):
                return None
            opcode = first & 0x0F
            length = second & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._read(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._read(8))[0]
            mask = self._read(4) if second & 0x80 else b""
            payload = self._read(length) if length else b""
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if opcode == 0x8:
                return None
            if opcode == 0x9:
                self._frame(0xA, payload)
                continue
            if opcode == 0xA:
                continue
            message += payload
            if first & 0x80:
                return message.decode("utf-8", "replace")

    def _frame(self, opcode: int, payload: bytes) -> None:
        header = bytearray([0x80 | opcode])
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", length)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", length)
        mask = secrets.token_bytes(4)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        with self._send_lock:
            self._sock.sendall(bytes(header) + masked)

    def send(self, text: str) -> None:
        self._frame(0x1, text.encode("utf-8"))

    def close(self) -> None:
        try:
            self._frame(0x8, b"")
        except OSError:
            pass
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self._sock.close()
        except OSError:
            pass


class CdpError(Exception):
    pass


Handler = Callable[[dict, Optional[str]], None]


class Cdp:
    """
    A DevTools protocol connection: commands with replies, events, and flattened per-target sessions. A reader thread takes every message;
    handlers run on it and must not wait for a reply themselves (they hand work to the main thread instead).
    """

    def __init__(self, ws: _WebSocket) -> None:
        self._ws = ws
        self._next_id = 1
        self._pending: dict[int, "queue.Queue[tuple[bool, Any]]"] = {}
        self._handlers: dict[str, list[Handler]] = {}
        self._closers: list[Callable[[], None]] = []
        self._lock = threading.Lock()
        self.closed = False
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    @staticmethod
    def connect(url: str, timeout_ms: int = 10_000) -> "Cdp":
        try:
            return Cdp(_WebSocket(url, timeout_ms / 1000))
        except socket.timeout:
            raise CdpError("DevTools connection timed out") from None
        except (OSError, ConnectionError):
            raise CdpError("DevTools connection failed") from None

    def _read_loop(self) -> None:
        while True:
            text = self._ws.receive()
            if text is None:
                break
            try:
                message = json.loads(text)
            except ValueError:
                continue
            if isinstance(message.get("id"), int):
                with self._lock:
                    waiting = self._pending.pop(message["id"], None)
                if waiting is None:
                    continue
                if message.get("error"):
                    waiting.put((False, str((message["error"] or {}).get("message") or "DevTools command failed")))
                else:
                    waiting.put((True, message.get("result") or {}))
            elif isinstance(message.get("method"), str):
                for handler in list(self._handlers.get(message["method"], [])):
                    try:
                        handler(message.get("params") or {}, message.get("sessionId"))
                    except Exception:
                        pass
        self._finish()

    def _finish(self) -> None:
        with self._lock:
            if self.closed:
                return
            self.closed = True
            pending = list(self._pending.values())
            self._pending.clear()
        for waiting in pending:
            waiting.put((False, "DevTools connection closed"))
        for closer in list(self._closers):
            try:
                closer()
            except Exception:
                pass

    def send(self, method: str, params: Optional[dict] = None, session_id: Optional[str] = None, timeout_ms: int = 30_000) -> dict:
        if self.closed:
            raise CdpError("DevTools connection closed")
        reply: "queue.Queue[tuple[bool, Any]]" = queue.Queue(1)
        with self._lock:
            ident = self._next_id
            self._next_id += 1
            self._pending[ident] = reply
        message: dict[str, Any] = {"id": ident, "method": method, "params": params or {}}
        if session_id:
            message["sessionId"] = session_id
        try:
            self._ws.send(json.dumps(message))
        except OSError:
            with self._lock:
                self._pending.pop(ident, None)
            raise CdpError("DevTools connection closed") from None
        try:
            ok, value = reply.get(timeout=timeout_ms / 1000)
        except queue.Empty:
            with self._lock:
                self._pending.pop(ident, None)
            raise CdpError(f"DevTools command timed out: {method}") from None
        if not ok:
            raise CdpError(value)
        return value

    def on(self, method: str, handler: Handler) -> Callable[[], None]:
        self._handlers.setdefault(method, []).append(handler)

        def off() -> None:
            try:
                self._handlers[method].remove(handler)
            except (KeyError, ValueError):
                pass

        return off

    def on_close(self, handler: Callable[[], None]) -> None:
        """Called once when the connection ends, whichever side ended it (it is called at once if it already has)."""
        if self.closed:
            handler()
        else:
            self._closers.append(handler)

    def close(self) -> None:
        self._ws.close()
        self._finish()


class BrowserHandle:
    """`launched` is True when this call started the browser; False when it joined one already running on the profile."""

    def __init__(self, cdp: Cdp, launched: bool, closer: Callable[[], None]) -> None:
        self.cdp = cdp
        self.launched = launched
        self._closer = closer

    def close(self) -> None:
        """Closes the browser the graceful way, so cookies and local storage are written to the profile; ends it by force if it will not go."""
        self._closer()


def _active_port_file(profile_dir: str) -> str:
    return os.path.join(profile_dir, "DevToolsActivePort")


def _read_active_port(profile_dir: str) -> Optional[tuple[int, str]]:
    try:
        with open(_active_port_file(profile_dir), "r", encoding="utf-8") as handle:
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
        taskkill = os.path.join(os.environ.get("SystemRoot", "C:\\Windows"), "System32", "taskkill.exe")
        subprocess.run([taskkill, "/PID", str(pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    else:
        try:
            os.kill(pid, 9)
        except OSError:
            pass


def _endpoint_on(port: int, timeout_ms: int = 3_000) -> Optional[str]:
    """The path of the browser endpoint that answers on this port (/devtools/browser/<id>, '' when it does not say), or None when none answers."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"http://127.0.0.1:{port}/json/version", timeout=timeout_ms / 1000) as response:
            if not 200 <= response.status < 300:
                return None
            body = response.read()
    except Exception:
        return None
    try:
        url = json.loads(body).get("webSocketDebuggerUrl")
    except Exception:
        return ""
    if not isinstance(url, str):
        return ""
    try:
        return urlsplit(url).path
    except ValueError:
        return ""


def _answers(port: int, timeout_ms: int = 3_000) -> bool:
    """Whether a browser's DevTools endpoint answers on this port."""
    return _endpoint_on(port, timeout_ms) is not None


def _join_browser(profile_dir: str) -> Optional[BrowserHandle]:
    """Joins a browser that is already running on this profile (a login window left open by an earlier command), if there is one."""
    found = _read_active_port(profile_dir)
    if not found:
        return None
    port, endpoint = found
    url = f"ws://127.0.0.1:{port}{endpoint}"
    try:
        cdp = Cdp.connect(url, 2_000)
    except CdpError:
        # The port file of a browser that was killed stays behind, and its port may belong to another browser by now; a running one that is
        # only slow to answer must not be taken for gone: a second browser started on its profile is handed over to it by Chrome and exits,
        # and the port file deleted on the way leaves it unreachable.
        answering = _endpoint_on(port)
        if answering is None or (answering and answering != endpoint):
            return None
        try:
            cdp = Cdp.connect(url, 15_000)
        except CdpError:
            raise Blocked("this user's browser is running but its DevTools port does not answer; try again a little later") from None

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
        while waited < 8_000 and _answers(port, 500):
            time.sleep(0.25)
            waited += 250

    return BrowserHandle(cdp, False, close)


# ---- the profile lock -------------------------------------------------------------------------------------------------------------
#
# One command at a time drives a profile's browser. Two would share its first tab and one would close the browser under the other. The lock
# is a file beside the profile, held from opening the browser until this process lets go of it: by closing the browser, or only its
# connection when the browser is left running for a scan. The holder renews it every LOCK_RENEW_MS. A holder whose process has ended does
# not count, and neither does one that has not renewed it for LOCK_STALE_MS (Windows soon gives a dead process's id to another one, which
# would otherwise keep the lock alive). Within one process the lock is shared (nested opens of a profile are the caller's own business).

LOCK_WAIT_MS = 120_000
LOCK_RENEW_MS = 30_000
LOCK_STALE_MS = 2 * 60_000
_held: dict[str, dict] = {}
_held_lock = threading.Lock()


def _now_ms() -> int:
    return int(time.time() * 1000)


def alive(pid: int) -> bool:
    """Whether a process is running (``process.kill(pid, 0)``; a process this one may not touch is running too)."""
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() == 5  # ERROR_ACCESS_DENIED: it exists
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
    except OSError:
        return False


def _abandoned(file: str, text: str) -> bool:
    """Whether a lock file is of nobody any more: its process ended, it has not been renewed for long, or it was never finished being written."""
    holder: Any = None
    try:
        holder = json.loads(text)
    except ValueError:
        pass  # half written
    pid = holder.get("pid") if isinstance(holder, dict) else None
    at = holder.get("at") if isinstance(holder, dict) else None
    if not isinstance(pid, (int, float)) or isinstance(pid, bool) or not isinstance(at, (int, float)) or isinstance(at, bool):
        try:
            age = _now_ms() - os.stat(file).st_mtime * 1000
        except OSError:
            age = _now_ms()
        return age > 10_000
    return not alive(int(pid)) or _now_ms() - at > LOCK_STALE_MS


def _owned_by(file: str, token: str) -> bool:
    """Whether the lock file is still the one this holder wrote (it may have been taken over as abandoned meanwhile)."""
    try:
        with open(file, "r", encoding="utf-8") as handle:
            return json.load(handle).get("token") == token
    except Exception:
        return False


def lock_profile(profile_dir: str, wait_ms: int = LOCK_WAIT_MS) -> Callable[[], None]:
    """Takes this profile's lock (waiting for another command's) and returns the function that lets go of it."""
    file = f"{os.path.abspath(profile_dir)}.lock"
    released = [False]

    def release() -> None:
        if released[0]:
            return
        released[0] = True
        with _held_lock:
            mine = _held.get(file)
            if not mine:
                return
            mine["count"] -= 1
            if mine["count"] > 0:
                return
            del _held[file]
        mine["stop"].set()
        if _owned_by(file, mine["token"]):
            try:
                os.remove(file)
            except OSError:
                pass

    with _held_lock:
        mine = _held.get(file)
        if mine:
            mine["count"] += 1
            return release
    os.makedirs(os.path.dirname(file), exist_ok=True)
    token = str(uuid.uuid4())

    def content() -> str:
        return json.dumps({"pid": os.getpid(), "at": _now_ms(), "token": token}, separators=(",", ":"))

    deadline = _now_ms() + wait_ms
    while True:
        try:
            with open(file, "x", encoding="utf-8") as handle:
                handle.write(content())
            stop = threading.Event()

            def renew() -> None:
                while not stop.wait(LOCK_RENEW_MS / 1000):
                    if _owned_by(file, token):
                        try:
                            with open(file, "w", encoding="utf-8") as handle:
                                handle.write(content())
                        except OSError:
                            pass  # renewed at the next beat

            threading.Thread(target=renew, daemon=True).start()
            with _held_lock:
                _held[file] = {"count": 1, "token": token, "stop": stop}
            return release
        except FileExistsError:
            pass
        except PermissionError:
            pass  # Windows: the file is being replaced at this moment
        text: Optional[str] = None
        try:
            with open(file, "r", encoding="utf-8") as handle:
                text = handle.read()
        except OSError:
            pass  # let go of a moment ago, or not a file that can be read: waited on like a held lock
        if text is not None and _abandoned(file, text):
            # Taken away under a name of its own, so that of several waiters seeing the same dead lock none removes a lock just made by another.
            aside = f"{file}.{os.getpid()}-{secrets.token_hex(6)}"
            taken = True
            try:
                os.rename(file, aside)
            except OSError:
                taken = False
            if taken:
                moved = ""
                try:
                    with open(aside, "r", encoding="utf-8") as handle:
                        moved = handle.read()
                except OSError:
                    pass
                if moved == text:
                    try:
                        os.remove(aside)
                    except OSError:
                        pass
                else:
                    try:
                        os.rename(aside, file)
                    except OSError:
                        try:
                            os.remove(aside)
                        except OSError:
                            pass
                continue
        if _now_ms() > deadline:
            raise Blocked("this user's browser is in use by another command: only one browser command (login, login check, screenshot, upload) can run at a time; run this one after it has finished")
        time.sleep(0.25)


def _holding(handle: BrowserHandle, release: Callable[[], None]) -> BrowserHandle:
    """The handle with the profile lock: let go of when the browser is closed, or when only its connection is (the browser left running)."""
    closing = [False]
    handle.cdp.on_close(lambda: None if closing[0] else release())

    def close() -> None:
        closing[0] = True
        try:
            handle.close()
        finally:
            release()

    return BrowserHandle(handle.cdp, handle.launched, close)


def attach_browser(profile_dir: str) -> Optional[BrowserHandle]:
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


# ---- starting a browser -----------------------------------------------------------------------------------------------------------

_job_handle: Any = None


def _kill_with_this_process(pid: int) -> None:
    """
    On Windows, puts the browser in a job that ends when this process does, which is what Node did for a child it had not detached: a
    hidden browser should go with the command that started it, so a crashed command leaves none behind.
    """
    global _job_handle
    if sys.platform != "win32":
        return
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.OpenProcess.restype = wintypes.HANDLE
        if _job_handle is None:
            job = kernel32.CreateJobObjectW(None, None)
            if not job:
                return

            class Basic(ctypes.Structure):
                _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64), ("LimitFlags", wintypes.DWORD),
                            ("MinimumWorkingSetSize", ctypes.c_size_t), ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                            ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]

            class Io(ctypes.Structure):
                _fields_ = [(n, ctypes.c_uint64) for n in ("Read", "Write", "Other", "ReadT", "WriteT", "OtherT")]

            class Extended(ctypes.Structure):
                _fields_ = [("Basic", Basic), ("Io", Io), ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                            ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

            info = Extended()
            info.Basic.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            kernel32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info))
            _job_handle = job
        process = kernel32.OpenProcess(0x0100 | 0x0001, False, pid)  # PROCESS_SET_QUOTA | PROCESS_TERMINATE
        if process:
            kernel32.AssignProcessToJobObject(_job_handle, process)
            kernel32.CloseHandle(process)
    except Exception:
        pass  # the browser is closed by the command anyway; this only covers a crash


class _Browser(subprocess.Popen):
    """The browser process. It is meant to outlive the object that started it (left for a scan, joined later), so that is no warning."""

    def __del__(self, _maxsize: int = sys.maxsize, _warn: Any = None) -> None:
        super().__del__(_maxsize, lambda *args, **kwargs: None)


def launch_browser(exe: str, profile_dir: str, detach: bool = False, env: Optional[Mapping[str, str]] = None) -> BrowserHandle:
    """
    Starts the browser on the profile. `detach` lets it outlive this process: a browser left waiting for a person to scan a QR code keeps
    running when the command returns, and the next command joins it. Otherwise it ends with this process.
    """
    os.makedirs(profile_dir, exist_ok=True)
    try:
        os.remove(_active_port_file(profile_dir))
    except OSError:
        pass
    args = [
        # CLS signs in through the company's Feishu SSO, whose cookies are session-only. A browser that exits normally forgets those, so
        # every later command would find nobody logged in; "continue where you left off" is what makes it keep them.
        f"--user-data-dir={profile_dir}", "--restore-last-session", "--remote-debugging-port=0", "--no-first-run", "--no-default-browser-check",
        "--disable-sync", "--disable-background-networking", "--disable-component-update", "--password-store=basic", "--disable-features=Translate",
        "--headless=new", "--disable-gpu", "--hide-scrollbars", "--mute-audio",
        "about:blank",
    ]
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if detach and sys.platform == "win32":
        flags |= getattr(subprocess, "DETACHED_PROCESS", 0x8) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200) | 0x01000000  # CREATE_BREAKAWAY_FROM_JOB
    start_failure = ""
    try:
        try:
            child = _Browser([exe, *args], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     env=dict(env) if env is not None else None, creationflags=flags, start_new_session=detach and sys.platform != "win32")
        except OSError as error:
            if detach and sys.platform == "win32" and flags & 0x01000000:
                # A job that does not allow breaking away: the browser is started detached all the same.
                child = _Browser([exe, *args], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                         env=dict(env) if env is not None else None, creationflags=flags & ~0x01000000)
            else:
                raise error
    except OSError as error:
        start_failure = str(error)
        raise Blocked(f"the browser did not start ({start_failure}); make sure Chrome or Edge is installed, or set {BROWSER_ENV} to the browser's absolute path") from None
    if not detach:
        _kill_with_this_process(child.pid)
    found = None
    waited = 0
    while waited < 30_000:
        found = _read_active_port(profile_dir)
        if found or child.poll() is not None:
            break
        time.sleep(0.1)
        waited += 100
    if not found:
        if child.poll() is None:
            _kill_tree(child.pid)
        raise Blocked(f"the browser did not start; make sure Chrome or Edge is installed, or set {BROWSER_ENV} to the browser's absolute path")
    port, endpoint = found
    try:
        cdp = Cdp.connect(f"ws://127.0.0.1:{port}{endpoint}")
    except CdpError as error:
        _kill_tree(child.pid)
        raise Blocked(f"cannot connect to the browser's DevTools port: {error}") from None
    _close_spare_blank_tabs(cdp)

    def close() -> None:
        try:
            cdp.send("Browser.close", {}, None, 5_000)
        except CdpError:
            pass
        waited_close = 0
        while child.poll() is None and waited_close < 8_000:
            time.sleep(0.1)
            waited_close += 100
        if child.poll() is None:
            _kill_tree(child.pid)
        cdp.close()

    return BrowserHandle(cdp, True, close)


def _close_spare_blank_tabs(cdp: Cdp) -> None:
    """
    Restoring the last session brings back last time's tabs, and the address given on the command line opens one more blank tab on top of
    them, so the blank tabs would add up with every start. One is kept for the command to use.
    """
    try:
        infos = cdp.send("Target.getTargets").get("targetInfos") or []
        blanks = [t for t in infos if t.get("type") == "page" and t.get("url") == "about:blank"]
        for spare in blanks[1:]:
            try:
                cdp.send("Target.closeTarget", {"targetId": spare["targetId"]})
            except CdpError:
                pass
    except CdpError:
        pass  # the tabs are only tidied; the browser works either way


def open_browser(exe: str, profile_dir: str, detach: bool = False, env: Optional[Mapping[str, str]] = None) -> BrowserHandle:
    """Joins the browser on this profile or starts one; another command using the profile is waited for."""
    release = lock_profile(profile_dir)
    try:
        handle = _join_browser(profile_dir) or launch_browser(exe, profile_dir, detach, env)
        return _holding(handle, release)
    except BaseException:
        release()
        raise
