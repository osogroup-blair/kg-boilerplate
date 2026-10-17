"""What a reconcile scan reports. Every class is a closed name; a finding is an id (never content); the report carries counts and a few sample ids per class."""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass, field
from typing import Literal

Scope = Literal["all", "graph", "memory", "ledger", "deletions"]
SCOPES: tuple[str, ...] = ("all", "graph", "memory", "ledger", "deletions")
SAMPLE = 20


@dataclass(frozen=True)
class DriftClass:
    name: str
    scope: str
    description: str
    repair: str  # what `--repair` does, or "person" when only a human can settle it
    spends_money: bool = False  # a repair that calls a model needs allow_reingest


CLASSES: dict[str, DriftClass] = {
    c.name: c
    for c in (
        DriftClass("graph.entity.missing", "graph", "an entity the graph should have is missing", "re-project from Postgres"),
        DriftClass("graph.entity.mismatch", "graph", "an entity node differs from its Postgres row", "re-project from Postgres"),
        DriftClass("graph.entity.orphan", "graph", "a graph node with no Postgres entity", "delete the node"),
        DriftClass("graph.fact.missing", "graph", "a fact the graph should have is missing", "re-project from Postgres"),
        DriftClass("graph.fact.mismatch", "graph", "a fact edge differs from its Postgres row", "re-project from Postgres"),
        DriftClass("graph.fact.orphan", "graph", "a graph edge with no Postgres fact", "delete the edge"),
        DriftClass("graph.episode.missing", "graph", "a recorded episode is not in the graph", "re-run graph_ingest (model calls)", True),
        DriftClass(
            "graph.episode.unrecorded",
            "graph",
            "an episode of a half-finished extraction has no record",
            "re-run graph_ingest (model calls)",
            True,
        ),
        DriftClass("graph.episode.orphan", "graph", "an episode in the graph belongs to no interaction", "delete the episode"),
        DriftClass("graph.mention.missing", "graph", "a recorded mention is not in the graph", "re-run graph_projection"),
        DriftClass("ledger.missing_stage", "ledger", "an interaction lacks a stage its pipeline defines", "insert the stage"),
        DriftClass("ledger.missing_job", "ledger", "work is waiting but no live job exists", "enqueue the job"),
        DriftClass("ledger.terminal_job_open_stage", "ledger", "a job finished but its stage is still open", "new generation and job"),
        DriftClass("ledger.orphan_job", "ledger", "a pending job for an interaction that no longer exists", "cancel the job"),
        DriftClass("ledger.job_missing_subject", "ledger", "a pending job for a command or memory that no longer exists", "cancel the job"),
        DriftClass("ledger.context_item_tombstoned", "ledger", "a context trace item names a deleted fact or memory", "person"),
        DriftClass(
            "memory.embedding_missing", "memory", "an active memory has no vector for the current embedding version", "enqueue memory_embed"
        ),
        DriftClass(
            "deletions.residual",
            "deletions",
            "a completed deletion left rows that point at deleted ids",
            "open a deletion_exception review",
        ),
    )
}


@dataclass
class ReconcileResult:
    run_id: uuid.UUID
    workspace_id: uuid.UUID
    scope: str
    mode: str  # check | repair
    trigger: str
    status: str = "completed"
    drift: dict[str, int] = field(default_factory=dict)
    samples: dict[str, list[str]] = field(default_factory=dict)
    repaired: dict[str, int] = field(default_factory=dict)
    needs_confirmation: dict[str, int] = field(default_factory=dict)  # classes whose repair spends money, with how many items
    reingest_preview: dict[str, float | int] | None = None
    errors: list[str] = field(default_factory=list)
    started_at: dt.datetime | None = None
    finished_at: dt.datetime | None = None

    @property
    def clean(self) -> bool:
        return not any(self.drift.values()) or all(self.drift.get(c, 0) <= self.repaired.get(c, 0) for c in self.drift)

    def add(self, name: str, ids: list[str]) -> None:
        if not ids:
            return
        self.drift[name] = self.drift.get(name, 0) + len(ids)
        have = self.samples.setdefault(name, [])
        have.extend(ids[: max(0, SAMPLE - len(have))])

    def repaired_n(self, name: str, n: int) -> None:
        if n:
            self.repaired[name] = self.repaired.get(name, 0) + n

    def as_dict(self) -> dict[str, object]:
        return {
            "run_id": str(self.run_id),
            "workspace_id": str(self.workspace_id),
            "scope": self.scope,
            "mode": self.mode,
            "trigger": self.trigger,
            "status": self.status,
            "drift": dict(sorted(self.drift.items())),
            "samples": {k: v for k, v in sorted(self.samples.items())},
            "repaired": dict(sorted(self.repaired.items())),
            "needs_confirmation": dict(sorted(self.needs_confirmation.items())),
            "reingest_preview": self.reingest_preview,
            "errors": self.errors,
            "clean": self.clean,
        }
