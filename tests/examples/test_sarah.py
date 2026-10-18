"""Runs the Sarah example end to end against the running stack (`make up seed`). Skipped when the stack is not up, unless KG_REQUIRE_STACK=1."""

import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]


def _stack_up() -> bool:
    try:
        return httpx.get("http://127.0.0.1:8000/health", timeout=2).status_code == 200
    except httpx.HTTPError:
        return False


@pytest.mark.skipif(not _stack_up() and os.environ.get("KG_REQUIRE_STACK") != "1", reason="the dev stack is not up")
def test_sarah_scenario_passes(tmp_path: Path) -> None:
    proc = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "examples.sarah.run", "--reports", str(tmp_path)],
        capture_output=True,
        text=True,
        cwd=ROOT,
        timeout=900,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-1000:]
    assert "PASS: 11/11" in proc.stdout
