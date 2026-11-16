"""How requirement-auto reaches the feishu skill.

Feishu is the feishu skill's: the login, the allowlisted API calls and the hidden browser that holds the user's Feishu web session all
live there, shared with every skill that works in Feishu, so the user logs in once. This module finds the skill's feishu.py and runs it
with this process's environment (the host's user directory, Open ID and app), so it serves the same user. The contract is the feishu
skill's references/for-other-skills.md (`serve` line protocol, `bridge <command>` with JSON on stdin, `feishu-sdk-bridge/1`); nothing of the skill
is imported.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from typing import Any, Mapping, Optional

from .util import Blocked

#: The bridge protocol this code speaks; the skill says its own in `info.bridge`.
BRIDGE_VERSION = "feishu-sdk-bridge/1"
FEISHU_SKILL_ENV = "FEISHU_SKILL"
MISSING = "没有找到 feishu 技能（飞书登录和飞书接口都由它提供）：lark-agent 里把 feishu 技能文件夹放进 skills/ 并重启"

#: This skill's own folder (scripts/gof/feishu_skill.py → skills/requirement-auto).
SKILL_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def locate_feishu(env: Optional[Mapping[str, str]] = None, skill_dir: Optional[str] = None) -> str:
    """Where feishu.py is: FEISHU_SKILL when it is set (an absolute path), else the feishu skill next to this one (`skill_dir`: tests)."""
    env = os.environ if env is None else env
    named = (env.get(FEISHU_SKILL_ENV) or "").strip()
    if named:
        if os.path.isabs(named) and os.path.isfile(named):
            return named
        raise Blocked(f"{FEISHU_SKILL_ENV} 不是 feishu.py 的绝对路径")
    beside = os.path.join(os.path.dirname(skill_dir or SKILL_DIR), "feishu", "scripts", "feishu.py")
    if os.path.isfile(beside):
        return beside
    raise Blocked(MISSING)


def _child_env(env: Optional[Mapping[str, str]]) -> dict[str, str]:
    result = dict(os.environ if env is None else env)
    # The answers are JSON in UTF-8 whatever the console's code page.
    result.setdefault("PYTHONIOENCODING", "utf-8")
    result.setdefault("PYTHONUTF8", "1")
    return result


def run_feishu(args: list[str], env: Optional[Mapping[str, str]] = None) -> dict:
    """A feishu.py command a skill would run (login, authorize): its one line of JSON and its exit code, as {"result", "code"}."""
    entry = locate_feishu(env)
    completed = subprocess.run([sys.executable, entry, *args], env=_child_env(env), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=None, creationflags=_NO_WINDOW)
    out = completed.stdout.decode("utf-8", "replace").strip()
    try:
        result = json.loads(out.split("\n")[-1] if out else "")
    except ValueError:
        raise Blocked("feishu 技能没有给出结果") from None
    return {"result": result, "code": completed.returncode if completed.returncode is not None else 2}


class FeishuServe:
    """
    The feishu skill's bridge, one `feishu.py serve` process for the whole of this command: started at the first call, ended by close()
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
                    raise ConnectionError("feishu 技能进程已结束")
                try:
                    message = json.loads(raw.decode("utf-8", "replace"))
                except ValueError:
                    continue
                if isinstance(message, dict) and message.get("id") == ident and isinstance(message.get("id"), int) and "reply" in message:
                    return message["reply"]

    def close(self) -> None:
        """Lets the skill's process finish what it has and end."""
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
    The profile of this user's hidden browser, which is the feishu skill's: its Feishu web session is what CLS's single sign-on and the
    sheet screenshots need, so one scan covers them all. Asked of the skill once per user directory.
    """
    env = os.environ if env is None else env
    key = env.get("LARK_AGENT_USER_DIR", "")
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
        raise Blocked("feishu 技能没有给出浏览器的位置") from None
    data = info.get("data") if isinstance(info, dict) else None
    profile = data.get("browser_profile") if isinstance(data, dict) else None
    if not isinstance(info, dict) or info.get("ok") is not True or not isinstance(profile, str) or not os.path.isabs(profile):
        detail = ""
        if isinstance(info, dict) and info.get("ok") is False:
            error = info.get("error") if isinstance(info.get("error"), dict) else {}
            detail = f"：{error.get('message') or ''}"
        raise Blocked(f"feishu 技能没有给出浏览器的位置{detail}")
    _profiles[key] = profile
    return profile
