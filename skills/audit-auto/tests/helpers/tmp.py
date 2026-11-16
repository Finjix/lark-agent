"""Temporary directories removed when the test process ends, whether or not the test cleaned up itself."""

from __future__ import annotations

import atexit
import shutil
import tempfile

_made: list[str] = []


@atexit.register
def _cleanup() -> None:
    for directory in _made:
        shutil.rmtree(directory, ignore_errors=True)  # still in use: left to the OS temp cleanup


def temp_dir(prefix: str) -> str:
    directory = tempfile.mkdtemp(prefix=prefix)
    _made.append(directory)
    return directory
