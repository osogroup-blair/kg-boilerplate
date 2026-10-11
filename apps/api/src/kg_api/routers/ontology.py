"""Ontology registry API (ONT-01, ONT-02, ONT-05 partial). Mutations need an Idempotency-Key; edits to a draft need If-Match."""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from kg_domain.envelope import Page
from kg_governance.authz.policy import Action, requires
from kg_knowledge.models import (
    ActivateIn,
    ActiveOntologyOut,
    DraftCreateIn,
    DraftReplaceIn,
    OntologyVersionOut,
    OntologyVersionSummary,
    PublishIn,
    RuleCatalogOut,
)
from kg_ontology import DiffResult, ValidationReport
from pydantic import BaseModel

from kg_api.deps import AuditDep, DbDep, PrincipalDep, StateDep, WorkspaceDep
from kg_api.http import IdempotencyKeyHeader, IfMatchHeader, etag, parse_if_match, require_key, run_idempotent

router = APIRouter(prefix="/v1/ontologies", tags=["ontology"])
LimitQ = Annotated[int, Query(ge=1, le=100)]
CONFLICT: dict[int | str, dict[str, Any]] = {
    409: {"description": "Conflict (immutable_version, version_exists, already_active, ...)"},
    412: {"description": "If-Match does not match"},
    428: {"description": "If-Match required"},
}


class DeletedOut(BaseModel):
    id: uuid.UUID
    deleted: bool = True


def _versioned(out: OntologyVersionOut, status: int = 200) -> tuple[int, OntologyVersionOut, dict[str, str]]:
    return status, out, {"ETag": etag(out.revision)}


@router.get("/versions", response_model=Page[OntologyVersionSummary])
@requires(Action.ONTOLOGY_READ)
async def list_versions(
    state: StateDep, ws: WorkspaceDep, db: DbDep, limit: LimitQ = 50, cursor: str | None = None
) -> Page[OntologyVersionSummary]:
    return await state.knowledge.ontology.list_versions(db, ws, limit=limit, cursor=cursor)


@router.post("/versions", response_model=OntologyVersionOut, status_code=201, responses=CONFLICT)
@requires(Action.ONTOLOGY_WRITE)
async def create_draft(
    body: DraftCreateIn,
    state: StateDep,
    principal: PrincipalDep,
    ws: WorkspaceDep,
    db: DbDep,
    audit: AuditDep,
    idempotency_key: IdempotencyKeyHeader = None,
) -> JSONResponse:
    key = require_key(idempotency_key)

    async def produce() -> tuple[int, OntologyVersionOut, dict[str, str]]:
        out = await state.knowledge.ontology.create_draft(
            db, ws, principal, audit, content=body.content, fmt=body.format or "yaml", copy_of=body.copy_of
        )
        return _versioned(out, 201)

    return await run_idempotent(db, ws.workspace_id, key, "POST /v1/ontologies/versions", body.model_dump(mode="json"), produce)


@router.get("/versions/{version_id}", response_model=OntologyVersionOut)
@requires(Action.ONTOLOGY_READ)
async def get_version(version_id: uuid.UUID, state: StateDep, ws: WorkspaceDep, db: DbDep) -> JSONResponse:
    out = await state.knowledge.ontology.get_version(db, ws, version_id)
    return JSONResponse(out.model_dump(mode="json"), headers={"ETag": etag(out.revision)})


@router.put("/versions/{version_id}", response_model=OntologyVersionOut, responses=CONFLICT)
@requires(Action.ONTOLOGY_WRITE)
async def replace_draft(
    version_id: uuid.UUID,
    body: DraftReplaceIn,
    state: StateDep,
    ws: WorkspaceDep,
    db: DbDep,
    audit: AuditDep,
    idempotency_key: IdempotencyKeyHeader = None,
    if_match: IfMatchHeader = None,
) -> JSONResponse:
    key = require_key(idempotency_key)
    revision = parse_if_match(if_match)

    async def produce() -> tuple[int, OntologyVersionOut, dict[str, str]]:
        out = await state.knowledge.ontology.replace_draft(
            db, ws, audit, version_id, content=body.content, fmt=body.format or "yaml", expected_revision=revision
        )
        return _versioned(out)

    return await run_idempotent(
        db, ws.workspace_id, key, f"PUT /v1/ontologies/versions/{version_id}", [body.model_dump(mode="json"), revision], produce
    )


@router.delete("/versions/{version_id}", response_model=DeletedOut, responses=CONFLICT)
@requires(Action.ONTOLOGY_WRITE)
async def delete_draft(
    version_id: uuid.UUID, state: StateDep, ws: WorkspaceDep, db: DbDep, audit: AuditDep, idempotency_key: IdempotencyKeyHeader = None
) -> JSONResponse:
    key = require_key(idempotency_key)

    async def produce() -> tuple[int, DeletedOut, dict[str, str]]:
        await state.knowledge.ontology.delete_draft(db, ws, audit, version_id)
        return 200, DeletedOut(id=version_id), {}

    return await run_idempotent(db, ws.workspace_id, key, f"DELETE /v1/ontologies/versions/{version_id}", None, produce)


@router.post("/versions/{version_id}/validate", response_model=ValidationReport)
@requires(Action.ONTOLOGY_READ)
async def validate_version(version_id: uuid.UUID, state: StateDep, ws: WorkspaceDep, db: DbDep) -> ValidationReport:
    """Read-only: recomputes the validation report (including the Graphiti compile check and the rule tests)."""
    return await state.knowledge.ontology.validate(db, ws, version_id)


@router.get("/versions/{version_id}/diff", response_model=DiffResult)
@requires(Action.ONTOLOGY_READ)
async def diff_version(version_id: uuid.UUID, state: StateDep, ws: WorkspaceDep, db: DbDep, against: str = "active") -> DiffResult:
    return await state.knowledge.ontology.diff(db, ws, version_id, against)


@router.post(
    "/versions/{version_id}/publish", response_model=OntologyVersionOut, responses={**CONFLICT, 422: {"description": "ontology_invalid"}}
)
@requires(Action.ONTOLOGY_PUBLISH)
async def publish_version(
    version_id: uuid.UUID,
    body: PublishIn,
    state: StateDep,
    principal: PrincipalDep,
    ws: WorkspaceDep,
    db: DbDep,
    audit: AuditDep,
    idempotency_key: IdempotencyKeyHeader = None,
) -> JSONResponse:
    key = require_key(idempotency_key)

    async def produce() -> tuple[int, OntologyVersionOut, dict[str, str]]:
        out = await state.knowledge.ontology.publish(db, ws, principal, audit, version_id, notes=body.notes, activate=body.activate)
        return _versioned(out)

    return await run_idempotent(
        db, ws.workspace_id, key, f"POST /v1/ontologies/versions/{version_id}/publish", body.model_dump(mode="json"), produce
    )


@router.post("/versions/{version_id}/retire", response_model=OntologyVersionOut, responses=CONFLICT)
@requires(Action.ONTOLOGY_PUBLISH)
async def retire_version(
    version_id: uuid.UUID, state: StateDep, ws: WorkspaceDep, db: DbDep, audit: AuditDep, idempotency_key: IdempotencyKeyHeader = None
) -> JSONResponse:
    key = require_key(idempotency_key)

    async def produce() -> tuple[int, OntologyVersionOut, dict[str, str]]:
        return _versioned(await state.knowledge.ontology.retire(db, ws, audit, version_id))

    return await run_idempotent(db, ws.workspace_id, key, f"POST /v1/ontologies/versions/{version_id}/retire", None, produce)


@router.get("/active", response_model=ActiveOntologyOut)
@requires(Action.ONTOLOGY_READ)
async def get_active(state: StateDep, ws: WorkspaceDep, db: DbDep) -> ActiveOntologyOut:
    return await state.knowledge.ontology.active(db, ws)


@router.post("/active", response_model=ActiveOntologyOut, responses=CONFLICT)
@requires(Action.ONTOLOGY_PUBLISH)
async def activate_version(
    body: ActivateIn,
    state: StateDep,
    principal: PrincipalDep,
    ws: WorkspaceDep,
    db: DbDep,
    audit: AuditDep,
    idempotency_key: IdempotencyKeyHeader = None,
) -> JSONResponse:
    """Rollback (ONT-02): activate an older published version. Appends an activation; no version is changed."""
    key = require_key(idempotency_key)

    async def produce() -> tuple[int, ActiveOntologyOut, dict[str, str]]:
        return 200, await state.knowledge.ontology.activate(db, ws, principal, audit, body.version_id, body.reason_code), {}

    return await run_idempotent(db, ws.workspace_id, key, "POST /v1/ontologies/active", body.model_dump(mode="json"), produce)


@router.get("/rule-catalog", response_model=RuleCatalogOut)
@requires(Action.ONTOLOGY_READ)
async def rule_catalog(state: StateDep) -> RuleCatalogOut:
    return state.knowledge.ontology.rule_catalog()
