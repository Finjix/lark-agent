"""Where one user's requirement-auto data lives (paths.ts).

Batches, temporary files and the CLS records (the account the CLS login is pinned to, the QR picture waiting to be scanned). The Feishu
login and the hidden browser are lark-agent's (its Feishu module, see agent_feishu.py). The directory is decided by the host (lark-agent) for the
process it starts, never by a tool argument or by the model: whoever is served by this process can only reach this one directory.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping

from .util import Blocked


@dataclass(frozen=True)
class DataPaths:
    #: host: LARK_AGENT_USER_DIR was given; standalone: the home folder fallback
    mode: str
    root: str
    batches: str
    #: intermediate files of a task (catalog, snapshots, requests); like the batches they are removed once old, see retention.py
    tmp: str
    cls: str


USER_DIR_ENV = "LARK_AGENT_USER_DIR"
OPEN_ID_ENV = "LARK_AGENT_USER_OPEN_ID"


def _absolute(name: str, value: str) -> str:
    if "\0" in value or not os.path.isabs(value):
        raise Blocked(f"{name} 必须是绝对路径")
    resolved = os.path.abspath(value)
    if os.path.dirname(resolved) == resolved:
        raise Blocked(f"{name} 不能是磁盘根目录")
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
        raise Blocked(f"{OPEN_ID_ENV} 已设置但没有 {USER_DIR_ENV}：拒绝回落到共享目录")
    return _layout("standalone", os.path.join(home, ".requirement-auto", "default"))
