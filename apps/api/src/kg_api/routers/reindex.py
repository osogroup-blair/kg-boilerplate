"""Flow F (phase-9 plan WP8): rebuild the graph of a workspace in a separate namespace, check it, cut over atomically, roll back. Admin only (`reindex.run`).

POST /v1/ontologies/versions/{id}/reindex   preview (confirm=false) or start (confirm=true; Idempotency-Key; `max_cost_usd` is a ceiling you set)
GET  /v1/reindex-runs[/{id}]                progress, the verification report, the comparison with the live namespace
POST /v1/reindex-runs/{id}/cancel
GET  /v1/graph/namespaces
POST /v1/graph/namespaces/{namespace}/activate   cutover of a READY namespace, or rollback to a RETIRED one (the response says which)"""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from kg_common.errors import Conflict, NotFound
from kg_domain.principal import WorkspaceContext
from kg_governance.authz.policy import Action, requires
from pydantic import BaseModel, ConfigDict, Field

from kg_api.deps import AuditDep, DbDep, PrincipalDep, StateDep, WorkspaceDep
from kg_api.http import IdempotencyKeyHeader, require_key

router = APIRouter(prefix="/v1", tags=["reindex"])
NO_STORE = {"Cache-Control": "no-store"}


class ReindexIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["project", "reextract"] = "project"
    confirm: bool = False
    max_cost_usd: float | None = Field(default=None, ge=0.0)


class ActivateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason_code: str = Field(default="operator_decision", pattern=r"^[a-z][a-z0-9_]{0,63}$")


class CostCeilingExceeded(Conflict):
    code, default_message = "cost_ceiling_exceeded", "The estimated cost is above the ceiling you set"


@router.post("/ontologies/versions/{version_id}/reindex")
@requires(Action.REINDEX_RUN)
async def reindex(
    version_id: uuid.UUID,
    body: ReindexIn,
    state: StateDep,
    principal: PrincipalDep,
    ws: WorkspaceDep,
    db: DbDep,
    audit: AuditDep,
    idempotency_key: IdempotencyKeyHeader = None,
) -> JSONResponse:
    """Without `confirm`: the estimate (entities, facts, embedding calls, cost) and the namespace that would be built. With it: starts the build as a job (202)."""
    await state.knowledge.specs.get(db, ws.workspace_id, version_id)  # the version must exist here (published or retired)
    if not body.confirm:
        est = await state.reindex.preview(db, ws, mode=body.mode)
        return JSONResponse({"status": "preview", **est, "ontology_version_id": str(version_id)}, headers=NO_STORE)
    key = require_key(idempotency_key)
    est = await state.reindex.preview(db, ws, mode=body.mode)
    if body.max_cost_usd is not None and est["estimated_cost_usd"] > body.max_cost_usd:
        raise CostCeilingExceeded(details={"estimated_cost_usd": est["estimated_cost_usd"], "max_cost_usd": body.max_cost_usd})
    run = await state.reindex.start(db, ws, principal, mode=body.mode, ontology_version_id=version_id, audit=audit, key=key)
    return JSONResponse(_json(run), status_code=202, headers={**NO_STORE, "Location": f"/v1/reindex-runs/{run['id']}"})


@router.get("/reindex-runs")
@requires(Action.REINDEX_RUN)
async def list_runs(state: StateDep, ws: WorkspaceDep, db: DbDep, limit: Annotated[int, Query(ge=1, le=50)] = 20) -> JSONResponse:
    return JSONResponse([_json(r) for r in await state.reindex.list_runs(db, ws.workspace_id, limit)], headers=NO_STORE)


@router.get("/reindex-runs/{run_id}")
@requires(Action.REINDEX_RUN)
async def get_run(run_id: uuid.UUID, state: StateDep, ws: WorkspaceDep, db: DbDep) -> JSONResponse:
    return JSONResponse(_json(await state.reindex.get(db, ws.workspace_id, run_id)), headers=NO_STORE)


@router.post("/reindex-runs/{run_id}/cancel")
@requires(Action.REINDEX_RUN)
async def cancel_run(
    run_id: uuid.UUID, state: StateDep, ws: WorkspaceDep, audit: AuditDep, idempotency_key: IdempotencyKeyHeader = None
) -> JSONResponse:
    require_key(idempotency_key)
    return JSONResponse(_json(await state.reindex.cancel(ws, run_id, audit)), headers=NO_STORE)


@router.get("/graph/namespaces")
@requires(Action.REINDEX_RUN)
async def list_namespaces(state: StateDep, ws: WorkspaceDep, db: DbDep) -> JSONResponse:
    return JSONResponse(_json(await state.reindex.namespaces(db, ws.workspace_id)), headers=NO_STORE)


@router.post("/graph/namespaces/{namespace}/activate")
@requires(Action.REINDEX_RUN)
async def activate_namespace(
    namespace: str,
    body: ActivateIn,
    state: StateDep,
    principal: PrincipalDep,
    ws: WorkspaceDep,
    db: DbDep,
    audit: AuditDep,
    idempotency_key: IdempotencyKeyHeader = None,
) -> JSONResponse:
    """A namespace that is `building` and READY is cut over to; a `retired` one (inside its retention) is rolled back to."""
    key = require_key(idempotency_key)
    row = (
        await db.execute(
            __import__("sqlalchemy").text(
                "SELECT status, reindex_run_id FROM workspace_graph_namespaces WHERE workspace_id = :ws AND namespace = :ns"
            ),
            {"ws": ws.workspace_id, "ns": namespace},
        )
    ).first()
    if row is None:
        raise NotFound()
    ctx = WorkspaceContext.of(principal)
    if row.status == "building":
        out = await state.reindex.activate(ctx, row.reindex_run_id, principal, key=key, audit=audit)
        action = "cutover"
    elif row.status == "retired":
        out = await state.reindex.rollback(ctx, namespace, principal, key=key, audit=audit)
        action = "rollback"
    else:
        raise Conflict(
            "this namespace is already the active one" if row.status == "active" else "this namespace cannot be activated",
            details={"status": row.status},
        )
    return JSONResponse({"action": action, "run": _json(out)}, headers=NO_STORE)


def _json(value: Any) -> Any:
    from fastapi.encoders import jsonable_encoder

    return jsonable_encoder(value)
