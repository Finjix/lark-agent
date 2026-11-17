"""How requirement-auto reaches Feishu: through the agent's Feishu module (FEISHU_CLI).

Feishu as the user belongs to the agent's Feishu module (lark-agent's feishu-sdk/feishu.py, named by FEISHU_CLI): the user's Feishu login, the allowlisted API calls and the hidden browser that holds the
user's Feishu web session, shared with every skill that works in Feishu, so the user logs in once. This module runs that skill's feishu.py
with this process's environment (the same user directory, Open ID and app), so it serves the same user. The contract is the module's
bridge.md (`serve` line protocol, `bridge <command>` with JSON on stdin, `feishu-sdk-bridge/1`); nothing of it is imported. This
skill never logs in to Feishu.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from typing import Any, Mapping, Optional

from .util import Blocked

#: The bridge protocol this code speaks; the module says its own in `info.bridge`.
BRIDGE_VERSION = "feishu-sdk-bridge/1"
#: Where the agent's Feishu module is: the absolute path of its feishu.py, set by the host.
FEISHU_ENV = "FEISHU_CLI"
MISSING = ("The agent's Feishu module is not reachable: FEISHU_CLI must be the absolute path of its feishu.py (lark-agent sets it "
           "for every command)")


def locate_feishu(env: Optional[Mapping[str, str]] = None) -> str:
    """Where the agent's Feishu module (feishu.py) is: FEISHU_CLI."""
    env = os.environ if env is None else env
    named = (env.get(FEISHU_ENV) or "").strip()
    if not named:
        raise Blocked(MISSING)
    if os.path.isabs(named) and os.path.isfile(named):
        return named
    raise Blocked(f"{FEISHU_ENV} is not the absolute path of feishu.py")


_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _child_env(env: Optional[Mapping[str, str]]) -> dict[str, str]:
    result = dict(os.environ if env is None else env)
    # The answers are JSON in UTF-8 whatever the console's code page.
    result.setdefault("PYTHONIOENCODING", "utf-8")
    result.setdefault("PYTHONUTF8", "1")
    return result


class FeishuServe:
    """
    The Feishu module's bridge, one `feishu.py serve` process for the whole of this command: started at the first call, ended by close()
    (or when this process ends). A call whose process went away before it answered fails, which the caller treats as no answer.
    """

    def __init__(self, env: Optional[Mapping[str, str]] = None) -> None:
        self._env = env
        self._child: Optional[subprocess.Popen] = None
        self._next = 1
        self._lock = threading.Lock()

    def _start(self) -> subprocess.Popen:
        if self._child is not None and self._child.poll() is None:
            return self._child
        self._child = subprocess.Popen([sys.executable, locate_feishu(self._env), "serve"], env=_child_env(self._env), stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, stderr=None, creationflags=_NO_WINDOW)
        return self._child

    def call(self, command: str, input_: Any) -> dict:
        with self._lock:
            child = self._start()
            ident = self._next
            self._next += 1
            line = json.dumps({"id": ident, "command": command, "input": input_}, ensure_ascii=False) + "\n"
            try:
                assert child.stdin is not None
                child.stdin.write(line.encode("utf-8"))
                child.stdin.flush()
            except OSError:
                # A request written to a process that has gone (it crashed, or ended before reading) fails on the pipe: the end of its
                # output below says it gave no answer, which the caller takes as such.
                pass
            assert child.stdout is not None
            while True:
                raw = child.stdout.readline()
                if not raw:
                    self._child = None
                    for pipe in (child.stdin, child.stdout):
                        try:
                            pipe.close()
                        except OSError:
                            pass
                    try:
                        child.wait(5)
                    except subprocess.TimeoutExpired:
                        pass
                    raise ConnectionError("the Feishu module's process ended")
                try:
                    message = json.loads(raw.decode("utf-8", "replace"))
                except ValueError:
                    continue
                if isinstance(message, dict) and message.get("id") == ident and isinstance(message.get("id"), int) and "reply" in message:
                    return message["reply"]

    def close(self) -> None:
        """Lets the Feishu module's process finish what it has and end."""
        child = self._child
        self._child = None
        if child is None:
            return
        try:
            if child.stdin is not None:
                child.stdin.close()
        except OSError:
            pass
        try:
            child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()
        if child.stdout is not None:
            child.stdout.close()


_shared: Optional[FeishuServe] = None


def feishu_serve() -> FeishuServe:
    global _shared
    if _shared is None:
        _shared = FeishuServe()
    return _shared


def close_feishu() -> None:
    global _shared
    if _shared is not None:
        _shared.close()
    _shared = None


_profiles: dict[str, str] = {}


def browser_profile_dir(env: Optional[Mapping[str, str]] = None) -> str:
    """
    The profile of this user's hidden browser, which is the Feishu module's: its Feishu web session is what CLS's single sign-on and the
    sheet screenshots need, so one scan covers them all. Asked of the module once per user directory.
    """
    env = os.environ if env is None else env
    key = env.get("AGENT_USER_DIR", "")
    known = _profiles.get(key)
    if known:
        return known
    entry = locate_feishu(env)
    try:
        completed = subprocess.run([sys.executable, entry, "bridge", "info"], env=_child_env(env), input=b"{}", stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, timeout=30, creationflags=_NO_WINDOW)
        out = completed.stdout.decode("utf-8", "replace")
    except subprocess.TimeoutExpired as error:
        out = (error.stdout or b"").decode("utf-8", "replace")
    try:
        info = json.loads(out.strip().split("\n")[-1] if out.strip() else "")
    except ValueError:
        raise Blocked("the Feishu module did not say where its browser profile is") from None
    data = info.get("data") if isinstance(info, dict) else None
    profile = data.get("browser_profile") if isinstance(data, dict) else None
    if not isinstance(info, dict) or info.get("ok") is not True or not isinstance(profile, str) or not os.path.isabs(profile):
        detail = ""
        if isinstance(info, dict) and info.get("ok") is False:
            error = info.get("error") if isinstance(info.get("error"), dict) else {}
            detail = f": {error.get('message') or ''}"
        raise Blocked(f"the Feishu module did not say where its browser profile is{detail}")
    _profiles[key] = profile
    return profile
