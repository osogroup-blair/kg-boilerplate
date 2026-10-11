from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Header, Query, Request
from kg_common.errors import Unauthenticated
from kg_db.session import tenant_session
from kg_domain.api import AuditEventOut, MeOut, PrincipalOut, PrincipalSummary, TokenOut, WorkspaceOut
from kg_domain.envelope import Page
from kg_domain.principal import AuthMethod, WorkspaceContext
from kg_governance.audit import AuditMetadata, AuditWriter
from kg_governance.authz.policy import Action, authenticated, public, requires
from kg_governance.repositories import reads
from kg_obs import context

from kg_api.deps import DbDep, PrincipalDep, StateDep, WorkspaceDep

router = APIRouter(tags=["identity"])
LimitQ = Annotated[int, Query(ge=1, le=100)]
CursorQ = Annotated[str | None, Query()]


@router.post("/v1/auth/token", response_model=TokenOut)
@public("the API key in the Authorization header is the credential being exchanged")
async def issue_token(request: Request, state: StateDep, authorization: Annotated[str | None, Header()] = None) -> TokenOut:
    principal = await state.authenticator.authenticate(authorization)
    if principal.auth_method is not AuthMethod.API_KEY or principal.key_id is None:
        raise Unauthenticated("Authentication failed")  # a token cannot be exchanged for another token
    signer = state.authenticator.signer
    token = signer.issue(
        principal_id=principal.principal_id, workspace_id=principal.workspace_id, key_id=principal.key_id, kind=principal.kind.value
    )
    async with tenant_session(state.engine, WorkspaceContext.of(principal), principal.principal_id) as conn:
        await AuditWriter(conn, principal, request_id=context.request_id_var.get(), correlation_id=context.correlation_id_var.get()).record(
            "auth.token_issued",
            resource_type="api_key",
            resource_id=principal.key_id,
            metadata=AuditMetadata(key_id=principal.key_id, ttl_seconds=signer.ttl_seconds),
        )
    return TokenOut(access_token=token, expires_in=signer.ttl_seconds)


@router.get("/v1/me", response_model=MeOut)
@authenticated("any authenticated principal may read its own identity")
async def me(principal: PrincipalDep, ws: WorkspaceDep, db: DbDep) -> MeOut:
    return MeOut(
        principal=PrincipalSummary(id=principal.principal_id, handle=principal.handle, kind=principal.kind, roles=sorted(principal.roles)),
        workspace=await reads.get_workspace(db, ws),
    )


@router.get("/v1/workspaces/current", response_model=WorkspaceOut)
@requires(Action.WORKSPACE_READ)
async def get_current_workspace(ws: WorkspaceDep, db: DbDep) -> WorkspaceOut:
    return await reads.get_workspace(db, ws)


@router.get("/v1/workspaces/{workspace_id}", response_model=WorkspaceOut)
@requires(Action.WORKSPACE_READ)
async def get_workspace(workspace_id: uuid.UUID, ws: WorkspaceDep, db: DbDep) -> WorkspaceOut:
    return await reads.get_workspace(db, ws, workspace_id)  # any id but the caller's own is NotFound


@router.get("/v1/principals", response_model=Page[PrincipalOut])
@requires(Action.PRINCIPAL_READ)
async def list_principals(ws: WorkspaceDep, db: DbDep, limit: LimitQ = 50, cursor: CursorQ = None) -> Page[PrincipalOut]:
    return await reads.list_principals(db, ws, limit=limit, cursor=cursor)


@router.get("/v1/principals/{principal_id}", response_model=PrincipalOut)
@requires(Action.PRINCIPAL_READ)
async def get_principal(principal_id: uuid.UUID, ws: WorkspaceDep, db: DbDep) -> PrincipalOut:
    return await reads.get_principal(db, ws, principal_id)


@router.get("/v1/audit", response_model=Page[AuditEventOut])
@requires(Action.AUDIT_READ)
async def list_audit(
    ws: WorkspaceDep,
    db: DbDep,
    limit: LimitQ = 50,
    cursor: CursorQ = None,
    action: Annotated[str | None, Query(max_length=64, pattern=r"^[a-z_]+\.[a-z_]+$")] = None,
) -> Page[AuditEventOut]:
    return await reads.list_audit(db, ws, limit=limit, cursor=cursor, action=action)
