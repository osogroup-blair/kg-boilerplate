"""The memory gold set: the `memory_gold` entry of every scenario file under tools/mock_router/fixtures/scenarios. One source of truth for what a scenario means: the mock
supplies the model's answers from the same files, the evaluation scores what the platform did with them (mock or live)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

SCENARIOS = Path(__file__).resolve().parents[2] / "tools" / "mock_router" / "fixtures" / "scenarios"


def load() -> list[dict[str, Any]]:
    out = []
    for path in sorted(SCENARIOS.glob("memory_*.json")):
        for entry in json.loads(path.read_text()):
            if "memory_gold" in entry:
                out.append({"file": path.stem, **entry["memory_gold"]})
    return out
