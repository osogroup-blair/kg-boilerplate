"""Request dependencies. `guard` is applied to every route: deny by default, then authenticate and authorize per the route's policy."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Annotated, cast

from fastapi import Depends, Request
from kg_common.errors import Forbidden, PlatformError, Unauthenticated
from kg_db.session import tenant_session
from kg_domain.principal import Principal, WorkspaceContext
from kg_governance.audit import AuditMetadata, AuditReason, AuditWriter
from kg_governance.authz.policy import is_allowed, policy_of
from kg_governance.grants import ScopeGrants, SubjectFilter
from kg_obs import context, metrics
from kg_obs.logging import get_logger
from sqlalchemy.ext.asyncio import AsyncConnection

from kg_api.state import AppState

log = get_logger("kg.http.guard")


def get_state(request: Request) -> AppState:
    return cast(AppState, request.app.state.kg)


StateDep = Annotated[AppState, Depends(get_state)]


# The principal lives in the per-request ASGI scope, NOT request.state: some servers/test transports hand every request the same
# `scope["state"]` dict, which would leak one caller's identity into the next request.
_PRINCIPAL_KEY = "kg.principal"


async def authenticate(request: Request) -> Principal:
    cached = request.scope.get(_PRINCIPAL_KEY)
    if cached is not None:
        return cast(Principal, cached)
    state = get_state(request)
    principal = await state.authenticator.authenticate(request.headers.get("authorization"))
    request.scope[_PRINCIPAL_KEY] = principal
    context.bind(
        principal_id=str(principal.principal_id),
        workspace_hash=context.hmac_hash(str(principal.workspace_id), state.settings.telemetry_hmac_secret.get_secret_value()),
    )
    return principal


async def guard(request: Request) -> None:
    """Deny by default. L4: the workspace is whatever the credential says; X-Workspace-Id headers and workspace query params are never read."""
    route = request.scope.get("route")
    policy = policy_of(getattr(route, "endpoint", None))
    if policy is None:
        raise PlatformError("route has no authorization policy")  # also caught at startup; this is the runtime backstop
    if policy.kind == "public":
        return
    principal = await authenticate(request)
    await get_state(request).quotas.admit_request(principal)  # P7: the principal's requests-per-minute quota (429 quota_exceeded)
    if "x-act-as-principal" in request.headers and request.scope.get("path") != "/v1/context/build":
        log.warning("act_as_header_ignored")  # only the context build honours it (ADR-035); anywhere else it changes nothing
    if policy.kind == "requires" and policy.action is not None and not is_allowed(principal, policy.action):
        metrics.authz_denials_total.labels(action=policy.action.value).inc()
        state = get_state(request)
        try:
            async with tenant_session(state.engine, WorkspaceContext.of(principal), principal.principal_id) as conn:
                await AuditWriter(
                    conn, principal, request_id=context.request_id_var.get(), correlation_id=context.correlation_id_var.get()
                ).record(
                    "authz.denied",
                    outcome="denied",
                    resource_type="route",
                    metadata=AuditMetadata(action_name=policy.action.value, reason=AuditReason.MISSING_ROLE, status_code=403),
                )
        except Exception:
            log.warning("audit_denial_failed")
        raise Forbidden("Action not permitted")


async def current_principal(request: Request) -> Principal:
    principal = request.scope.get(_PRINCIPAL_KEY)
    if principal is None:
        raise Unauthenticated("Authentication failed")
    return cast(Principal, principal)


PrincipalDep = Annotated[Principal, Depends(current_principal)]


async def workspace_context(principal: PrincipalDep) -> WorkspaceContext:
    return WorkspaceContext.of(principal)


WorkspaceDep = Annotated[WorkspaceContext, Depends(workspace_context)]


async def tenant_db(request: Request, principal: PrincipalDep) -> AsyncIterator[AsyncConnection]:
    """One transaction per request with app.workspace_id set first (L2). `scope="function"` below commits before the response is sent."""
    state = get_state(request)
    async with tenant_session(state.engine, WorkspaceContext.of(principal), principal.principal_id) as conn:
        yield conn


DbDep = Annotated[AsyncConnection, Depends(tenant_db, scope="function")]


async def audit_writer(conn: DbDep, principal: PrincipalDep) -> AuditWriter:
    return AuditWriter(conn, principal, request_id=context.request_id_var.get(), correlation_id=context.correlation_id_var.get())


AuditDep = Annotated[AuditWriter, Depends(audit_writer)]


async def subject_filter(request: Request, conn: DbDep, principal: PrincipalDep) -> SubjectFilter:
    """The caller's entity/fact filter under the active ontology (P7, ADR-037): built once per request and passed to every `kg_knowledge` read, where it is part of the SQL."""
    state = get_state(request)
    grants = await ScopeGrants.for_principal(conn, principal)
    return await state.knowledge.reads.subject_filter(conn, principal.workspace_id, grants)


SubjectsDep = Annotated[SubjectFilter, Depends(subject_filter)]


def parse_uuid(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(value)
    except ValueError:
        return None
