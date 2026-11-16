"""What the skill runs with (port of config.ts).

Everything comes from the host (lark-agent) through the environment, or has a default here:

  AUDIT_AUTO_GEMINI_KEY        the AI gateway key (lark-agent's .env; it reaches the script through the environment)
  LARK_AGENT_SHARED_DIR        the folder skills share; this skill keeps its review groups and dynamic specs in audit-auto/ there
  LARK_AGENT_USER_DIR          the user's folder (scratch space goes to audit-auto/tmp in it, kept reviews to audit-auto/reviews)
  LARK_AGENT_CHAT_ID / _TYPE   the chat of this turn and whether it is a group (listening to a review group needs one)
  LARK_AGENT_USER_OPEN_ID      who asked
  FEISHU_APP_ID / _SECRET      the bot's Feishu app, to read the review groups' messages as the bot

Overrides, for the administrator (lark-agent's .env) and tests: AUDIT_AUTO_GEMINI_BASE_URL, AUDIT_AUTO_GEMINI_API_VERSION,
AUDIT_AUTO_GEMINI_MODEL, AUDIT_AUTO_GEMINI_THINKING, AUDIT_AUTO_FEISHU_BASE, AUDIT_AUTO_SKILL_ROOT, FFMPEG.

The gateway key comes from the environment only (AUDIT_AUTO_GEMINI_KEY in lark-agent's .env).
"""

from __future__ import annotations

import os
import re
import tempfile
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
    raise Blocked("找不到技能目录（SKILL.md），技能文件不完整，请联系管理员")


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
    need(thinking in THINKING, "AUDIT_AUTO_GEMINI_THINKING 只能是 minimal、low、medium 或 high")
    return Gateway(
        base_url=(text(env, "AUDIT_AUTO_GEMINI_BASE_URL") or "https://lumos.diandian.info/winky/gemini").rstrip("/"),
        api_version=text(env, "AUDIT_AUTO_GEMINI_API_VERSION") or "v1",
        model=text(env, "AUDIT_AUTO_GEMINI_MODEL") or "gemini-3.8-flash",
        thinking_level=thinking,
        key=gateway_key(env),
    )


def gateway_key(env: Env | None = None) -> str:
    """The gateway key: AUDIT_AUTO_GEMINI_KEY, which the administrator sets in lark-agent's .env. The model never reads it, and it
    appears in no output."""
    env = os.environ if env is None else env
    given = text(env, "AUDIT_AUTO_GEMINI_KEY")
    need(given, "找不到 AI 网关的 key：lark-agent 的 .env 里没有设置 AUDIT_AUTO_GEMINI_KEY，请联系管理员")
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
    need(app_id and secret, "没有飞书应用的凭证（FEISHU_APP_ID / FEISHU_APP_SECRET，由 lark-agent 提供），请联系管理员")
    return App(id=app_id, secret=secret, base=(text(env, "AUDIT_AUTO_FEISHU_BASE") or "https://open.feishu.cn/open-apis").rstrip("/"))  # type: ignore[arg-type]


@dataclass
class Turn:
    """The chat this turn's message came from and who sent it; a periodic job has none."""
    chat: str | None = None
    chat_type: str | None = None
    user: str | None = None


def turn_of(env: Env | None = None) -> Turn:
    env = os.environ if env is None else env
    return Turn(chat=text(env, "LARK_AGENT_CHAT_ID"), chat_type=text(env, "LARK_AGENT_CHAT_TYPE"), user=text(env, "LARK_AGENT_USER_OPEN_ID"))


def shared_dir(env: Env | None = None, home: str | None = None) -> str:
    """What the skill keeps for every user alike (the review groups it listens to, their dynamic specs): never cleaned up, never sent."""
    env = os.environ if env is None else env
    given = text(env, "LARK_AGENT_SHARED_DIR")
    if given:
        return os.path.join(os.path.abspath(given), SKILL)
    return os.path.join(home or os.path.expanduser("~"), ".audit-auto", "shared")


def scratch_root(env: Env | None = None) -> str:
    """Scratch space for unpacked zips and compressed videos. Never the workspace: the host sends the user what appears there."""
    env = os.environ if env is None else env
    user = text(env, "LARK_AGENT_USER_DIR")
    return os.path.join(os.path.abspath(user), SKILL, "tmp") if user else os.path.join(tempfile.gettempdir(), SKILL)
