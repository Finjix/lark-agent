"""Tests of the feishu skill: `python -m unittest discover -s skills/feishu/tests -t skills/feishu` (ported from dev/feishu-sdk/test)."""

import os
import sys

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
ENTRY = os.path.join(SCRIPTS, "feishu.py")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)
