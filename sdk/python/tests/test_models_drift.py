"""The generated models equal a fresh generation from contracts/openapi.json (phase-7 plan decision 8): an API change that reaches the SDK shows up as a diff to review."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def test_models_py_is_what_the_generator_produces_from_the_committed_openapi() -> None:
    done = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "gen_sdk_models.py"), "--check"], capture_output=True, text=True, cwd=ROOT
    )  # noqa: S603
    assert done.returncode == 0, f"regenerate with `make sdk-models`:\n{done.stdout[:3000]}\n{done.stderr[-1500:]}"
