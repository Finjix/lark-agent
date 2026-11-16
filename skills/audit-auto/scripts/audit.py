"""audit-auto's command line: `python <skill>/scripts/audit.py <command> …` (see audit_sdk/cli.py for the commands).

Progress goes to stderr; the result is one line of JSON on stdout; the exit code is 0, or 2 when refused or failed.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from audit_sdk.cli import main  # noqa: E402


def run() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass
    result, code = main(sys.argv[1:])
    sys.stdout.write(json.dumps(result, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()
    return code


if __name__ == "__main__":
    sys.exit(run())
