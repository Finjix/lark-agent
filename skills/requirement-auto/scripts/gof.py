"""requirement-auto's command line: `python gof.py <batch|feishu|cls|creative|login> <command> [options]` (gof.ts).

Every command prints one line of canonical JSON on stdout; a refusal is {"status":"blocked","reason":…} with exit code 2. First, old batches
and temporary files of this user go (see gof/retention.py). Failing to tidy up never stops the command.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gof.cli import main  # noqa: E402
from gof.feishu_skill import close_feishu  # noqa: E402
from gof.js import canonical  # noqa: E402


def run() -> int:
    try:
        from gof.paths import resolve_data_paths
        from gof.retention import prune_expired

        prune_expired(resolve_data_paths())
    except Exception:
        pass  # a bad data directory is reported by the command itself
    answer = main(sys.argv[1:])
    close_feishu()
    out = canonical(answer["result"]) + "\n"
    sys.stdout.buffer.write(out.encode("utf-8", "surrogatepass"))
    sys.stdout.flush()
    return int(answer["code"])


if __name__ == "__main__":
    sys.exit(run())
