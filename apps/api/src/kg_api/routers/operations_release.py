"""Operations v2 (phase-9 plan WP9): reconciliation here; cost, latency, freshness, backups and live-evaluation views are added below. Everything is a server-side aggregate from
Postgres (the console needs no Prometheus access) and carries ids, counts and enums only."""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query
from kg_common.errors import ValidationFailed
from kg_domain.principal import WorkspaceContext
from kg_governance.authz.policy import Action, enforce, requires
from kg_reconcile import CLASSES
from pydantic import BaseModel, ConfigDict
from sqlalchemy import text

from kg_api.deps import DbDep, PrincipalDep, StateDep, WorkspaceDep
from kg_api.http import IdempotencyKeyHeader, require_key

router = APIRouter(prefix="/v1/operations", tags=["operations"])


class ReconcileIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope: Literal["all", "graph", "memory", "ledger", "deletions"] = "all"
    repair: bool = False
    allow_reingest: bool = False


class ReconcileClassOut(BaseModel):
    name: str
    scope: str
    description: str
    repair: str
    spends_money: bool


class ReconcileRunOut(BaseModel):
    id: uuid.UUID
    scope: str
    mode: str
    trigger: str
    status: str
    drift: dict[str, int]
    repaired: dict[str, int]
    error_code: str | None = None
    started_at: Any
    finished_at: Any = None


class ReconcileOverview(BaseModel):
    classes: list[ReconcileClassOut]
    runs: list[ReconcileRunOut]


@router.get("/reconcile", response_model=ReconcileOverview)
@requires(Action.OPERATIONS_READ)
async def reconcile_overview(ws: WorkspaceDep, db: DbDep, limit: Annotated[int, Query(ge=1, le=50)] = 10) -> ReconcileOverview:
    rows = (
        await db.execute(
            text(
                "SELECT id, scope, mode, trigger, status, drift, repaired, error_code, started_at, finished_at FROM reconcile_runs "
                "WHERE workspace_id = :ws ORDER BY started_at DESC LIMIT :n"
            ),
            {"ws": ws.workspace_id, "n": limit},
        )
    ).all()
    return ReconcileOverview(
        classes=[
            ReconcileClassOut(name=c.name, scope=c.scope, description=c.description, repair=c.repair, spends_money=c.spends_money)
            for c in CLASSES.values()
        ],
        runs=[ReconcileRunOut(**dict(r._mapping)) for r in rows],
    )


@router.post("/reconcile")
@requires(Action.OPERATIONS_READ)
async def reconcile_run(
    body: ReconcileIn, state: StateDep, principal: PrincipalDep, idempotency_key: IdempotencyKeyHeader = None
) -> dict[str, object]:
    """A scan (any operator) or, with `repair`, a scan plus the SAFE repairs (operations.repair: admins and operators). A repair that would call a model needs `allow_reingest`
    and answers with the cost preview first (`needs_confirmation`)."""
    if body.allow_reingest and not body.repair:
        raise ValidationFailed("allow_reingest only applies with repair", details={"fields": ["allow_reingest"]})
    if body.repair:
        enforce(principal, Action.OPERATIONS_REPAIR)
        require_key(idempotency_key)
    ctx = WorkspaceContext.of(principal)
    result = await state.reconcile.run(
        ctx, scope=body.scope, repair=body.repair, allow_reingest=body.allow_reingest, trigger="manual", started_by=principal.principal_id
    )
    return result.as_dict()
