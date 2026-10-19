"""The second domain must need no platform change (principle P-01): nothing in the platform's source mentions the CRM domain, and the CRM example's Python
imports only the kit, the standard library and PyYAML."""

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CRM_WORDS = re.compile(r"HAS_ACCOUNT_OWNER|renewal_prep|account_review|support_triage|crm-accounts|ESCALATED_TO|PROJECT_FOR")
PLATFORM = ("apps", "packages", "sdk", "tools", "deploy", "contracts")


def test_the_platform_does_not_mention_the_crm_domain() -> None:
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
                if CRM_WORDS.search(f.read_text(errors="ignore")):
                    hits.append(str(f.relative_to(ROOT)))
    assert not hits, f"CRM-specific names in platform source: {hits}"


def test_crm_python_imports_only_the_kit_and_standard_libraries() -> None:
    std = set(__import__("sys").stdlib_module_names)
    bad = []
    for f in (ROOT / "examples" / "domains" / "crm").rglob("*.py"):
        for node in ast.walk(ast.parse(f.read_text())):
            mods = (
                [a.name for a in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
                if isinstance(node, ast.ImportFrom) and node.level == 0
                else []
            )
            bad += [f"{f.name}: {m}" for m in mods if m and m.split(".")[0] not in std | {"yaml", "examples"}]
    assert not bad, bad


def test_the_crm_ontology_validates_with_the_platforms_own_validator() -> None:
    from kg_ontology.validate import validate_text

    spec, _, report = validate_text((ROOT / "examples" / "domains" / "crm" / "ontology.yaml").read_text())
    assert spec is not None and not report.errors, report.errors
    assert {"account_review", "renewal_prep", "support_triage"} <= set(spec.context_profiles)
