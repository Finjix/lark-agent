"""The dynamic spec: review rules that come out of a project's review groups (port of rules.ts).

Someone @-mentions the bot in a group with 「监听本群更新审片规范」 (watch); from then on the group's messages are read (the last year
once, then every three days what is new, see references/scheduled-sync.md) and Gemini turns the rulings in them into changes of the
project's dynamic spec: add, update or remove a rule. Each rule keeps the messages it rests on. The reviews of the project apply it
together with the fixed spec.

Kept in the shared folder (<LARK_AGENT_SHARED_DIR>/audit-auto), the same for every user, in the very format the TypeScript version
wrote (both read each other's files):
  groups.json      the groups listened to: project, from when, how far they have been read
  rules/<P>.json   the dynamic spec of project P, and rules/<P>.md, the same for people
  names.json       members' display names, looked up once
  lock             one writer at a time (a user's watch and the sync every three days)
  jobs.json        the reading every three days, for lark-agent to run, while a group is listened to

A rule is {id, category, rule, fix?, example?, sources: [{chat, message, at, by?, text?}], created, updated}; a removed one adds
{removed, reason, by: 'chat'|'user'}; a book is {version: 1, project, next, rules, removed, updated}. A group is {chat, project, since,
cursor, cursorIds, watchedBy?, watchedAt, read, threads?, pending?, lastSync?, lastError?, stuck?, skipped?}: `cursor` is the time of the
newest message read and `cursorIds` the ids of the messages at exactly that time (Feishu's time filter is in seconds); `threads` holds
the threads (topics) read for new replies, with the time of the newest reply read in each (Feishu lists a thread's replies only in the
thread, not in the chat; a thread is followed from its first message until THREAD_DAYS without a reply); `stuck` is a chunk the model
keeps failing on (a picture it refuses, say) and `skipped` the chunks skipped after STUCK_TRIES failed readings (the newest 20).
"""

from __future__ import annotations

import base64
import json
import os
import re
import sys
import threading
import time
import uuid
from dataclasses import dataclass, replace
from typing import Any, Callable, TypeVar

from .config import Turn
from .errors import Blocked, need
from .gemini import GatewayDown, GatewayRefused, JsonRequest, Model, Part
from .jsutil import day, js_number, js_str, now_ms, stamp
from .lark import FeishuMessage, Lark, explain_lark
from .projects import PROJECTS, Project, fixed_spec, project_of
from .transcript import Entry, EntryContext, chunks, entries_of, entry_of, render_chunk

T = TypeVar("T")
Book = dict[str, Any]
Group = dict[str, Any]
Source = dict[str, Any]

#: The parts of a spec a rule belongs to: the sections of the fixed specs.
CATEGORIES = ["红线", "字幕和口播", "画面和界面", "和游戏对得上", "按素材类型", "命名", "成片质量", "其他"]
TYPES = re.compile(r"素材类型|3D|玩法|解说|迭代|真人|实拍|AI|明星|KOL", re.IGNORECASE)
DAY = 24 * 60 * 60_000
#: How far back the first reading of a group goes unless the user says.
HISTORY_DAYS = 365
PICTURE_TYPES = {"image/png", "image/jpeg", "image/webp", "image/heic", "image/heif"}
PICTURE_MAX = 8 * 1024 * 1024
#: Pictures one model call carries at most, in bytes (the request goes inline).
PICTURES_BYTES = 16 * 1024 * 1024
#: A thread (topic) is read for new replies until it has had none for this long.
THREAD_DAYS = 14
#: Messages from before a reading that its replies point to, looked up at most per reading.
PARENTS_MAX = 50
#: Messages from just before a reading that go with it, so that a new one that refers to them without replying (「那个也不行」) makes sense.
CONTEXT = 20
CONTEXT_CHARS = 300
#: Readings in a row the model may fail on the same chunk before the chunk is skipped.
STUCK_TRIES = 3
_BOM = "\N{ZERO WIDTH NO-BREAK SPACE}"

# ---- files


def book_file(shared: str, project: str) -> str:
    return os.path.join(shared, "rules", f"{project}.json")


def groups_file(shared: str) -> str:
    return os.path.join(shared, "groups.json")


def names_file(shared: str) -> str:
    return os.path.join(shared, "names.json")


def read_json(file: str, fallback: T) -> T:
    try:
        with open(file, encoding="utf-8") as handle:
            text = handle.read()
    except FileNotFoundError:
        return fallback
    try:
        return json.loads(text[1:] if text.startswith(_BOM) else text)
    except ValueError:
        raise Blocked(f"动态规范的数据文件坏了（{os.path.basename(file)}），请联系管理员") from None


def dump(value: Any, pretty: bool = True) -> str:
    """JSON as JSON.stringify writes it: two-space indented (or compact), non-ASCII as it is."""
    if pretty:
        return json.dumps(value, ensure_ascii=False, indent=2)
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def write_atomic(file: str, content: str) -> None:
    """Written whole or not at all: a reader never sees half a file. On Windows a program that has the file open (a virus scanner,
    say) can refuse the replacement for a moment, so it is tried again for a few seconds."""
    os.makedirs(os.path.dirname(file), exist_ok=True)
    temp = f"{file}.{os.getpid()}.{threading.get_ident()}.tmp"
    with open(temp, "w", encoding="utf-8", newline="") as handle:
        handle.write(content)
    attempt = 0
    while True:
        attempt += 1
        try:
            os.replace(temp, file)
            return
        except PermissionError:
            if attempt >= 10:
                _remove(temp)
                raise
            time.sleep(0.1 * attempt)
        except OSError:
            _remove(temp)
            raise


def _remove(file: str) -> None:
    try:
        os.remove(file)
    except OSError:
        pass


def load_book(shared: str, project: str) -> Book:
    return read_json(book_file(shared, project), {"version": 1, "project": project, "next": 1, "rules": [], "removed": [], "updated": 0})


def save_book(shared: str, book: Book) -> None:
    write_atomic(book_file(shared, book["project"]), f"{dump(book)}\n")
    write_atomic(re.sub(r"\.json$", ".md", book_file(shared, book["project"])), render_book(book))


def load_groups(shared: str) -> dict[str, Any]:
    return read_json(groups_file(shared), {"version": 1, "groups": []})


def jobs_file(shared: str) -> str:
    return os.path.join(shared, "jobs.json")


#: The reading of the groups listened to every three days (references/scheduled-sync.md), for lark-agent to run: it reads jobs.json in this skill's
#: shared folder and runs the job in the skill's folder.
SYNC_JOB = {"jobs": [{"name": "audit-rules-sync", "everyMinutes": 4320, "timeoutMinutes": 30, "command": "python",
                      "args": ["scripts/audit.py", "rules", "sync"]}]}


def save_groups(shared: str, groups: dict[str, Any]) -> None:
    write_atomic(groups_file(shared), f"{dump(groups)}\n")
    keep_sync_job(shared, bool(groups["groups"]))


def keep_sync_job(shared: str, listening: bool) -> None:
    """The reading every three days is wanted while a group is listened to: jobs.json is there exactly then (written as SYNC_JOB is now)."""
    file = jobs_file(shared)
    if not listening:
        _remove(file)
        return
    wanted = f"{dump(SYNC_JOB)}\n"
    if _read_text(file) != wanted:
        write_atomic(file, wanted)


class Busy(Blocked):
    """Busy: another process is changing the dynamic spec."""


# ---- the lock

def _alive(pid: int) -> bool:
    """Whether a process is running (process.kill(pid, 0) in Node). On Windows os.kill would end it, so it is asked through the API."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            # No such process (ERROR_INVALID_PARAMETER); any other refusal (access denied) means it is there.
            return ctypes.get_last_error() != 87
        try:
            code = wintypes.DWORD()
            if kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
                return code.value == 259  # STILL_ACTIVE
            return True
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _read_text(file: str) -> str | None:
    try:
        with open(file, encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        return None


def _stale_lock(file: str) -> str | None:
    """The lock's content when its holder died or went silent (no heartbeat for 10 minutes), else None."""
    text = _read_text(file)
    if text is None:
        return None
    try:
        held = json.loads(text)
        pid, at = int(held["pid"]), float(held["at"])
    except (ValueError, KeyError, TypeError):
        # Being written this very moment, or left empty by a process that died before writing it.
        try:
            return text if now_ms() - os.stat(file).st_mtime * 1000 > 30_000 else None
        except OSError:
            return None
    if now_ms() - at > 10 * 60_000:
        return text
    return None if _alive(pid) else text


def _take_over(file: str, seen: str) -> None:
    """Moves a dead holder's lock out of the way. Of several waiters that found it stale only one moves it; one that moved a lock
    another waiter had just taken (not the stale one it saw) puts it back."""
    aside = f"{file}.{uuid.uuid4()}.stale"
    try:
        os.rename(file, aside)
    except OSError:
        return
    moved = _read_text(aside) or ""
    if moved != seen:
        try:
            os.link(aside, file)
        except OSError:
            pass  # a lock is there again
    _remove(aside)


def with_lock(shared: str, fn: Callable[[], T], wait_ms: int = 5 * 60_000) -> T:
    """Runs `fn` as the only writer of the shared folder, waiting up to `wait_ms` for another; a holder that died or went silent is
    ignored."""
    file = os.path.join(shared, "lock")
    os.makedirs(shared, exist_ok=True)
    deadline = now_ms() + wait_ms
    token = str(uuid.uuid4())

    def mine() -> str:
        return dump({"pid": os.getpid(), "token": token, "at": now_ms()}, pretty=False)

    def ours() -> bool:
        try:
            return json.loads(_read_text(file) or "").get("token") == token
        except (ValueError, AttributeError):
            return False

    while True:
        try:
            with open(file, "x", encoding="utf-8") as handle:
                handle.write(mine())
            break
        except FileExistsError:
            stale = _stale_lock(file)
            if stale is not None:
                _take_over(file, stale)
                continue
            if now_ms() >= deadline:
                raise Busy("动态规范正在更新（另一个任务正在读群消息），过几分钟再试") from None
            time.sleep(2)
    # The heartbeat and the release touch the lock only while it is still this run's.
    stop = threading.Event()

    def beat() -> None:
        while not stop.wait(60):
            if ours():
                try:
                    with open(file, "w", encoding="utf-8") as handle:
                        handle.write(mine())
                except OSError:
                    pass  # the next beat tries again

    heart = threading.Thread(target=beat, name="audit-auto-lock", daemon=True)
    heart.start()
    try:
        return fn()
    finally:
        stop.set()
        if ours():
            _remove(file)


# ---- the spec as text

def _sources_text(sources: list[Source]) -> str:
    named = list(dict.fromkeys(f"{day(s['at'])}{' ' + s['by'] if s.get('by') else ''}" for s in sources))
    return f"{'、'.join(named[-3:])} 等 {len(sources)} 条消息" if len(named) > 3 else "、".join(named)


def _said_text(sources: list[Source]) -> str:
    """The words of the newest messages a rule rests on: 王审核「以后「上头」都别写了」."""
    return "；".join(f"{s.get('by') or '成员'}「{s['text']}」" for s in [s for s in sources if s.get("text")][-2:])


def prompt_rules(book: Book, with_sources: bool = False) -> str:
    """The dynamic spec for the model: one line a rule, with its id (the review cites it, the next sync changes it by it);
    `with_sources` adds the words it came from, for the sync that may change it."""
    lines = []
    for r in book["rules"]:
        said = _said_text(r.get("sources") or []) if with_sources else ""
        lines.append(f"- {r['id']}〔{r['category']}〕{r['rule']}{' 怎么改：' + r['fix'] if r.get('fix') else ''}"
                     f"{' 例：' + r['example'] if r.get('example') else ''}（{day(r['updated'])}{'；依据：' + said if said else ''}）")
    return "\n".join(lines)


def render_book(book: Book) -> str:
    """The dynamic spec for people."""
    lines = [
        f"# {book['project']} 动态审片规范", "",
        "从审核群的聊天记录自动整理：机器人每天读一次群里的新消息，有新的审核结论就更新这里。审片时和固定规范一起用；说法和固定规范不一样的，以这里为准（这里更新）。", "",
        f"更新于 {stamp(book['updated']) if book.get('updated') else '—'} · 共 {len(book['rules'])} 条",
    ]
    if not book["rules"]:
        lines += ["", "（还没有整理出规范）"]
    for category in CATEGORIES:
        rules = [r for r in book["rules"] if (r["category"] if r["category"] in CATEGORIES else "其他") == category]
        if not rules:
            continue
        lines += ["", f"## {category}"]
        for r in rules:
            lines += ["", f"**{r['id']}** {r['rule']}"]
            if r.get("fix"):
                lines.append(f"- 怎么改：{r['fix']}")
            if r.get("example"):
                lines.append(f"- 例：{r['example']}")
            lines.append(f"- 依据：{_sources_text(r.get('sources') or [])}")
    return "\n".join(lines) + "\n"


# ---- turning messages into changes

Operation = dict[str, Any]

#: The JSON the model answers with (described in the prompt: see gemini.py for why there is no response schema).
DISTILL_SHAPE = '{"operations": [{"op": "add|update|remove", "id": "D<n>", "category": "…", "rule": "…", "fix": "…", "example": "…", "reason": "…", "messages": [1, 2]}]}'


def distill_system(project: Project, fixed: str, current: str, today: str) -> str:
    return "\n".join([
        f"You keep the dynamic review spec of the mobile game {project.name}: rules for its advertising material that come out of its review group's chat, on top of its fixed spec (the review standard). Read the new messages from the group and say how the dynamic spec changes. Today is {today} (China time).",
        "",
        "# What counts as a rule",
        "A rule says what the project's ad material may or may not contain, or how it has to be made, to pass review: a platform's rejection and its reason, a reviewer's ruling (\"X 不能用\", \"Y 可以用了\", \"Z 要写成 W\"), a new requirement, a correction or withdrawal of an earlier rule, a number or wording made precise. Leave out everything else: chatter, thanks, scheduling, who does what, where files are uploaded or delivered, questions nobody answered, and decisions about one particular material that set no rule for others.",
        "Weigh the messages: a platform's rejection or the ruling of whoever reviews counts; a guess or a question does not, until someone answers it. A reply belongs to the message it answers: a question and its answer make one ruling. Pictures (attached after the transcript, labelled with their message number) count as messages: a screenshot of a rejection notice gives its reason.",
        "Messages marked 之前的消息 were read at an earlier reading: the last ones before the new messages, and earlier ones a new message replies to. They are there so that the new messages can be understood (\"那个也不行\" refers to what came just before); whatever they ruled is in the dynamic spec already. Give operations for what the new messages say, citing earlier messages only together with new ones.",
        "",
        "# Operations",
        "Go through the messages in order and give one operation for every ruling in them; several rulings in one message are several operations.",
        "- add: a rule the dynamic spec does not have yet. When the fixed spec already says exactly the same, add nothing; when the chat goes further (stricter or looser, a word or picture the fixed spec does not list, a new replacement wording, a fresh rejection that shows how the rule is applied), add it and say in the rule which fixed rule it extends.",
        "- update: the chat changes or sharpens a rule of the dynamic spec: give its id and the whole rule as it now stands.",
        "- remove: the chat withdraws a rule of the dynamic spec, or a newer one replaces it: give its id and why.",
        "A rule is one sentence in Simplified Chinese, concrete enough to check a material against: what is not allowed or what is required, with the exact words or the kind of picture (keep the chat's own wording for forbidden words). Put what to do instead in fix and an example from the chat in example, not in the rule; keep each of them short. When something takes effect from a date, say so. Start a rule with 【红线】 when the chat treats it as a red line (account penalties, legal or public-opinion risk). Every operation lists the numbers (#n) of the messages it rests on.",
        "Most messages change nothing; when none of these do, return no operations.",
        "",
        "# Answer",
        "Answer with JSON only, in this shape:",
        DISTILL_SHAPE,
        "- id: for update and remove, the id of a rule in the dynamic spec below. A rule added in this same answer and then changed again by a later message needs no update: add it as it finally stands.",
        f"- category: one of {', '.join(CATEGORIES)} (the sections of the spec).",
        "- Leave out the fields an operation does not need.",
        "",
        "# Fixed spec",
        fixed or "(none yet)",
        "",
        "# Dynamic spec now",
        "Each rule with the date it last changed and the words of the messages it rests on (依据).",
        current or "(empty)",
    ])


_SPACES = re.compile(r"\s+")
_SQUASH = re.compile(r"[\s，。、；：！？,.;:!?「」“”\"'（）()【】]")


def _clean(value: Any, maximum: int = 500) -> str:
    return _SPACES.sub(" ", value).strip()[:maximum] if isinstance(value, str) else ""


def _squash(value: str) -> str:
    return _SQUASH.sub("", value)


def operations_of(data: Any) -> list[Operation]:
    """The operations in the model's answer: {"operations": [...]}, or the bare list (JSON mode does not hold the model to the shape)."""
    listed = data if isinstance(data, list) else data.get("operations") if isinstance(data, dict) else None
    out: list[Operation] = []
    for o in listed if isinstance(listed, list) else []:
        if not isinstance(o, dict) or o.get("op") not in ("add", "update", "remove"):
            continue
        op: Operation = {"op": o["op"]}
        if isinstance(o.get("id"), str):
            op["id"] = o["id"].strip().upper()
        for key in ("category", "rule", "fix", "example", "reason"):
            if _clean(o.get(key)):
                op[key] = _clean(o.get(key))
        # A number as the model may write it: 3, "3", "#3", or a picture label "3-1".
        found = (re.search(r"\d+", _js_string(m)) for m in (o.get("messages") if isinstance(o.get("messages"), list) else []))
        op["messages"] = [int(f.group(0)) for f in found if f]
        out.append(op)
    return out


def _js_string(value: Any) -> str:
    """String(value), as far as a message number written by the model can be anything."""
    if isinstance(value, list):
        return ",".join("" if v is None else _js_string(v) for v in value)
    if isinstance(value, dict):
        return "[object Object]"
    return "null" if value is None else js_str(value)


def _merge(a: list[Source], b: list[Source]) -> list[Source]:
    return [*a, *[s for s in b if not any(x["message"] == s["message"] for x in a)]][-10:]


def category_of(value: str | None) -> str:
    """A category as the spec names it; the material types (真人实拍, AI 类 …) are 按素材类型."""
    if not value:
        return "其他"
    found = next((c for c in CATEGORIES if c in value), None)
    if found:
        return found
    if TYPES.search(value):
        return "按素材类型"
    if re.search(r"字幕|口播|文案", value):
        return "字幕和口播"
    if re.search(r"画面|界面|UI", value, re.IGNORECASE):
        return "画面和界面"
    return "其他"


def apply(book: Book, operations: list[Operation], refs: dict[int, Source], now: int, earlier: set[int] | None = None) -> dict[str, list[str]]:
    """Applies the model's operations to the spec. One that names no message of this chunk, or only messages from an earlier reading
    (`earlier`, their numbers), or a rule that does not exist, is not taken."""
    earlier = earlier or set()
    changes: dict[str, list[str]] = {"added": [], "updated": [], "removed": []}
    # An answer may give a rule it adds an id of its own and change it further down: that id stands for the rule's real one.
    given: dict[str, str] = {}

    def id_of(rule_id: str | None) -> str | None:
        return given.get(rule_id, rule_id) if rule_id else None

    for op in operations:
        sources = [refs[n] for n in dict.fromkeys(op["messages"]) if n in refs]
        if not any(n in refs and n not in earlier for n in op["messages"]):
            continue
        if op["op"] == "add":
            if not op.get("rule"):
                continue
            same = next((r for r in book["rules"] if _squash(r["rule"]) == _squash(op["rule"])), None)
            if same:
                same["sources"] = _merge(same["sources"], sources)
                same["updated"] = now
                if same["id"] not in changes["updated"] and same["id"] not in changes["added"]:
                    changes["updated"].append(same["id"])
                if op.get("id"):
                    given[op["id"]] = same["id"]
                continue
            rule: dict[str, Any] = {"id": f"D{book['next']}", "category": category_of(op.get("category")), "rule": op["rule"]}
            book["next"] += 1
            if op.get("fix"):
                rule["fix"] = op["fix"]
            if op.get("example"):
                rule["example"] = op["example"]
            rule.update({"sources": sources, "created": now, "updated": now})
            if op.get("id") and not any(r["id"] == op["id"] for r in book["rules"]):
                given[op["id"]] = rule["id"]
            book["rules"].append(rule)
            changes["added"].append(rule["id"])
        elif op["op"] == "update":
            target = next((r for r in book["rules"] if r["id"] == id_of(op.get("id"))), None)
            if not target:
                continue
            if op.get("rule"):
                target["rule"] = op["rule"]
            if op.get("category"):
                target["category"] = category_of(op["category"])
            if op.get("fix"):
                target["fix"] = op["fix"]
            if op.get("example"):
                target["example"] = op["example"]
            target["sources"] = _merge(target["sources"], sources)
            target["updated"] = now
            if target["id"] not in changes["updated"] and target["id"] not in changes["added"]:
                changes["updated"].append(target["id"])
        else:
            at = next((i for i, r in enumerate(book["rules"]) if r["id"] == id_of(op.get("id"))), -1)
            if at < 0:
                continue
            gone = book["rules"].pop(at)
            book["removed"].append({**gone, "sources": _merge(gone["sources"], sources), "removed": now,
                                    "reason": op.get("reason") or "群里撤回或被新规则取代", "by": "chat"})
            changes["removed"].append(gone["id"])
    book["removed"] = book["removed"][-200:]
    if changes["added"] or changes["updated"] or changes["removed"]:
        book["updated"] = now
    return changes


# ---- reading a group

@dataclass
class RulesContext:
    lark: Lark
    model: Model
    #: The skill's folder (fixed specs) and the shared folder.
    root: str
    shared: str
    log: Callable[[str], None]
    now: Callable[[], int] | None = None
    #: How many model calls one sync of one group makes at most; the rest waits for the next sync.
    max_chunks: int | None = None
    #: No model call is started after this moment (the job is ended after 30 minutes; a user waits for a watch): the rest waits.
    deadline: int | None = None

    def current(self) -> int:
        return self.now() if self.now else now_ms()


def _time(m: FeishuMessage) -> int:
    value = js_number(m.get("create_time"))
    return int(value) if value == value else 0


def _from_user(m: FeishuMessage) -> str | None:
    sender = m.get("sender") or {}
    return sender["id"] if sender.get("sender_type") == "user" and sender.get("id") else None


def _with_parents(entries: list[Entry], parents: dict[str, FeishuMessage], ctx: EntryContext) -> list[Entry]:
    """The entries with, before the first reply to each, the earlier message it answers (looked up because it was read before)."""
    out: list[Entry] = []
    shown: set[str] = set()
    for entry in entries:
        parent = parents.get(entry["reply"]) if entry.get("reply") else None
        if parent and parent["message_id"] not in shown:
            shown.add(parent["message_id"])
            earlier = entry_of(parent, ctx)
            if earlier:
                out.append({**earlier, "earlier": True})
        out.append(entry)
    return out


def _not_this_chunk(error: BaseException) -> bool:
    """A failure that is not this chunk's doing: the gateway, the network, or a bug."""
    return not isinstance(error, Blocked) or isinstance(error, (GatewayDown, GatewayRefused))


def _advance(group: Group, read: list[tuple[str, int]]) -> None:
    newest = max([group["cursor"], *(at for _, at in read)])
    if newest > group["cursor"]:
        group["cursorIds"] = []
    group["cursorIds"] = list(dict.fromkeys([*group["cursorIds"], *(i for i, at in read if at == newest)]))
    group["cursor"] = newest


def sync_group(group: Group, ctx: RulesContext, persist: Callable[[], None]) -> dict[str, Any]:
    """Reads what is new in a group (its chat, and the threads it follows) and applies it to its project's dynamic spec, chunk by
    chunk; after each chunk the spec and the group's progress are saved (`persist`), so a failure loses nothing that was done. Never
    raises: a failure is the result's `error`."""
    now = ctx.current()
    result: dict[str, Any] = {"chat": group["chat"], "project": group["project"], "read": 0, "considered": 0, "added": [], "updated": [],
                              "removed": [], "pending": False}
    names: dict[str, str] = read_json(names_file(ctx.shared), {})
    raw: list[FeishuMessage] = []
    # The messages read that the group's progress has moved past (what a stopped or failed reading did not finish is read again).
    covered = 0
    threads: dict[str, int] = group.setdefault("threads", {})
    # Threads that could not be read this time: they stay where they were and are asked again next time.
    unread: set[str] = set()
    try:
        project = project_of(group["project"])
        start = max(group["since"], group["cursor"])

        def already(m: FeishuMessage) -> bool:
            return _time(m) == group["cursor"] and m["message_id"] in group["cursorIds"]

        for message in ctx.lark.history(group["chat"], start // 1000, -(-now // 1000)):
            if _time(message) >= start and not already(message):
                raw.append(message)
        # Thread replies are listed only in their thread: every thread followed is asked for what came after the newest reply read in it.
        for message in raw:
            if message.get("thread_id") and message["thread_id"] not in threads:
                threads[message["thread_id"]] = _time(message)
        seen = {m["message_id"] for m in raw}
        for thread, newest in list(threads.items()):
            try:
                for message in ctx.lark.thread(thread):
                    if newest < _time(message) <= now and message["message_id"] not in seen:
                        seen.add(message["message_id"])
                        raw.append(message)
            except Exception as error:  # noqa: BLE001 - read again next time
                unread.add(thread)
                ctx.log(f"话题读取失败，下次再读：{explain_lark(error)}")
        raw.sort(key=_time)
        if not raw:
            group["pending"] = False
            return result

        # The last messages the previous readings read, for context: they are shown, not read again.
        context: list[FeishuMessage] = []
        if group["cursor"] > 0:
            def before(m: FeishuMessage) -> bool:
                return _time(m) >= group["since"] and m["message_id"] not in seen and (
                    _time(m) < group["cursor"] or m["message_id"] in group["cursorIds"])
            try:
                context += [m for m in ctx.lark.recent(group["chat"], group["cursor"] // 1000, CONTEXT + 10) if before(m)]
            except Exception as error:  # noqa: BLE001 - read without it
                ctx.log(f"读不到之前的消息，这次不带上：{explain_lark(error)}")
        shown_before = {m["message_id"] for m in context}

        carried: dict[str, list[FeishuMessage]] = {}
        for message in (m for m in raw if m.get("msg_type") == "merge_forward"):
            try:
                carried[message["message_id"]] = ctx.lark.message(message["message_id"])
            except Exception:  # noqa: BLE001 - shown as a forward without its content
                pass
        # A reply to a message from an earlier reading needs that message to make sense (「以后这种都不行」).
        parents: dict[str, FeishuMessage] = {}
        wanted = list(dict.fromkeys(p for p in (m.get("parent_id") for m in raw) if p and p not in seen and p not in shown_before))
        for parent_id in wanted[:PARENTS_MAX]:
            try:
                parent = next((m for m in ctx.lark.message(parent_id) if m.get("message_id") == parent_id), None)
                if parent:
                    parents[parent_id] = parent
            except Exception:  # noqa: BLE001 - shown as a reply to an earlier message
                pass
        everyone = [*raw, *(m for ms in carried.values() for m in ms), *parents.values(), *context]
        for person in dict.fromkeys(p for p in map(_from_user, everyone) if p):
            # A name that cannot be looked up now (no permission yet, say) is asked again next time.
            if not names.get(person):
                name = ctx.lark.user_name(person)
                if name:
                    names[person] = name
        entry_context = EntryContext(app_id=ctx.lark.app_id, name=lambda i: names.get(i) or f"成员{i[-4:]}", forwarded=carried)
        bot = {m["message_id"] for m in [*raw, *parents.values(), *context]
               if (m.get("sender") or {}).get("sender_type") == "app" and (m.get("sender") or {}).get("id") == ctx.lark.app_id}
        # Context goes short and without its pictures, leaving the chunk to what is new (a picture that matters is usually replied to,
        # and then comes along with the reply).
        shown: list[Entry] = []
        for e in entries_of(context, entry_context)[-CONTEXT:]:
            text = f"{e['text'][:CONTEXT_CHARS]}…" if len(e["text"]) > CONTEXT_CHARS else e["text"]
            shown.append({**e, "earlier": True, "pictures": [], "text": f"{text} [图片]".strip() if e["pictures"] else text})
        entries = [*shown, *_with_parents(entries_of(raw, entry_context), parents, entry_context)]
        result["considered"] = sum(1 for e in entries if not e.get("earlier"))

        parts_all = chunks(entries)
        limit = ctx.max_chunks if ctx.max_chunks is not None else 40
        fixed = fixed_spec(ctx.root, project).text
        book = load_book(ctx.shared, project.id)
        earlier: dict[str, str] = {}

        def ask(part: list[Entry], pictures: bool) -> Any:
            fresh = sum(1 for e in part if not e.get("earlier"))
            head = f"New messages from the review group ({fresh})" + (
                f", with {len(part) - fresh} from earlier readings (marked 之前的消息)" if fresh < len(part) else "")
            # A picture that does not come along (too large, left out, or one Feishu does not hand over: those of a forwarded chat) is
            # marked as missing in the transcript, not labelled for an image that never follows.
            media: list[Part] = []
            attached: set[str] = set()
            size = 0
            for i, entry in enumerate(part):
                for j, key in enumerate(entry["pictures"] if pictures else []):
                    picture = ctx.lark.picture(entry["id"], key)
                    if not picture or picture.mime not in PICTURE_TYPES or len(picture.data) > PICTURE_MAX or size + len(picture.data) > PICTURES_BYTES:
                        continue
                    size += len(picture.data)
                    attached.add(f"{i + 1}-{j + 1}")
                    media += [{"text": f"[图片 #{i + 1}-{j + 1}]"},
                              {"inlineData": {"mimeType": picture.mime, "data": base64.b64encode(picture.data).decode("ascii")}}]
            parts: list[Part] = [{"text": f"{head}:\n\n{render_chunk(part, earlier, bot, attached)}"}, *media]
            return ctx.model.json(JsonRequest(system=distill_system(project, fixed, prompt_rules(book, True), day(now)), parts=parts,
                                              max_output_tokens=16_384))

        def done(part: list[Entry]) -> None:
            nonlocal covered
            fresh = [e for e in part if not e.get("earlier")]
            _advance(group, [(e["id"], e["at"]) for e in fresh])
            for entry in fresh:
                if entry.get("thread") and entry["thread"] not in unread:
                    threads[entry["thread"]] = max(threads.get(entry["thread"], 0), entry["at"])
            covered = sum(1 for m in raw if _time(m) <= group["cursor"])
            for entry in part:
                earlier[entry["id"]] = entry["text"]
            persist()

        calls = 0
        for k, part in enumerate(parts_all):
            # Nothing new in it, only what earlier readings read (the new messages were all left out: the bot's own replies, stickers): no call.
            if not any(not e.get("earlier") for e in part):
                done(part)
                continue
            if calls >= limit or (ctx.deadline is not None and now_ms() > ctx.deadline):
                result["pending"] = True
                break
            calls += 1
            if len(parts_all) > 1:
                ctx.log(f"整理 {project.id} 动态规范：第 {k + 1}/{len(parts_all)} 段（{len(part)} 条消息）")
            reply = None
            try:
                reply = ask(part, True)
            except Exception as error:  # noqa: BLE001 - classified below
                # A gateway that is down, busy or silent, or that refuses every request as it is set up (the key, the model), is tried
                # again next time, as it is. Anything else may be this chunk itself: once more without its pictures, and a chunk that
                # fails reading after reading is skipped, so that the group does not stop for good.
                if _not_this_chunk(error):
                    raise
                last: Blocked = error  # type: ignore[assignment]
                if any(e["pictures"] for e in part):
                    try:
                        reply = ask(part, False)
                    except Exception as again:  # noqa: BLE001
                        if _not_this_chunk(again):
                            raise
                        last = again  # type: ignore[assignment]
                if reply is None:
                    stuck = group.get("stuck")
                    tries = stuck["tries"] + 1 if stuck and stuck.get("cursor") == group["cursor"] else 1
                    if tries < STUCK_TRIES:
                        group["stuck"] = {"cursor": group["cursor"], "tries": tries, "reason": last.message}
                        raise last from None
                    fresh = [e for e in part if not e.get("earlier")]
                    group["skipped"] = [*(group.get("skipped") or []),
                                        {"from": stamp(fresh[0]["at"]), "to": stamp(fresh[-1]["at"]), "reason": last.message}][-20:]
                    group.pop("stuck", None)
                    ctx.log(f"有一段消息整理了 {STUCK_TRIES} 次都失败，已跳过：{last.message}")
                    done(part)
                    continue
            group.pop("stuck", None)
            refs: dict[int, Source] = {}
            for i, e in enumerate(part):
                words = _SPACES.sub(" ", e["text"]).strip()[:60] or ("[图片]" if e["pictures"] else "")
                refs[i + 1] = {"chat": group["chat"], "message": e["id"], "at": e["at"], "by": e["by"], **({"text": words} if words else {})}
            earlier_ones = {i + 1 for i, e in enumerate(part) if e.get("earlier")}
            changes = apply(book, operations_of(reply.data), refs, now, earlier_ones)
            result["added"] += changes["added"]
            result["updated"] += [i for i in changes["updated"] if i not in result["updated"]]
            result["removed"] += changes["removed"]
            save_book(ctx.shared, book)
            done(part)
        if not result["pending"]:
            # Everything read, including what was left out (the bot's own messages, stickers): the next sync starts after the newest.
            _advance(group, [(m["message_id"], _time(m)) for m in raw])
            for message in raw:
                if message.get("thread_id") and message["thread_id"] not in unread:
                    threads[message["thread_id"]] = max(threads.get(message["thread_id"], 0), _time(message))
            covered = len(raw)
        group["pending"] = result["pending"]
        return result
    except Exception as error:  # noqa: BLE001 - never raises: the failure is the result's error
        result["error"] = error.message if isinstance(error, Blocked) else explain_lark(error)
        return result
    finally:
        # A thread is let go after THREAD_DAYS without a reply, which only a whole reading can tell: after one that stopped early or
        # failed, and for a thread that could not be read, newer replies may not have been read yet.
        if not result["pending"] and not result.get("error"):
            for thread, newest in list(threads.items()):
                if thread not in unread and newest < now - THREAD_DAYS * DAY:
                    del threads[thread]
        result["read"] = covered
        group["read"] += covered
        try:
            write_atomic(names_file(ctx.shared), f"{dump(names, pretty=False)}\n")
        except OSError:
            pass  # names are looked up again next time


# ---- commands

def _not_read(group: Group) -> dict[str, Any]:
    return {"chat": group["chat"], "project": group["project"], "read": 0, "considered": 0, "added": [], "updated": [], "removed": [],
            "pending": True}


def _summary(r: dict[str, Any]) -> dict[str, Any]:
    out = {"project": r["project"], "read": r["read"], "considered": r["considered"], "added": len(r["added"]), "updated": len(r["updated"]),
           "removed": len(r["removed"]), "pending": r["pending"]}
    if r.get("error"):
        out["error"] = r["error"]
    return out


#: How long a user's watch reads before answering (the rest is read by the next sync), and how long the sync every three days reads: the job is
#: ended at 30 minutes too, so a call started just before the time is up (one can take 10) may be cut off: what was read before it is kept,
#: and that part is read again at the next sync.
WATCH_MINUTES = 30
SYNC_MINUTES = 30


def watch(project: Project, since: int | None, turn: Turn, ctx: RulesContext) -> dict[str, Any]:
    """Starts listening to the turn's group for `project` (or reads it again from an earlier date), and reads its history right away."""
    need(turn.chat, "不知道这条消息来自哪个群：请在审核群里 @机器人 再说一次")
    need(turn.chat_type != "p2p", "要在审核群里开始：把机器人拉进审核群，在群里 @机器人 说「监听本群更新审片规范」")
    need(turn.chat_type == "group", "看不出这里是不是群聊：请在审核群里 @机器人 再说一次")
    now = ctx.current()
    start = since if since is not None else now - HISTORY_DAYS * DAY
    need(start < now, "开始日期不能晚于今天")

    def locked() -> dict[str, Any]:
        groups = load_groups(ctx.shared)
        group = next((g for g in groups["groups"] if g["chat"] == turn.chat), None)
        need(not group or group["project"] == project.id,
             f"本群已经在给 {group['project'] if group else None} 整理动态规范；要改成 {project.id}，先说「停止监听本群」，再重新开始")
        before = group is not None
        if group is None:
            group = {"chat": turn.chat, "project": project.id, "since": start, "cursor": 0, "cursorIds": []}
            if turn.user:
                group["watchedBy"] = turn.user
            group.update({"watchedAt": now, "read": 0})
            groups["groups"].append(group)
        elif since is not None and start < group["since"]:
            # An earlier start: read again from there (the model sees the rules it made already, and the same rule is not added twice).
            group.update({"since": start, "cursor": 0, "cursorIds": []})
        save_groups(ctx.shared, groups)
        deadline = ctx.deadline if ctx.deadline is not None else now_ms() + WATCH_MINUTES * 60_000
        synced = sync_group(group, replace(ctx, deadline=deadline), lambda: save_groups(ctx.shared, groups))
        group["lastSync"] = now
        if synced.get("error"):
            group["lastError"] = synced["error"]
        else:
            group.pop("lastError", None)
        save_groups(ctx.shared, groups)
        return {"status": "watching", **_summary(synced), "project": project.id, "already": before, "since": day(group["since"]),
                "rules": len(load_book(ctx.shared, project.id)["rules"])}

    return with_lock(ctx.shared, locked)


def unwatch(turn: Turn, shared: str) -> dict[str, Any]:
    need(turn.chat, "不知道这条消息来自哪个群：请在要停止的群里 @机器人 再说一次")

    def locked() -> dict[str, Any]:
        groups = load_groups(shared)
        at = next((i for i, g in enumerate(groups["groups"]) if g["chat"] == turn.chat), -1)
        need(at >= 0, "本群没有在监听")
        group = groups["groups"].pop(at)
        save_groups(shared, groups)
        return {"status": "stopped", "project": group["project"], "rules": len(load_book(shared, group["project"])["rules"])}

    return with_lock(shared, locked)


def _rule_counts(shared: str) -> dict[str, int]:
    return {p: len(load_book(shared, p)["rules"]) for p in PROJECTS}


def sync_all(ctx: RulesContext, chat: str | None = None, wait_ms: int = 0) -> dict[str, Any]:
    """Reads what is new in every group listened to (or in one), the job every three days. `wait_ms` 0: give up at once when another holds the lock."""
    def locked() -> dict[str, Any]:
        groups = load_groups(ctx.shared)
        # The group read longest ago goes first: when time runs out, the others are first the next time.
        targets = sorted((g for g in groups["groups"] if not chat or g["chat"] == chat), key=lambda g: g.get("lastSync") or 0)
        need(not chat or targets, "本群没有在监听：先 @机器人 说「监听本群更新审片规范」")
        deadline = ctx.deadline if ctx.deadline is not None else now_ms() + SYNC_MINUTES * 60_000
        results: list[dict[str, Any]] = []
        for group in targets:
            # A group the time ran out on before it was read (at all, or before anything of it was) keeps its place, first in line the
            # next time, instead of waiting behind the group that used the time up.
            if now_ms() > deadline:
                synced = _not_read(group)
            else:
                synced = sync_group(group, replace(ctx, deadline=deadline), lambda: save_groups(ctx.shared, groups))
            if synced["read"] or not synced["pending"]:
                group["lastSync"] = ctx.current()
            if synced.get("error"):
                group["lastError"] = synced["error"]
            else:
                group.pop("lastError", None)
            save_groups(ctx.shared, groups)
            results.append({"chat": group["chat"], **({"here": True} if group["chat"] == chat else {}), **_summary(synced)})
        # Every group failing is a failure (the host logs the job as failed); some failing is in their `error`.
        failed = bool(results) and all(r.get("error") for r in results)
        return {"status": "failed" if failed else "synced", "groups": results, "rules": _rule_counts(ctx.shared)}

    return with_lock(ctx.shared, locked, wait_ms)


def status(shared: str, turn: Turn) -> dict[str, Any]:
    groups = load_groups(shared)["groups"]
    here = next((g["project"] for g in groups if g["chat"] == turn.chat), None) if turn.chat else None
    listed = []
    for g in groups:
        item: dict[str, Any] = {"chat": g["chat"]}
        if g["chat"] == turn.chat:
            item["here"] = True
        item.update({"project": g["project"], "since": day(g["since"]), "watched": stamp(g["watchedAt"]),
                     "last_sync": stamp(g["lastSync"]) if g.get("lastSync") else None, "read": g["read"], "pending": bool(g.get("pending"))})
        if g.get("lastError"):
            item["error"] = g["lastError"]
        if g.get("skipped"):
            item["skipped"] = g["skipped"]
        listed.append(item)
    return {"status": "ok", "here": here, "groups": listed, "rules": _rule_counts(shared)}


def _free_name(file: str) -> str:
    """A free name next to `file`: report.md, report (2).md, …"""
    stem, ext = os.path.splitext(file)
    n = 1
    while True:
        candidate = file if n == 1 else f"{stem} ({n}){ext}"
        if not os.path.exists(candidate):
            return candidate
        n += 1


def show(shared: str, project: Project, output: str | None = None) -> dict[str, Any]:
    """The project's dynamic spec as Markdown: a file for the user (`output`, next to which a free name is taken when it exists), or,
    without one, in the answer itself, for reading it without leaving a file in the workspace (the host sends new files there to the
    user)."""
    book = load_book(shared, project.id)
    facts = {"project": project.id, "rules": len(book["rules"]), "updated": stamp(book["updated"]) if book.get("updated") else None}
    if not output:
        return {"status": "ok", **facts, "markdown": render_book(book)}
    file = _free_name(os.path.abspath(output))
    os.makedirs(os.path.dirname(file), exist_ok=True)
    with open(file, "x", encoding="utf-8", newline="") as handle:
        handle.write(render_book(book))
    return {"status": "saved", **facts, "file": file}


def remove_rules(project: Project, ids: list[str], turn: Turn, shared: str, now: Callable[[], int] | None = None) -> dict[str, Any]:
    """Takes rules out of the dynamic spec at a user's request (they stay on record, with who and when)."""
    wanted = list(dict.fromkeys(i.strip().upper() for i in ids if i.strip()))
    need(wanted, "要删哪几条？给出规范编号（如 D3）")

    def locked() -> dict[str, Any]:
        book = load_book(shared, project.id)
        missing = [i for i in wanted if not any(r["id"] == i for r in book["rules"])]
        need(not missing, f"{project.id} 的动态规范里没有 {'、'.join(missing)}")
        at = now() if now else now_ms()
        removed = []
        for rule_id in wanted:
            gone = book["rules"].pop(next(i for i, r in enumerate(book["rules"]) if r["id"] == rule_id))
            book["removed"].append({**gone, "removed": at, "reason": f"用户删除{'（' + turn.user + '）' if turn.user else ''}", "by": "user"})
            removed.append({"id": gone["id"], "rule": gone["rule"]})
        book["updated"] = at
        save_book(shared, book)
        return {"status": "removed", "project": project.id, "removed": removed, "rules": len(book["rules"])}

    return with_lock(shared, locked)
