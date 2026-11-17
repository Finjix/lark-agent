"""Shared test helpers: a local stand-in for Feishu, a hand-moved clock, temporary directories."""

from __future__ import annotations

import atexit
import os
import shutil
import tempfile

_made: list[str] = []


def temp_dir(prefix: str) -> str:
    """A fresh temporary directory that is removed when the test process ends, whether or not the test cleaned up itself."""
    folder = tempfile.mkdtemp(prefix=prefix)
    _made.append(folder)
    return folder


@atexit.register
def _cleanup() -> None:
    for folder in _made:
        shutil.rmtree(folder, ignore_errors=True)  # still in use: left to the OS temp cleanup


class Clock:
    """A clock the tests move by hand: sleeping just advances it, so nothing waits for real."""

    def __init__(self, start: float = 1_700_000_000_000) -> None:
        self.t = start
        self.slept: list[float] = []

    def now(self) -> float:
        return self.t

    def sleep(self, ms: float) -> None:
        self.slept.append(ms)
        self.t += ms


def now_ms() -> int:
    import time

    return int(time.time() * 1000)


def ended_pid() -> int:
    """The id of a process that has just ended."""
    import subprocess
    import sys

    done = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"], capture_output=True, text=True, check=True)
    return int(done.stdout.strip())


def rm(path: str) -> None:
    shutil.rmtree(path, ignore_errors=True) if os.path.isdir(path) else (os.path.exists(path) and os.remove(path))
