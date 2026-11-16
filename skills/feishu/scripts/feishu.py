"""feishu.py: the feishu skill's command line (login, documents as Markdown, and the bridge other skills call).

Run it with the Python of the host (other skills: sys.executable). Every command prints one line of JSON; see feishu_sdk/cli.py and the
skill's references/ for the commands.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from feishu_sdk.cli import run  # noqa: E402

if __name__ == "__main__":
    sys.exit(run())
