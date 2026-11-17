"""Where one user's requirement-auto data lives.

Batches, temporary files and the CLS records (the account the CLS login is pinned to, the QR picture waiting to be scanned), under
<AGENT_USER_DIR>/requirement-auto/, or ~/.agent-skills/user/requirement-auto/ when the host names no user directory. The Feishu login and the
hidden browser belong to the agent's Feishu module (see agent_feishu.py). The directory is decided by the host for the process it starts, never by a
tool argument or by the model: whoever is served by this process can only reach this one directory.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping

from .util import Blocked


@dataclass(frozen=True)
class DataPaths:
    #: host: AGENT_USER_DIR was given; standalone: the default under the home folder
    mode: str
    root: str
    batches: str
    #: intermediate files of a task (catalog, snapshots, requests); like the batches they are removed once old, see retention.py
    tmp: str
    cls: str


USER_DIR_ENV = "AGENT_USER_DIR"
OPEN_ID_ENV = "AGENT_USER_OPEN_ID"
#: The user directory below the home folder when the host names none (the Feishu module uses the same one).
DEFAULT_USER_DIR = (".agent-skills", "user")


def _absolute(name: str, value: str) -> str:
    if "\0" in value or not os.path.isabs(value):
        raise Blocked(f"{name} must be an absolute path")
    resolved = os.path.abspath(value)
    if os.path.dirname(resolved) == resolved:
        raise Blocked(f"{name} cannot be the root of a drive")
    return resolved


def _layout(mode: str, root: str) -> DataPaths:
    return DataPaths(mode, root, os.path.join(root, "batches"), os.path.join(root, "tmp"), os.path.join(root, "cls"))


def resolve_data_paths(env: Mapping[str, str] | None = None, home: str | None = None) -> DataPaths:
    env = os.environ if env is None else env
    home = os.path.expanduser("~") if home is None else home
    user_dir = (env.get(USER_DIR_ENV) or "").strip()
    if user_dir:
        return _layout("host", os.path.join(_absolute(USER_DIR_ENV, user_dir), "requirement-auto"))
    # A host that names the user but not the directory has forgotten the directory: falling back to a folder shared by everybody
    # would let that user's batches and CLS login be reached by the next one.
    if (env.get(OPEN_ID_ENV) or "").strip():
        raise Blocked(f"{OPEN_ID_ENV} is set without {USER_DIR_ENV}: refusing to fall back to a shared directory")
    return _layout("standalone", os.path.join(home, *DEFAULT_USER_DIR, "requirement-auto"))
