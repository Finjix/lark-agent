"""Tests of requirement-auto's gof command line. The package beside the CLI (scripts/gof) is put on the import path here."""

import os
import sys

_SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)
