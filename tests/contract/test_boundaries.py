"""T-GR-06 and the other import boundaries, plus the OpenAPI drift test."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.contract


def test_import_linter_contracts_hold():
    """graphiti_core only in kg_graph; openai only in kg_modelgw/kg_graph; tools/spikes never imported by production code; layering."""
    exe = Path(sys.executable).parent / "lint-imports"
    proc = subprocess.run([str(exe)], cwd=ROOT, capture_output=True, text=True, check=False)  # noqa: S603
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "0 broken" in proc.stdout


def test_no_production_module_imports_graphiti_outside_kg_graph():
    offenders = []
    for path in list((ROOT / "packages").rglob("*.py")) + list((ROOT / "apps").rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        if "/kg_graph/" in rel or "/tests/" in rel:
            continue
        text = path.read_text()
        if "import graphiti_core" in text or "from graphiti_core" in text:
            offenders.append(rel)
    assert offenders == []


def test_openapi_matches_the_committed_contract():
    from kg_api.openapi_export import generate

    committed = (ROOT / "contracts" / "openapi.json").read_text()
    assert generate() == committed, "contracts/openapi.json is stale: run `make openapi`"
    spec = json.loads(committed)
    assert spec["info"]["version"] == "0.1.0" and all(p.startswith(("/v1", "/health")) for p in spec["paths"])
