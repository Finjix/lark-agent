"""feishu.py: the command line of the agent's Feishu module (the user's Feishu login, documents as Markdown, and the bridge skills call).

Every command prints one line of JSON; see feishu_sdk/cli.py, README.md and bridge.md.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from feishu_sdk.cli import run  # noqa: E402

if __name__ == "__main__":
    sys.exit(run())
