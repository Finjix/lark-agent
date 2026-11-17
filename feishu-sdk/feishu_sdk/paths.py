"""Where one user's Feishu data lives.

The login, a device authorization waiting to be confirmed, and the hidden browser that holds the user's Feishu web session (other skills
use it too, see `browser_profile`). The directory comes from the environment of the process (a host serving several users sets
AGENT_USER_DIR for each), never from a command argument or the model: whoever is served by this process can only reach this one directory.
Skills that call this one inherit the same environment, so they reach the same user's data. Without a host it is ~/.agent-skills/user/feishu.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Mapping

from .errors import BridgeError

# The module's folder in a user directory, and the user directory without a host.
SKILL_FOLDER = "feishu"
STANDALONE_USER_DIR = (".agent-skills", "user")
USER_DIR_ENV = "AGENT_USER_DIR"
OPEN_ID_ENV = "AGENT_USER_OPEN_ID"
#: `p2p` or `group`, when the host says what kind of chat the request came from: the login QR is never shown in a group.
CHAT_TYPE_ENV = "AGENT_CHAT_TYPE"
_OPEN_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")


@dataclass(frozen=True)
class DataPaths:
    #: host: AGENT_USER_DIR was given; standalone: ~/.agent-skills/user
    mode: Literal["host", "standalone"]
    root: str
    auth: str
    login: str
    lock: str
    #: The record of a sign-in QR picture that is waiting to be scanned (see qr.py).
    qr: str
    #: The profile of this user's hidden browser. Signing in to Feishu there (the QR of `authorize --qr`) gives it a Feishu web session,
    #: which is also what the company's single sign-on asks for: a skill whose site signs in through Feishu (requirement-auto's CLS)
    #: opens its pages on this profile, so one scan covers both.
    browser_profile: str


def _absolute(name: str, value: str) -> str:
    if "\0" in value or not _is_absolute(value):
        raise BridgeError("config", f"{name} must be an absolute path")
    resolved = os.path.abspath(value)
    if os.path.dirname(resolved) == resolved:
        raise BridgeError("config", f"{name} cannot be the root of a drive")
    return resolved


def _is_absolute(value: str) -> bool:
    # A drive-relative "\\x" counts as absolute on Windows, as it did for Node's path.isAbsolute.
    return os.path.isabs(value) or (os.name == "nt" and value[:1] in ("/", "\\"))


def _layout(mode: Literal["host", "standalone"], root: str) -> DataPaths:
    join = os.path.join
    return DataPaths(
        mode=mode, root=root, auth=join(root, "auth.json"), login=join(root, "login.json"), lock=join(root, "refresh.lock"),
        qr=join(root, "login-qr.json"), browser_profile=join(root, "browser-profile"),
    )


def host_user_dir(env: Mapping[str, str]) -> str | None:
    """The user directory the host gave, if it gave one (`user/<name>`, not its workspace)."""
    user_dir = (env.get(USER_DIR_ENV) or "").strip()
    return _absolute(USER_DIR_ENV, user_dir) if user_dir else None


def resolve_data_paths(env: Mapping[str, str], home: str | None = None) -> DataPaths:
    user_dir = host_user_dir(env)
    if user_dir:
        return _layout("host", os.path.join(user_dir, SKILL_FOLDER))
    # A host that names the user but not the directory has forgotten the directory: falling back to a folder shared by everybody
    # would let that user's login be reached by the next one.
    if (env.get(OPEN_ID_ENV) or "").strip():
        raise BridgeError("config", f"{OPEN_ID_ENV} is set but {USER_DIR_ENV} is not: refusing to fall back to a shared folder")
    return _layout("standalone", os.path.join(home if home is not None else str(Path.home()), *STANDALONE_USER_DIR, SKILL_FOLDER))


def expected_open_id(env: Mapping[str, str]) -> str | None:
    """The Feishu Open ID the host says this process serves, if it said so. Logging in as anybody else is refused."""
    value = (env.get(OPEN_ID_ENV) or "").strip()
    if not value:
        return None
    if not _OPEN_ID.fullmatch(value):
        raise BridgeError("config", f"{OPEN_ID_ENV} is not a valid Open ID")
    return value
