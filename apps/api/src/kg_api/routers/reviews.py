"""The review queue (P4, ADR-027/028): list and read items, read the evidence behind one (audited), decide.

A decision is a ledger command (`review.decide`): idempotent, audited, projected by the outbox. The reviewer's evidence read is narrow on purpose: the content of an
interaction the item itself rests on, never a search through the ledger (that stays an admin action)."""

from __future__ import annotations

import asyncio
import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from kg_common.errors import NotFound
from kg_db.session import tenant_session
from kg_domain.envelope import Page
from kg_governance.audit import AuditMetadata, AuditWriter
from kg_governance.authz.policy import Action, is_allowed, requires
from kg_ingestion.models import ContentOut
from kg_knowledge.commands import CommandContext
from kg_knowledge.graphlock import GraphBusy
from kg_knowledge.models import DecisionIn, DecisionOut, ReviewDetail, ReviewOut
from kg_obs import context
from kg_obs.logging import get_logger

from kg_api.deps import DbDep, PrincipalDep, StateDep, WorkspaceDep
from kg_api.http import IdempotencyKeyHeader, IfMatchHeader, etag, parse_if_match, require_key

router = APIRouter(prefix="/v1/reviews", tags=["reviews"])
log = get_logger("kg.http.reviews")
LimitQ = Annotated[int, Query(ge=1, le=100)]
SETTLED = ("projected", "skipped_by_policy", "not_applicable")


@router.get("", response_model=Page[ReviewOut])
@requires(Action.REVIEW_READ)
async def list_reviews(
    state: StateDep,
    ws: WorkspaceDep,
    db: DbDep,
    type: Literal["policy_violation", "identity_match", "contradiction", "graphiti_invalidation"] | None = None,
    status: Literal["open", "decided"] | None = None,
    limit: LimitQ = 50,
    cursor: str | None = None,
) -> Page[ReviewOut]:
    return await state.knowledge.reviews.list_items(db, ws, item_type=type, status=status, limit=limit, cursor=cursor)


@router.get("/{review_id}", response_model=ReviewDetail)
@requires(Action.REVIEW_READ)
async def get_review(review_id: uuid.UUID, state: StateDep, principal: PrincipalDep, ws: WorkspaceDep, db: DbDep) -> JSONResponse:
    out = await state.knowledge.reviews.get(db, ws, review_id, can_see_sensitive=is_allowed(principal, Action.ENTITY_READ_SENSITIVE))
    return JSONResponse(out.model_dump(mode="json"), headers={"ETag": etag(out.revision)})


@router.get("/{review_id}/evidence/{interaction_id}", response_model=ContentOut)
@requires(Action.REVIEW_READ_EVIDENCE)
async def read_review_evidence(
    review_id: uuid.UUID, interaction_id: uuid.UUID, request: Request, state: StateDep, principal: PrincipalDep, ws: WorkspaceDep, db: DbDep
) -> JSONResponse:
    """The normalised content of ONE interaction the item rests on (Q14). Any other interaction id is a 404; every read is audited in the same transaction and
    the response is never cached."""
    refs = await state.knowledge.reviews.interactions_for(db, ws.workspace_id, review_id)  # a 404 for a review of another workspace, too
    if interaction_id not in {r.interaction_id for r in refs}:
        raise NotFound()
    out = await state.ingestion.reads.content(db, ws, interaction_id, "normalized")
    await AuditWriter(db, principal, request_id=context.request_id_var.get(), correlation_id=context.correlation_id_var.get()).record(
        "review.evidence_read",
        resource_type="review_item",
        resource_id=review_id,
        metadata=AuditMetadata(review_id=review_id, interaction_id=interaction_id, form="normalized"),
    )
    return JSONResponse(out.model_dump(mode="json"), headers={"Cache-Control": "no-store"})


@router.post(
    "/{review_id}/decision",
    response_model=DecisionOut,
    responses={
        409: {"description": "already_decided / requires_correction / not_available / cannot_reapply / idempotency_mismatch"},
        412: {"description": "If-Match does not match the item's current revision"},
        428: {"description": "If-Match is required"},
    },
)
@requires(Action.REVIEW_DECIDE)
async def decide_review(
    review_id: uuid.UUID,
    body: DecisionIn,
    request: Request,
    state: StateDep,
    principal: PrincipalDep,
    ws: WorkspaceDep,
    idempotency_key: IdempotencyKeyHeader = None,
    if_match: IfMatchHeader = None,
) -> JSONResponse:
    key, revision = require_key(idempotency_key), parse_if_match(if_match)
    async with tenant_session(state.engine, ws, principal.principal_id) as conn:
        audit = AuditWriter(conn, principal, request_id=context.request_id_var.get(), correlation_id=context.correlation_id_var.get())
        out, replayed = await state.knowledge.reviews.decide(
            CommandContext(conn, ws, principal, audit), review_id, body, key=key, if_match=revision
        )
    if not replayed and out.command_id is not None and out.projection_status == "pending":
        out.projection_status = await _project_now(state, ws, out.command_id)  # type: ignore[assignment]
    headers = {"ETag": etag(out.revision)}
    if replayed:
        headers["Idempotent-Replay"] = "true"
    return JSONResponse(out.model_dump(mode="json"), headers=headers)


async def _project_now(state: Any, ws: Any, command_id: uuid.UUID) -> str:
    """The fast path of a decision: restore the rows it changed within the request budget; the outbox job finishes anything slower."""
    try:
        async with (
            state.knowledge.lock.hold(ws.workspace_id, wait_seconds=0.25),
            asyncio.timeout(state.settings.projection_request_budget_seconds),
        ):
            return str(await state.knowledge.projector.project_command(ws, command_id))
    except (GraphBusy, TimeoutError):
        return "pending"
    except Exception as exc:
        log.warning("decision_projection_deferred", error_type=type(exc).__name__)
        return "pending"
