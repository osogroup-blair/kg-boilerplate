from __future__ import annotations

import time

from fastapi import APIRouter
from kg_common.errors import DependencyUnavailable
from kg_common.ids import uuid7_str
from kg_domain.api import OperationsHealthOut, OutboxHealth, RouterCheckOut, RouterStatus
from kg_governance.audit import AuditMetadata
from kg_governance.authz.policy import Action, requires
from kg_governance.repositories import reads
from kg_jobs import reads as job_reads
from kg_modelgw.errors import EgressDenied, GatewayException, RouterError
from kg_modelgw.gateway import Correlation
from kg_obs import context

from kg_api.deps import AuditDep, DbDep, PrincipalDep, StateDep, WorkspaceDep
from kg_api.routers.health import check_neo4j, check_postgres, check_router

router = APIRouter(tags=["operations"])
PROBE_TEXT = "kg platform router probe"  # constant, content-free


@router.get("/v1/operations/health", response_model=OperationsHealthOut)
@requires(Action.OPERATIONS_READ)
async def operations_health(state: StateDep, ws: WorkspaceDep, db: DbDep) -> OperationsHealthOut:
    postgres, neo4j, router_h = await check_postgres(state), await check_neo4j(state), await check_router(state)
    worker = await reads.worker_status(db, state.settings.worker_stale_seconds)
    node_count: int | None = None
    if neo4j == "ok":
        try:
            node_count = await state.graph.scoped(ws).node_count()
        except Exception:
            node_count = None
    box = await job_reads.backlog(db, ws.workspace_id)
    status = "down" if "down" in (postgres, neo4j) else ("degraded" if router_h.status != "ok" or worker.status != "ok" else "ok")
    return OperationsHealthOut(
        status=status,
        postgres=postgres,
        neo4j=neo4j,
        router=RouterStatus(mode=state.settings.oso_router_mode, reachability=router_h.status, missing_aliases=router_h.missing_aliases),
        worker=worker,
        principal_counts=await reads.principal_counts(db, ws),
        graph_node_count=node_count,
        outbox=OutboxHealth(backlog=box["backlog"], oldest_pending_age_s=box["oldest_pending_age_seconds"], dead=box["dead"]),
    )


@router.post("/v1/operations/router-check", response_model=RouterCheckOut)
@requires(Action.DIAGNOSTICS_RUN)
async def router_check(state: StateDep, principal: PrincipalDep, audit: AuditDep) -> RouterCheckOut:
    """Embeds a constant probe through ModelGateway (classification `internal`): proves the transport end to end and writes a model_calls row."""
    corr = Correlation(context.correlation_id_var.get() or uuid7_str(), str(principal.workspace_id), str(principal.principal_id), "kg-api")
    started = time.perf_counter()
    try:
        result = await state.router.gateway.embed("embedding", [PROBE_TEXT], kind="query", classification="internal", correlation=corr)
    except (RouterError, EgressDenied, GatewayException) as exc:
        await state.call_sink.flush()
        raise DependencyUnavailable("Model router check failed", details={"router_code": getattr(exc, "code", "egress_denied")}) from exc
    await state.call_sink.flush()  # the model_calls row exists once the probe returns
    alias = state.router.config.embedding.alias
    await audit.record(
        "operations.router_check", resource_type="model_router", metadata=AuditMetadata(alias=alias, count=len(result.vectors))
    )
    return RouterCheckOut(
        ok=True,
        alias=alias,
        dims=len(result.vectors[0]),
        latency_ms=round((time.perf_counter() - started) * 1000, 2),
        router_request_id=result.router_request_ids[0] or None,
    )
