"""What each frozen dataset IS: the files whose bytes define its cases, and how to list its case ids. A dataset is frozen when `manifest.json` records the sha256 of every one of
those files; `verify` recomputes them, so a gold file edited without re-freezing (and without bumping the version in the release baseline) fails the gate."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
DATASETS = ROOT / "eval" / "datasets"


def _glob(*patterns: str) -> list[str]:
    out: set[str] = set()
    for pattern in patterns:
        out.update(str(p.relative_to(ROOT)) for p in ROOT.glob(pattern) if p.is_file())
    return sorted(out)


def _context_ids() -> list[str]:
    return [c["id"] for c in yaml.safe_load((ROOT / "eval/context/benchmark.yaml").read_text())["cases"]]


def _extraction_ids() -> list[str]:
    from eval.extraction.gold import load

    return sorted(str(g["id"]) for g in load())


def _memory_ids() -> list[str]:
    from eval.memory.gold import load

    return sorted(str(g["id"]) for g in load())


def _consolidation_ids() -> list[str]:
    from eval.consolidation import gold

    return sorted([f"reinforce:{k}" for k in gold.REINFORCE] + [f"synth:{getattr(c, 'id', i)}" for i, c in enumerate(gold.SYNTHESIS)])


def _injection_ids() -> list[str]:
    return [p["id"] for p in yaml.safe_load((ROOT / "tests/fixtures/injection/corpus.yaml").read_text())["payloads"]]


def _generated_ids(suite: str) -> Callable[[], list[str]]:
    def ids() -> list[str]:
        path = DATASETS / suite / "v1" / "cases.json"
        return [c["id"] for c in json.loads(path.read_text())["cases"]] if path.exists() else []

    return ids


SUITES: dict[str, dict[str, Any]] = {
    "context": {
        "sources": lambda: ["eval/context/benchmark.yaml"],
        "ids": _context_ids,
        "ontology": "v1.3.0 + v1.4.0 (subject ACLs)",
        "note": "P6's 16 frozen cases; the authorization, deleted-subject and corrected-fact cases live in their own suites",
    },
    "extraction": {
        "sources": lambda: ["eval/extraction/gold.py", *_glob("tools/mock_router/fixtures/scenarios/*.json")],
        "ids": _extraction_ids,
        "ontology": "v1.0.0",
        "note": "P4's scenarios (the gold entry of each scenario fixture)",
    },
    "memory": {
        "sources": lambda: ["eval/memory/gold.py", "eval/memory/queries.py", *_glob("tools/mock_router/fixtures/scenarios/memory_*.json")],
        "ids": _memory_ids,
        "ontology": "v1.2.0",
        "note": "P5's scenarios and the labelled retrieval queries",
    },
    "consolidation": {
        "sources": lambda: ["eval/consolidation/gold.py"],
        "ids": _consolidation_ids,
        "ontology": "v1.5.0",
        "note": "P8's reinforcement and synthesis gold",
    },
    "injection": {
        "sources": lambda: ["tests/fixtures/injection/corpus.yaml"],
        "ids": _injection_ids,
        "ontology": "v1.5.0",
        "note": "SEC-04 / AT-15: hostile text pushed through every ingress",
    },
    "conflicts": {
        "sources": lambda: ["eval/datasets/conflicts/v1/cases.json"],
        "ids": _generated_ids("conflicts"),
        "ontology": "(pure policy)",
        "note": "authority x overlap matrix for the conflict policy, and identity cases (same-name people, aliases, external-id conflicts)",
    },
    "authorization": {
        "sources": lambda: ["eval/datasets/authorization/v1/cases.json"],
        "ids": _generated_ids("authorization"),
        "ontology": "v1.5.0",
        "note": "generated probes: every route x principal x workspace x frozen/deleted state",
    },
}
VERSION = "v1"
