"""Entities: the directory, detail with history, and the deterministic create/update commands (Flow B)."""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from kg_domain.envelope import Page
from kg_governance.authz.policy import Action, is_allowed, requires
from kg_knowledge.models import (
    AttributeClaimOut,
    CommandOut,
    DerivedSummaryOut,
    EntityCreateIn,
    EntityDetailOut,
    EntityOut,
    EntityUpdateIn,
    FactOut,
    GraphOut,
    Instant,
    MentionOut,
)
from kg_memory.reads import Viewer
from kg_memory.scopes import ScopeGrants

from kg_api.commands_runner import run_command
from kg_api.deps import DbDep, PrincipalDep, StateDep, SubjectsDep, WorkspaceDep
from kg_api.http import IdempotencyKeyHeader, IfMatchHeader, etag, parse_if_match, require_key

router = APIRouter(prefix="/v1/entities", tags=["entities"])


def parse_include(value: str | None) -> tuple[bool, list[str]]:
    """`include=history,proposed` -> (history?, extra statuses). `historical` is accepted as an alias of `history`."""
    parts = {p.strip() for p in (value or "").split(",") if p.strip()}
    return bool(parts & {"history", "historical"}), sorted(parts & {"proposed", "disputed", "rejected"})


LimitQ = Annotated[int, Query(ge=1, le=100)]
COMMAND_RESPONSES: dict[int | str, dict[str, Any]] = {
    200: {"model": CommandOut, "description": "matched_existing: an entity with that external identifier already exists"},
    201: {"model": CommandOut, "description": "Applied and projected"},
    202: {"model": CommandOut, "description": "Accepted: projection pending, or queued for review (needs_review)"},
    409: {"description": "no_active_ontology / idempotency_mismatch"},
    422: {"description": "rule_violation (details.violations) / unknown_entity / validation_failed"},
}


@router.post("", response_model=None, status_code=201, responses=COMMAND_RESPONSES)
@requires(Action.ENTITY_WRITE)
async def create_entity(
    body: EntityCreateIn, request: Request, state: StateDep, principal: PrincipalDep, idempotency_key: IdempotencyKeyHeader = None
) -> JSONResponse:
    key = require_key(idempotency_key)
    return await run_command(request, state, principal, lambda cc: state.knowledge.pipeline.entity_create(cc, key, body))


@router.get("", response_model=Page[EntityOut])
@requires(Action.ENTITY_READ)
async def list_entities(
    state: StateDep,
    principal: PrincipalDep,
    ws: WorkspaceDep,
    db: DbDep,
    subjects: SubjectsDep,
    type: Annotated[str | None, Query(max_length=64)] = None,
    q: Annotated[str | None, Query(max_length=200)] = None,
    status: Literal["active", "deleted", "candidate", "untyped", "merged"] | None = None,
    limit: LimitQ = 50,
    cursor: str | None = None,
) -> Page[EntityOut]:
    return await state.knowledge.reads.list_entities(
        db,
        ws,
        can_see_sensitive=is_allowed(principal, Action.ENTITY_READ_SENSITIVE),
        subjects=subjects,
        type_key=type,
        q=q,
        status=status,
        limit=limit,
        cursor=cursor,
    )


@router.get("/{entity_id}", response_model=EntityDetailOut)
@requires(Action.ENTITY_READ)
async def get_entity(
    entity_id: uuid.UUID,
    state: StateDep,
    principal: PrincipalDep,
    ws: WorkspaceDep,
    db: DbDep,
    subjects: SubjectsDep,
    include: Annotated[str | None, Query(max_length=100, description="comma list: history, proposed, disputed, rejected")] = None,
    as_of: Annotated[Instant | None, Query()] = None,
    known_at: Annotated[Instant | None, Query()] = None,
) -> JSONResponse:
    """A merged entity still answers (200) with `merged_into_id`: an id is never reused or lost (KGR-01)."""
    history, extras = parse_include(include)
    out = await state.knowledge.reads.get_entity(
        db,
        ws,
        entity_id,
        can_see_sensitive=is_allowed(principal, Action.ENTITY_READ_SENSITIVE),
        subjects=subjects,
        include_history=history,
        as_of=as_of,
        known_at=known_at,
        include=extras,
    )
    if is_allowed(principal, Action.MEMORY_READ):
        viewer = Viewer(principal, await ScopeGrants.for_principal(db, principal), curator=False, subjects=subjects)
        out.memories = await state.memory.reads.count_visible(db, viewer, entity_id)
    return JSONResponse(out.model_dump(mode="json"), headers={"ETag": etag(out.revision)})


@router.patch(
    "/{entity_id}",
    response_model=None,
    responses={**COMMAND_RESPONSES, 412: {"description": "If-Match does not match"}, 428: {"description": "If-Match required"}},
)
@requires(Action.ENTITY_WRITE)
async def update_entity(
    entity_id: uuid.UUID,
    body: EntityUpdateIn,
    request: Request,
    state: StateDep,
    principal: PrincipalDep,
    idempotency_key: IdempotencyKeyHeader = None,
    if_match: IfMatchHeader = None,
) -> JSONResponse:
    key = require_key(idempotency_key)
    revision = parse_if_match(if_match)
    return await run_command(
        request, state, principal, lambda cc: state.knowledge.pipeline.entity_update(cc, key, entity_id, body, revision)
    )


@router.get("/{entity_id}/relationships", response_model=list[FactOut])
@requires(Action.ENTITY_READ)
async def entity_relationships(
    entity_id: uuid.UUID,
    state: StateDep,
    principal: PrincipalDep,
    ws: WorkspaceDep,
    db: DbDep,
    subjects: SubjectsDep,
    direction: Literal["out", "in", "both"] = "both",
    type: Annotated[str | None, Query(max_length=64)] = None,
    as_of: Annotated[Instant | None, Query()] = None,
    known_at: Annotated[Instant | None, Query()] = None,
    include_history: bool = False,
    include: Annotated[str | None, Query(max_length=100, description="comma list: history, proposed, disputed, rejected")] = None,
) -> list[FactOut]:
    await state.knowledge.reads.entity_exists(
        db, ws, entity_id, subjects=subjects
    )  # a foreign, unknown or hidden id is a 404, never an empty list
    return await state.knowledge.reads.list_facts(
        db,
        ws,
        entity_id=entity_id,
        direction=direction,
        rel_type=type,
        as_of=as_of,
        known_at=known_at,
        include_history=include_history or parse_include(include)[0],
        can_see_sensitive=is_allowed(principal, Action.ENTITY_READ_SENSITIVE),
        subjects=subjects,
        include=parse_include(include)[1],
    )


@router.get("/{entity_id}/graph", response_model=GraphOut)
@requires(Action.ENTITY_READ)
async def entity_graph(
    entity_id: uuid.UUID,
    state: StateDep,
    ws: WorkspaceDep,
    db: DbDep,
    subjects: SubjectsDep,
    hops: Annotated[int, Query(ge=1, le=2)] = 1,
    as_of: Annotated[Instant | None, Query()] = None,
    known_at: Annotated[Instant | None, Query()] = None,
    include: Annotated[str | None, Query(max_length=100, description="comma list: historical, proposed, disputed, rejected")] = None,
    types: Annotated[str | None, Query(max_length=300, description="comma list of entity types to keep (the centre always stays)")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
) -> GraphOut:
    """The neighbourhood of an entity, read from Postgres (the graph store serves retrieval, not this view). Hops, nodes and the status filters are capped here."""
    history, extras = parse_include(include)
    return await state.knowledge.graph_reads.neighborhood(
        db,
        ws,
        entity_id,
        subjects=subjects,
        hops=hops,
        as_of=as_of,
        known_at=known_at,
        include=extras,
        history=history,
        types=[t.strip() for t in (types or "").split(",") if t.strip()],
        limit=limit,
    )


@router.get("/{entity_id}/mentions", response_model=list[MentionOut])
@requires(Action.ENTITY_READ)
async def entity_mentions(entity_id: uuid.UUID, state: StateDep, ws: WorkspaceDep, db: DbDep, subjects: SubjectsDep) -> list[MentionOut]:
    """The interactions that mention the entity (metadata only), with how each mention was resolved."""
    return await state.knowledge.graph_reads.mentions(db, ws, entity_id, subjects=subjects)


@router.get("/{entity_id}/attribute-claims", response_model=list[AttributeClaimOut])
@requires(Action.ENTITY_READ)
async def entity_attribute_claims(
    entity_id: uuid.UUID, state: StateDep, ws: WorkspaceDep, db: DbDep, subjects: SubjectsDep
) -> list[AttributeClaimOut]:
    """Attribute values a model proposed. They never change the entity: accepting one is a correction (a later phase)."""
    return await state.knowledge.graph_reads.attribute_claims(db, ws, entity_id, subjects=subjects)


@router.get("/{entity_id}/summary", response_model=DerivedSummaryOut)
@requires(Action.ENTITY_READ)
async def entity_summary(entity_id: uuid.UUID, state: StateDep, ws: WorkspaceDep, db: DbDep, subjects: SubjectsDep) -> DerivedSummaryOut:
    """Graphiti's summary of the entity: derived text, labelled `derived_unverified`, from the graph (best effort: a graph outage answers `graph_available: false`)."""
    await state.knowledge.reads.entity_exists(
        db, ws, entity_id, subjects=subjects
    )  # 404 first: a foreign or hidden id never reaches the graph
    try:
        node = await state.graph.scoped(ws).read_node(str(entity_id))
    except Exception:
        return DerivedSummaryOut(entity_id=entity_id, summary=None, graph_available=False)
    return DerivedSummaryOut(entity_id=entity_id, summary=(node.summary or None) if node else None)
