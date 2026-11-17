"""Tests of the feishu skill: `python -m unittest discover -s skills/feishu/tests -t skills/feishu`."""

import os
import sys

ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
ENTRY = os.path.join(ROOT, "feishu.py")
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
