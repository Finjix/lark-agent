"""Where a command's time goes: phases timed in this process, one line per command in <data>/timing.jsonl, and `gof.py timing` to read them.

A line holds the command (its group and name, never its arguments), the outcome, the wall-clock start and the total, and per phase name the
count, total and longest time in milliseconds. Phases nest (a Feishu export inside `feishu append`), so they are not meant to add up. The
gap between one command's end and the next one's start is the agent's own time (thinking, reading, writing files) and `timing` shows it too.

Nothing here may stop or change a command: a failure to time or to write the log is ignored. The log holds no IDs, paths, names or tokens.
"""

from __future__ import annotations

import json
import os
import re
import time
from contextlib import contextmanager
from typing import Any, Iterator, Optional

LOG_NAME = "timing.jsonl"
#: The log is cut back to its newer half past this size.
MAX_BYTES = 2_000_000

_start = time.perf_counter()
_wall = time.time()
_phases: dict[str, list[float]] = {}


def reset() -> None:
    """For tests: forget what was timed and start the clock again."""
    global _start, _wall
    _start = time.perf_counter()
    _wall = time.time()
    _phases.clear()


def add(name: str, seconds: float) -> None:
    entry = _phases.setdefault(name, [0, 0.0, 0.0])
    entry[0] += 1
    entry[1] += seconds
    entry[2] = max(entry[2], seconds)


@contextmanager
def phase(name: str) -> Iterator[None]:
    began = time.perf_counter()
    try:
        yield
    finally:
        try:
            add(name, time.perf_counter() - began)
        except Exception:
            pass


_ID = re.compile(r"/(?:\d+|[A-Za-z0-9_-]{20,})(?=/|$)")


def http_name(method: str, url: str) -> str:
    """`http GET host/api/path` with numbers and long tokens in the path replaced by `:id`, the query left out."""
    rest = url.split("://", 1)[-1].split("?", 1)[0].split("#", 1)[0]
    return f"http {method.upper()} {_ID.sub('/:id', rest)}"


def command_name(argv: list[str]) -> str:
    words = []
    for arg in argv[:2]:
        if arg.startswith("-"):
            break
        words.append(arg)
    return " ".join(words) or "(none)"


def snapshot(command: str, status: Any, code: int) -> dict:
    now = time.perf_counter()
    return {"command": command, "status": status if isinstance(status, str) else None, "code": code,
            "start": round(_wall, 3), "total_ms": round((now - _start) * 1000),
            "phases": {name: {"count": int(c), "ms": round(total * 1000), "max_ms": round(longest * 1000)}
                       for name, (c, total, longest) in sorted(_phases.items())}}


def log_file(root: str) -> str:
    return os.path.join(root, LOG_NAME)


def write(root: str, entry: dict) -> None:
    """Appends the entry; never raises."""
    try:
        os.makedirs(root, exist_ok=True)
        file = log_file(root)
        try:
            if os.path.getsize(file) > MAX_BYTES:
                with open(file, "rb") as handle:
                    data = handle.read()
                keep = data[len(data) // 2:]
                keep = keep[keep.find(b"\n") + 1:]
                with open(file, "wb") as handle:
                    handle.write(keep)
        except OSError:
            pass
        with open(file, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n")
    except Exception:
        pass


def read(root: str) -> list[dict]:
    try:
        with open(log_file(root), encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict) and isinstance(entry.get("start"), (int, float)) and isinstance(entry.get("total_ms"), (int, float)):
            out.append(entry)
    return out


def report(entries: list[dict], since: Optional[float] = None, last: Optional[int] = None, idle_gap_s: float = 600.0) -> dict:
    """
    The commands in order with the agent's time before each (`gap_ms`: since the previous command ended, left out after more than
    `idle_gap_s`, which is a new task or a person away), and totals per command and per phase.
    """
    chosen = sorted((e for e in entries if since is None or e["start"] >= since), key=lambda e: e["start"])
    if last is not None:
        chosen = chosen[-last:] if last > 0 else []
    runs: list[dict] = []
    by_command: dict[str, dict[str, Any]] = {}
    by_phase: dict[str, dict[str, Any]] = {}
    previous_end: Optional[float] = None
    agent_ms = 0
    for entry in chosen:
        gap = None
        if previous_end is not None:
            seconds = entry["start"] - previous_end
            if 0 <= seconds <= idle_gap_s:
                gap = round(seconds * 1000)
                agent_ms += gap
        previous_end = entry["start"] + entry["total_ms"] / 1000
        phases = entry.get("phases") if isinstance(entry.get("phases"), dict) else {}
        top = sorted(phases.items(), key=lambda kv: -kv[1].get("ms", 0))[:3]
        run = {"command": entry.get("command"), "status": entry.get("status"), "total_ms": entry["total_ms"],
               "slowest": {k: v.get("ms") for k, v in top}}
        if gap is not None:
            run["gap_ms"] = gap
        runs.append(run)
        c = by_command.setdefault(str(entry.get("command")), {"count": 0, "ms": 0, "max_ms": 0})
        c["count"] += 1
        c["ms"] += entry["total_ms"]
        c["max_ms"] = max(c["max_ms"], entry["total_ms"])
        for name, value in phases.items():
            p = by_phase.setdefault(name, {"count": 0, "ms": 0, "max_ms": 0})
            p["count"] += value.get("count", 0)
            p["ms"] += value.get("ms", 0)
            p["max_ms"] = max(p["max_ms"], value.get("max_ms", 0))
    commands_ms = sum(e["total_ms"] for e in chosen)
    return {"status": "ok", "runs": runs, "commands_ms": commands_ms, "agent_ms": agent_ms,
            "by_command": dict(sorted(by_command.items(), key=lambda kv: -kv[1]["ms"])),
            "by_phase": dict(sorted(by_phase.items(), key=lambda kv: -kv[1]["ms"]))}
