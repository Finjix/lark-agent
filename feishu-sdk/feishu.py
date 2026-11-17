"""feishu.py: the command line of lark-agent's Feishu module (login, documents as Markdown, and the bridge skills call).

The agent's Feishu tools and the skills run it with the host's Python (skills: sys.executable). Every command prints one line of JSON; see
feishu_sdk/cli.py and README.md for the commands.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from feishu_sdk.cli import run  # noqa: E402

if __name__ == "__main__":
    sys.exit(run())
