"""The extraction gold set: the `gold` entry of every scenario under tools/mock_router/fixtures/scenarios. One source of truth for what the scenario means, used
by the mock (which supplies the model's answers) and by the evaluation (which scores what the platform did with them, mock or live)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

SCENARIOS = Path(__file__).resolve().parents[2] / "tools" / "mock_router" / "fixtures" / "scenarios"
SARAH_STORY = {
    "entities": [("Person", "Sarah Johnson"), ("Organization", "Acme"), ("Person", "Michael"), ("Topic", "Entrepreneurship")],
    "facts": [
        ("WORKS_AT", "Sarah Johnson", "Acme", "2022-01-10", "Software Engineer"),
        ("WORKS_AT", "Sarah Johnson", "Acme", "2024-06-01", "Engineering Manager"),
    ],
}


def load() -> list[dict[str, Any]]:
    out = []
    for path in sorted(SCENARIOS.glob("*.json")):
        for entry in json.loads(path.read_text()):
            if "gold" in entry:
                out.append({"file": path.stem, **entry["gold"]})
    return out
