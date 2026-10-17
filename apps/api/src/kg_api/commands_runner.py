"""Runs one deterministic command: decide and persist in ONE tenant transaction, then (after the commit) project to the graph within a time budget.

Postgres is authoritative, so the response is correct either way: 201 when the graph already reflects the command, 202 with a status URL when the
projection is still pending (the outbox job finishes it). A rejected command is stored too (the ledger records refusals), then answered with 422."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from fastapi import Request
from fastapi.responses import JSONResponse
from kg_db.session import tenant_session
from kg_domain.principal import Principal, WorkspaceContext
from kg_governance.audit import AuditWriter
from kg_knowledge.commands import CommandContext, PipelineResult
from kg_knowledge.graphlock import GraphBusy
from kg_obs import context
from kg_obs.logging import get_logger

from kg_api.errors import envelope
from kg_api.state import AppState

log = get_logger("kg.http.commands")
SETTLED = ("projected", "skipped_by_policy", "not_applicable")
FAST_PATH_LOCK_WAIT_SECONDS = 0.25


def _status_code(result: PipelineResult) -> int:
    if result.status == "needs_review":
        return 202
    if result.out.matched_existing:
        return 200
    return 201 if result.out.projection_status in SETTLED else 202


async def run_command(
    request: Request, state: AppState, principal: Principal, run: Callable[[CommandContext], Awaitable[PipelineResult]]
) -> JSONResponse:
    ws = WorkspaceContext.of(principal)
    async with tenant_session(state.engine, ws, principal.principal_id) as conn:
        audit = AuditWriter(conn, principal, request_id=context.request_id_var.get(), correlation_id=context.correlation_id_var.get())
        result = await run(CommandContext(conn, ws, principal, audit))
    headers = {"Location": result.out.status_url}
    if result.replayed:
        headers["Idempotent-Replay"] = "true"
    if result.status == "rejected":
        return envelope(
            422,
            "rule_violation",
            "The command violates an ontology rule",
            details={"command_id": str(result.command_id), "violations": result.violations},
            headers=headers,
        )
    if result.status == "applied" and result.out.projection_status == "pending":
        try:
            # the fast path is a graph writer too: it takes the workspace's graph lock, but never waits long for it (an extraction may hold it for a while).
            async with (
                state.knowledge.lock.hold(ws.workspace_id, wait_seconds=FAST_PATH_LOCK_WAIT_SECONDS),
                asyncio.timeout(state.settings.projection_request_budget_seconds),
            ):
                result.out.projection_status = await state.knowledge.projector.project_command(ws, result.command_id)  # type: ignore[assignment]
        except GraphBusy:
            log.info(
                "projection_deferred", reason="graph_busy"
            )  # the outbox job projects it once the other writer is done: 202 with a status URL
        except TimeoutError:
            log.info("projection_deferred", reason="budget")  # the outbox job finishes it; the answer below is 202 with a status URL
        except Exception as exc:
            log.warning("projection_attempt_errored", error_type=type(exc).__name__)
    return JSONResponse(result.out.model_dump(mode="json"), status_code=_status_code(result), headers=headers)


async def project_now(state: AppState, ws: WorkspaceContext, command_id: object) -> str:
    """The fast path of a ledger command that is not a deterministic knowledge command (a review decision, a correction, a split): restore the rows it changed
    within the request budget, after the commit. The outbox job finishes anything slower; Postgres is already right either way."""
    try:
        async with (
            state.knowledge.lock.hold(ws.workspace_id, wait_seconds=FAST_PATH_LOCK_WAIT_SECONDS),
            asyncio.timeout(state.settings.projection_request_budget_seconds),
        ):
            return str(await state.knowledge.projector.project_command(ws, command_id))  # type: ignore[arg-type]
    except GraphBusy:
        log.info("projection_deferred", reason="graph_busy")
    except TimeoutError:
        log.info("projection_deferred", reason="budget")
    except Exception as exc:
        log.warning("projection_attempt_errored", error_type=type(exc).__name__)
    return "pending"
