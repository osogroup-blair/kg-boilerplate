"""The tutorial domain (a support desk) must need no platform change, and must run end to end."""

import os
import re
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
SUPPORT_WORDS = re.compile(r"ticket_triage|support-desk|ABOUT_PRODUCT|FOR_CUSTOMER|OPENED_BY")
PLATFORM = ("apps", "packages", "sdk", "tools", "deploy", "contracts")


def test_the_platform_does_not_mention_the_support_domain() -> None:
    hits = []
    for top in PLATFORM:
        for f in (ROOT / top).rglob("*"):
            if (
                f.is_file()
                and f.suffix in {".py", ".yaml", ".yml", ".json", ".ts", ".tsx", ".sql"}
                and "node_modules" not in f.parts
                and f.relative_to(ROOT).parts[:2]
                != ("apps", "site")  # the public site shows the example domains; it is documentation, not platform source
                and f.stat().st_size < 2_000_000
            ):
                if SUPPORT_WORDS.search(f.read_text(errors="ignore")):
                    hits.append(str(f.relative_to(ROOT)))
    assert not hits, f"support-domain names in platform source: {hits}"


def test_the_support_ontology_validates_with_the_platforms_own_validator() -> None:
    from kg_ontology.validate import validate_text

    spec, _, report = validate_text((ROOT / "examples" / "domains" / "support" / "ontology.yaml").read_text())
    assert spec is not None and not report.errors, report.errors
    assert "ticket_triage" in spec.context_profiles


def _stack_up() -> bool:
    try:
        return httpx.get("http://127.0.0.1:8000/health", timeout=2).status_code == 200
    except httpx.HTTPError:
        return False


@pytest.mark.skipif(not _stack_up() and os.environ.get("KG_REQUIRE_STACK") != "1", reason="the dev stack is not up")
def test_support_domain_passes(tmp_path: Path) -> None:
    proc = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "examples.domains.support.run", "--reports", str(tmp_path)],
        capture_output=True,
        text=True,
        cwd=ROOT,
        timeout=900,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-1000:]
    assert "PASS: 8/8" in proc.stdout
