"""Step results of an example run: pass/fail with the blueprint ids and the evidence, written as JSON and markdown. No raw content."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Step:
    id: str
    title: str
    at_ids: list[str]
    passed: bool
    evidence: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


class Report:
    def __init__(self, name: str, mode: str = "mock") -> None:
        self.name, self.mode, self.steps = name, mode, []  # type: str, str, list[Step]

    def add(self, step: Step) -> Step:
        self.steps.append(step)
        return step

    @property
    def passed(self) -> bool:
        return bool(self.steps) and all(s.passed for s in self.steps)

    def to_json(self) -> str:
        return json.dumps(
            {"name": self.name, "mode": self.mode, "passed": self.passed, "steps": [asdict(s) for s in self.steps]},
            indent=2,
            sort_keys=True,
        )

    def to_markdown(self) -> str:
        out = [
            f"# {self.name} ({self.mode})",
            "",
            f"Verdict: **{'PASS' if self.passed else 'FAIL'}**",
            "",
            "| step | title | blueprint ids | result |",
            "|---|---|---|---|",
        ]
        for s in self.steps:
            out.append(f"| {s.id} | {s.title} | {', '.join(s.at_ids)} | {'pass' if s.passed else 'FAIL: ' + (s.error or '')} |")
        return "\n".join(out) + "\n"

    def write(self, directory: Path, stem: str) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{stem}.json").write_text(self.to_json() + "\n")
        (directory / f"{stem}.md").write_text(self.to_markdown())
