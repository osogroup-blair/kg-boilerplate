"""The graph projection job on the outbox (P3 §5.6). The P2 polling sweeper is gone (P4 WP0): the outbox is the only driver.

The job is shaped by the dispatcher contract (kg_jobs.JobHandler): `handle` projects the command's rows STATE-BASED (the graph is made equal to the
Postgres row), so running it twice, late or out of order gives the same graph (ADR-021). At-least-once delivery therefore needs no extra dedupe here:
a job that finds its rows already projected at their current revision succeeds without doing anything."""

from __future__ import annotations

import asyncio
import uuid

from kg_common.errors import NotFound
from kg_domain.principal import WorkspaceContext
from kg_jobs.client import ClaimedJob
from kg_jobs.errors import Category, JobError
from kg_jobs.policy import Decision
from kg_obs.logging import get_logger

from kg_knowledge.graphlock import GraphBusy, GraphLock
from kg_knowledge.projection import Projector, classify_error

log = get_logger("kg.projection.job")
SETTLED = frozenset({"projected", "skipped_by_policy", "not_applicable"})
# projection error code -> job category (ADR-012 mapping; codes come from projection.classify_error)
CATEGORY_BY_CODE: dict[str, Category] = {
    "graph_unavailable": Category.RETRYABLE,
    "graph_conflict": Category.RETRYABLE,
    "router_unavailable": Category.RETRYABLE,
    "endpoint_not_projected": Category.RETRYABLE,
    "unexpected_error": Category.RETRYABLE,
    "budget_exceeded": Category.BUDGET,
    "egress_denied": Category.POLICY_TERMINAL,
    "router_policy": Category.POLICY_TERMINAL,
    "router_rejected": Category.CONFIG_TERMINAL,
    "graph_rejected": Category.CONFIG_TERMINAL,
    "ontology_unreadable": Category.CONFIG_TERMINAL,
}


def category_for(code: str | None) -> Category:
    return CATEGORY_BY_CODE.get(code or "", Category.RETRYABLE)


class GraphProjectionHandler:
    """Projection jobs. Three shapes share the stage (P4): a command (P2/P3), a review decision or merge (also a command), and an interaction's
    extraction (restore everything it may have touched). All of them take the workspace's graph lock."""

    stage = "graph_projection"

    def __init__(self, projector: Projector, lock: GraphLock | None = None):
        self._projector, self._lock = projector, lock

    @staticmethod
    def _ctx(job: ClaimedJob) -> WorkspaceContext:
        return WorkspaceContext.for_system_task(job.workspace_id, reason="graph projection job")

    async def handle(self, job: ClaimedJob) -> None:
        ctx = self._ctx(job)
        try:
            if self._lock is not None:
                async with self._lock.hold(job.workspace_id, wait_seconds=60.0):
                    await self._run(job, ctx)
            else:
                await self._run(job, ctx)
        except GraphBusy as exc:
            raise JobError(Category.RETRYABLE, "graph_busy") from exc
        except (JobError, asyncio.CancelledError):
            raise
        except Exception as exc:
            code, _ = classify_error(exc)
            raise JobError(category_for(code), code) from exc

    async def _run(self, job: ClaimedJob, ctx: WorkspaceContext) -> None:
        if "interaction_id" in job.payload:
            report = await self._projector.restore_interaction(ctx, uuid.UUID(str(job.payload["interaction_id"])))
            if report.unsettled:
                raise JobError(Category.RETRYABLE, "graph_unavailable")
            return
        command_id = uuid.UUID(str(job.payload["command_id"]))
        try:
            # generation > 0 is an operator retry: rows that went `failed` are made due again first
            status = await (
                self._projector.retry_command(ctx, command_id) if job.generation > 0 else self._projector.project_command(ctx, command_id)
            )
        except NotFound as exc:
            raise JobError(Category.CONFIG_TERMINAL, "command_missing") from exc
        if status in SETTLED:
            return
        code = await self._projector.command_error_code(ctx, command_id) or "projection_pending"
        raise JobError(category_for(code), code)

    async def on_failure(self, job: ClaimedJob, decision: Decision) -> None:
        if "command_id" not in job.payload:
            return  # an interaction's restore job has no row to mark: the stage result is already written and the job is replayable
        ctx, command_id = self._ctx(job), uuid.UUID(str(job.payload["command_id"]))
        if decision.outcome == "skipped":
            await self._projector.mark_command_skipped(ctx, command_id, decision.code or "policy")
        elif decision.outcome == "dead":
            await self._projector.mark_command_failed(ctx, command_id, decision.code or "attempts_exhausted")
