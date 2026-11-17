"""What the skill runs with: everything comes from the environment, or has a default here. Every variable is optional.

  AUDIT_AUTO_GEMINI_KEY      the AI gateway key (reviews and the dynamic spec need it)
  FEISHU_APP_ID / _SECRET    the bot's Feishu app, which reads the review groups (the bot is a member of each)
  AGENT_USER_DIR             the user's data root (absolute); this skill keeps per-user data in audit-auto/ there: tmp/ (scratch)
                             and reviews/ (kept review results). Default ~/.agent-skills/user
  AGENT_SHARED_DIR           data shared by all users (absolute); this skill keeps review groups and dynamic specs in audit-auto/
                             there. Default ~/.agent-skills/shared
  AGENT_CHAT_ID / _TYPE      the chat of this turn and whether it is 'p2p' or 'group', when the host knows it
  AGENT_USER_OPEN_ID         who asked
  AUDIT_AUTO_RETENTION_DAYS  how long `prune` keeps scratch and review results (default 14)

Overrides, for the administrator and tests: AUDIT_AUTO_GEMINI_BASE_URL, AUDIT_AUTO_GEMINI_API_VERSION, AUDIT_AUTO_GEMINI_MODEL,
AUDIT_AUTO_GEMINI_THINKING, AUDIT_AUTO_FEISHU_BASE, AUDIT_AUTO_SKILL_ROOT, FFMPEG.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .errors import Blocked, need

SKILL = "audit-auto"
Env = Mapping[str, str]


def text(env: Env, name: str) -> str | None:
    value = env.get(name)
    value = value.strip() if isinstance(value, str) else ""
    return value or None


def skill_root(env: Env | None = None) -> str:
    """Where the skill is (SKILL.md, references/ with the fixed specs): two folders up from this file (scripts/audit_sdk/)."""
    env = os.environ if env is None else env
    given = text(env, "AUDIT_AUTO_SKILL_ROOT")
    if given:
        return os.path.abspath(given)
    root = Path(__file__).resolve().parent.parent.parent
    if (root / "SKILL.md").is_file():
        return str(root)
    raise Blocked("The skill's folder (SKILL.md) is missing: the skill is incomplete, ask the administrator to reinstall it")


def skill_version(root: str) -> str:
    """The `version` of SKILL.md's metadata, or ''."""
    try:
        head = Path(root, "SKILL.md").read_text(encoding="utf-8-sig").split("\n---", 1)[0]
    except OSError:
        return ""
    found = re.search(r"^\s+version:\s*[\"']?([^\"'\s]+)", head, re.MULTILINE)
    return found.group(1) if found else ""


# ---- the AI gateway

@dataclass
class Gateway:
    base_url: str
    api_version: str
    model: str
    thinking_level: str
    key: str


THINKING = {"MINIMAL", "LOW", "MEDIUM", "HIGH"}


def gateway(env: Env | None = None) -> Gateway:
    """Gemini through the internal AI gateway: https://lumos.diandian.info/winky/gemini/v1/models/<model>:generateContent, with the
    gateway key (the gateway takes the same key for every model), gemini-3.8-flash thinking at medium."""
    env = os.environ if env is None else env
    thinking = (text(env, "AUDIT_AUTO_GEMINI_THINKING") or "medium").upper()
    need(thinking in THINKING, "AUDIT_AUTO_GEMINI_THINKING must be minimal, low, medium or high")
    return Gateway(
        base_url=(text(env, "AUDIT_AUTO_GEMINI_BASE_URL") or "https://lumos.diandian.info/winky/gemini").rstrip("/"),
        api_version=text(env, "AUDIT_AUTO_GEMINI_API_VERSION") or "v1",
        model=text(env, "AUDIT_AUTO_GEMINI_MODEL") or "gemini-3.8-flash",
        thinking_level=thinking,
        key=gateway_key(env),
    )


def gateway_key(env: Env | None = None) -> str:
    """The gateway key, from the environment only. The model never reads it, and it appears in no output."""
    env = os.environ if env is None else env
    given = text(env, "AUDIT_AUTO_GEMINI_KEY")
    need(given, "AUDIT_AUTO_GEMINI_KEY is not set: ask the administrator to set it in the environment")
    return given  # type: ignore[return-value]


# ---- Feishu, chats and folders

@dataclass
class App:
    id: str
    secret: str
    base: str


def feishu_app(env: Env | None = None) -> App:
    """The bot's Feishu app, which reads the review groups (the bot is a member of each)."""
    env = os.environ if env is None else env
    app_id = text(env, "FEISHU_APP_ID")
    secret = text(env, "FEISHU_APP_SECRET")
    need(app_id and secret, "FEISHU_APP_ID / FEISHU_APP_SECRET are not set: ask the administrator to set the bot's Feishu app in the environment")
    return App(id=app_id, secret=secret, base=(text(env, "AUDIT_AUTO_FEISHU_BASE") or "https://open.feishu.cn/open-apis").rstrip("/"))  # type: ignore[arg-type]


@dataclass
class Turn:
    """The chat this turn's message came from and who sent it; a periodic job has none."""
    chat: str | None = None
    chat_type: str | None = None
    user: str | None = None


def turn_of(env: Env | None = None) -> Turn:
    env = os.environ if env is None else env
    return Turn(chat=text(env, "AGENT_CHAT_ID"), chat_type=text(env, "AGENT_CHAT_TYPE"), user=text(env, "AGENT_USER_OPEN_ID"))


def _data_dir(env: Env, name: str, default: str, home: str | None) -> str:
    given = text(env, name)
    root = os.path.abspath(given) if given else os.path.join(home or os.path.expanduser("~"), ".agent-skills", default)
    return os.path.join(root, SKILL)


def shared_dir(env: Env | None = None, home: str | None = None) -> str:
    """What the skill keeps for every user alike (the review groups it listens to, their dynamic specs): never pruned."""
    return _data_dir(os.environ if env is None else env, "AGENT_SHARED_DIR", "shared", home)


def user_dir(env: Env | None = None, home: str | None = None) -> str:
    """This user's data: tmp/ (scratch) and reviews/ (kept results), both expendable (see prune.py)."""
    return _data_dir(os.environ if env is None else env, "AGENT_USER_DIR", "user", home)


def scratch_root(env: Env | None = None, home: str | None = None) -> str:
    """Scratch space for unpacked zips and compressed videos. Never the working directory: a host may send the user what appears there."""
    return os.path.join(user_dir(env, home), "tmp")


def reviews_dir(env: Env | None = None, home: str | None = None) -> str:
    return os.path.join(user_dir(env, home), "reviews")


def retention_days(env: Env | None = None) -> int:
    env = os.environ if env is None else env
    given = text(env, "AUDIT_AUTO_RETENTION_DAYS") or "14"
    need(re.fullmatch(r"\d+", given) and int(given) >= 1, f"AUDIT_AUTO_RETENTION_DAYS must be a whole number of days, at least 1 (it is {given!r})")
    return int(given)
