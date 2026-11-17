"""Tests of the audit-auto skill. Run from the folder that holds skills/ (the repository root):

    python -m unittest discover -s skills/audit-auto/tests -t skills/audit-auto
"""

import os
import sys

SKILL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(SKILL, "scripts"))

# The fake gateway and the fake Feishu listen on 127.0.0.1: never through a proxy.
os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost"
