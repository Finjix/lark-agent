"""Tests of lark-agent's Feishu module: `python -m unittest discover -s feishu-sdk/tests -t feishu-sdk` (ported from dev/feishu-sdk/test)."""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENTRY = os.path.join(ROOT, "feishu.py")
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
