"""The tree stays lint-clean, as ruff.toml defines it.

There is no CI, so this is the gate: a new unused import, unused variable or undefined name
fails `uv run pytest` like any other regression. Run through this interpreter's own ruff --
it is in requirements-dev.txt -- so the check is the venv's, not whatever is on PATH.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_ruff_finds_nothing():
    result = subprocess.run(
        [sys.executable, "-m", "ruff", "check", "--output-format", "concise", "."],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
