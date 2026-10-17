"""Workspace settings and the privacy audit view (phase 8; ADR-047, §5.8). Admin only: settings change what the platform keeps and sends, the audit shows who looked at what."""

from __future__ import annotations

import csv
import datetime as dt
import io
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse, PlainTextResponse
from kg_domain.envelope import Page
from kg_governance.authz.policy import Action, requires
from kg_governance.privacy import CATEGORIES, EgressRow, PrivacyItem, egress_summary, timeline
from kg_governance.settings import SettingsOut, SettingsVersionOut, WorkspaceSettings

from kg_api.deps import AuditDep, DbDep, PrincipalDep, StateDep, WorkspaceDep
from kg_api.http import IdempotencyKeyHeader, IfMatchHeader, etag, parse_if_match, require_key

router = APIRouter(prefix="/v1", tags=["governance"])
NO_STORE = {"Cache-Control": "no-store"}


@router.get("/workspaces/current/settings", response_model=SettingsOut)
@requires(Action.WORKSPACE_ADMIN)
async def get_settings(state: StateDep, ws: WorkspaceDep, db: DbDep) -> JSONResponse:
    out = await state.settings_service.get(db, ws.workspace_id)
    return JSONResponse(out.model_dump(mode="json"), headers={**NO_STORE, "ETag": etag(out.version)})


@router.get("/workspaces/current/settings/history", response_model=list[SettingsVersionOut])
@requires(Action.WORKSPACE_ADMIN)
async def settings_history(state: StateDep, ws: WorkspaceDep, db: DbDep) -> JSONResponse:
    rows = await state.settings_service.history(db, ws.workspace_id)
    return JSONResponse([r.model_dump(mode="json") for r in rows], headers=NO_STORE)


@router.put(
    "/workspaces/current/settings",
    response_model=SettingsOut,
    responses={412: {"description": "If-Match is not the current version"}, 428: {"description": "If-Match is required"}},
)
@requires(Action.WORKSPACE_ADMIN)
async def put_settings(
    body: WorkspaceSettings,
    state: StateDep,
    principal: PrincipalDep,
    ws: WorkspaceDep,
    db: DbDep,
    audit: AuditDep,
    idempotency_key: IdempotencyKeyHeader = None,
    if_match: IfMatchHeader = None,
) -> JSONResponse:
    """A new version of the settings (the previous one stays in the history). The egress list can only add classifications to the platform's own deny list."""
    out, replayed = await state.settings_service.put(
        db, ws.workspace_id, principal, body, if_match=parse_if_match(if_match), key=require_key(idempotency_key), audit=audit
    )
    if state.router.narrowing is not None:
        state.router.narrowing.invalidate(ws.workspace_id)  # this process sees the change at once; the others within their cache window
    headers = {**NO_STORE, "ETag": etag(out.version)}
    if replayed:
        headers["Idempotent-Replay"] = "true"
    return JSONResponse(out.model_dump(mode="json"), headers=headers)


@router.get("/governance/privacy-audit")
@requires(Action.AUDIT_READ)
async def privacy_audit(
    state: StateDep,
    ws: WorkspaceDep,
    db: DbDep,
    category: Annotated[list[str] | None, Query(description=f"any of {sorted(CATEGORIES)}")] = None,
    since: dt.datetime | None = None,
    until: dt.datetime | None = None,
    actor: uuid.UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    cursor: str | None = None,
    format: Annotated[str, Query(pattern="^(json|csv)$")] = "json",
) -> Any:
    """The privacy timeline: reveals, evidence reads, private memory reads, debug reads, act-as builds, deletions, legal holds, grants, authoritative overrides, settings and
    simulations, plus the model calls by purpose, classification and outcome (egress decisions). Ids and codes only. `format=csv` exports the page."""
    unknown = [c for c in (category or []) if c not in CATEGORIES]
    if unknown:
        from kg_common.errors import ValidationFailed

        raise ValidationFailed("unknown category", details={"fields": ["category"]})
    page = await timeline(db, ws.workspace_id, categories=category, since=since, until=until, actor=actor, limit=limit, cursor=cursor)
    if format == "csv":
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["at", "category", "action", "outcome", "actor_principal_id", "resource_type", "resource_id", "metadata_codes"])
        for i in page.items:
            w.writerow(
                [
                    i.at.isoformat(),
                    i.category,
                    i.action,
                    i.outcome,
                    i.actor_principal_id or "",
                    i.resource_type or "",
                    i.resource_id or "",
                    ";".join(f"{k}={v}" for k, v in sorted(i.metadata.items())),
                ]
            )
        return PlainTextResponse(buf.getvalue(), media_type="text/csv", headers=NO_STORE)
    egress: list[EgressRow] = await egress_summary(db, ws.workspace_id, since=since, until=until) if not cursor else []
    return JSONResponse(
        {
            "items": [i.model_dump(mode="json") for i in page.items],
            "next_cursor": page.next_cursor,
            "egress": [e.model_dump() for e in egress],
        },
        headers=NO_STORE,
    )


_ = (Page, PrivacyItem)
