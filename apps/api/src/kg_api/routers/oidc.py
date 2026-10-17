"""OIDC for human users (phase-9 plan WP7). The console signs in with the authorization-code flow and PKCE; the browser holds the ID token in memory only, and the refresh token
lives in an httpOnly, SameSite=Strict cookie scoped to /v1/auth/oidc. Everything the API accepts afterwards is the ID token as a Bearer credential: it is verified against the
IdP's JWKS on every request and mapped to a `user` principal by the database (migration 0009), so a revoked principal or a changed group mapping takes effect on the next call.

The group -> role mapping (`/v1/oidc-group-mappings`) is admin-managed. API keys stay for agents, apps, development and audited break-glass."""

from __future__ import annotations

import uuid
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Cookie, Header, Request, Response
from kg_common.errors import DependencyUnavailable, NotFound, Unauthenticated, ValidationFailed
from kg_common.ids import uuid7
from kg_governance.audit import AuditMetadata
from kg_governance.authn.oidc import OidcError
from kg_governance.authz.policy import Action, public, requires
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text

from kg_api.deps import AuditDep, DbDep, PrincipalDep, StateDep, WorkspaceDep
from kg_api.http import IdempotencyKeyHeader

router = APIRouter(prefix="/v1", tags=["oidc"])
mappings_router = APIRouter(prefix="/v1/oidc-group-mappings", tags=["oidc"])
COOKIE = "kg_oidc_refresh"
COOKIE_PATH = "/v1/auth/oidc"
NO_STORE = {"Cache-Control": "no-store"}
ROLE_NAMES = ["workspace_admin", "operator", "reviewer", "app_writer", "agent", "reader"]


class OidcConfigOut(BaseModel):
    enabled: bool
    issuer: str | None = None
    client_id: str | None = None
    authorization_endpoint: str | None = None
    scopes: list[str] = []
    redirect_uris: list[str] = []


class CallbackIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(min_length=1, max_length=2048)
    code_verifier: str = Field(min_length=43, max_length=128)
    redirect_uri: str = Field(min_length=1, max_length=500)


class SessionOut(BaseModel):
    id_token: str
    token_type: str = "Bearer"  # noqa: S105 (OAuth token type, not a secret)
    expires_in: int
    principal_id: uuid.UUID
    handle: str
    roles: list[str]


class MappingIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    group_name: str = Field(min_length=1, max_length=200)
    roles: list[str] = Field(min_length=1, max_length=6)
    issuer: str | None = Field(default=None, max_length=300)


class MappingOut(BaseModel):
    id: uuid.UUID
    issuer: str
    group_name: str
    roles: list[str]
    created_at: Any = None


def _enabled(state: StateDep) -> None:
    if state.authenticator.oidc is None:
        raise NotFound("OIDC is not enabled")


async def _token_request(state: StateDep, form: dict[str, str]) -> dict[str, Any]:
    oidc = state.authenticator.oidc
    assert oidc is not None
    s = state.settings
    try:
        endpoint = str((await oidc.discovery())["token_endpoint"])
        data = {**form, "client_id": s.oidc_client_id}
        secret = s.oidc_client_secret.get_secret_value()
        if secret:
            data["client_secret"] = secret
        resp = await oidc.http.post(endpoint, data=data, headers={"accept": "application/json"})
    except (httpx.HTTPError, OidcError, KeyError) as exc:
        raise DependencyUnavailable("The identity provider is unavailable") from exc
    if resp.status_code != 200:
        raise Unauthenticated("Authentication failed")  # a bad or replayed code, an expired refresh token: one generic answer
    body = resp.json()
    if not isinstance(body, dict) or not isinstance(body.get("id_token"), str):
        raise Unauthenticated("Authentication failed")
    return body


def _set_refresh(request: Request, response: Response, state: StateDep, refresh_token: str | None) -> None:
    if not refresh_token:
        return
    secure = state.settings.is_production or request.url.scheme == "https"
    response.set_cookie(COOKIE, refresh_token, httponly=True, secure=secure, samesite="strict", path=COOKIE_PATH, max_age=8 * 3600)


async def _session(state: StateDep, body: dict[str, Any]) -> SessionOut:
    id_token = str(body["id_token"])
    principal = await state.authenticator.authenticate(f"Bearer {id_token}")  # verifies, maps, provisions, re-reads status
    claims = await state.authenticator.oidc.verify(id_token)  # type: ignore[union-attr]
    import time

    return SessionOut(
        id_token=id_token,
        expires_in=max(1, claims.expires_at - int(time.time())),
        principal_id=principal.principal_id,
        handle=principal.handle,
        roles=sorted(r.value for r in principal.roles),
    )


@router.get("/auth/oidc/config", response_model=OidcConfigOut)
@public("the console needs the IdP's public endpoints and its own client id before anyone can sign in")
async def oidc_config(state: StateDep) -> OidcConfigOut:
    oidc = state.authenticator.oidc
    if oidc is None:
        return OidcConfigOut(enabled=False)
    try:
        doc = await oidc.discovery()
    except Exception as exc:
        raise DependencyUnavailable("The identity provider is unavailable") from exc
    return OidcConfigOut(
        enabled=True,
        issuer=oidc.config.issuer,
        client_id=state.settings.oidc_client_id,
        authorization_endpoint=str(doc.get("authorization_endpoint", "")),
        scopes=["openid", "profile", "email", "groups"],
        redirect_uris=list(state.settings.oidc_redirect_uris),
    )


@router.post("/auth/oidc/callback", response_model=SessionOut)
@public("the authorization code from the IdP (with its PKCE verifier) is the credential being exchanged")
async def oidc_callback(body: CallbackIn, request: Request, response: Response, state: StateDep) -> SessionOut:
    _enabled(state)
    if body.redirect_uri not in state.settings.oidc_redirect_uris:
        raise ValidationFailed("redirect_uri is not registered", details={"field": "redirect_uri"})
    tokens = await _token_request(
        state,
        {"grant_type": "authorization_code", "code": body.code, "code_verifier": body.code_verifier, "redirect_uri": body.redirect_uri},
    )
    out = await _session(state, tokens)
    _set_refresh(request, response, state, tokens.get("refresh_token"))
    response.headers.update(NO_STORE)
    return out


@router.post("/auth/oidc/refresh", response_model=SessionOut)
@public("the httpOnly refresh cookie is the credential; a custom header and SameSite=Strict keep a cross-site page from using it")
async def oidc_refresh(
    request: Request,
    response: Response,
    state: StateDep,
    refresh_cookie: Annotated[str | None, Cookie(alias=COOKIE)] = None,
    x_requested_with: Annotated[str | None, Header()] = None,
) -> SessionOut:
    _enabled(state)
    if x_requested_with != "kg-console" or not refresh_cookie:
        raise Unauthenticated("Authentication failed")
    tokens = await _token_request(state, {"grant_type": "refresh_token", "refresh_token": refresh_cookie})
    out = await _session(state, tokens)
    _set_refresh(request, response, state, tokens.get("refresh_token"))
    response.headers.update(NO_STORE)
    return out


@router.post("/auth/oidc/logout", status_code=204)
@public("clears the caller's own refresh cookie; there is nothing to protect")
async def oidc_logout(response: Response) -> None:
    response.delete_cookie(COOKIE, path=COOKIE_PATH)


# ---- the group -> role mapping (admin) ----------------------------------------------------------------------------------------------------------------
@mappings_router.get("", response_model=list[MappingOut])
@requires(Action.OIDC_MANAGE)
async def list_mappings(ws: WorkspaceDep, db: DbDep) -> list[MappingOut]:
    rows = (
        await db.execute(
            text(
                "SELECT id, issuer, group_name, roles, created_at FROM oidc_group_mappings WHERE workspace_id = :ws ORDER BY group_name, issuer"
            ),
            {"ws": ws.workspace_id},
        )
    ).all()
    return [MappingOut(**dict(r._mapping)) for r in rows]


@mappings_router.post("", response_model=MappingOut, status_code=201)
@requires(Action.OIDC_MANAGE)
async def create_mapping(
    body: MappingIn, state: StateDep, principal: PrincipalDep, db: DbDep, audit: AuditDep, idempotency_key: IdempotencyKeyHeader = None
) -> MappingOut:
    bad = [r for r in body.roles if r not in ROLE_NAMES]
    if bad:
        raise ValidationFailed("unknown role", details={"field": "roles"})
    issuer = body.issuer or state.settings.oidc_issuer
    if not issuer:
        raise ValidationFailed("an issuer is required (none is configured)", details={"field": "issuer"})
    mid = uuid7()
    existing = (
        await db.execute(
            text("SELECT id FROM oidc_group_mappings WHERE workspace_id = :ws AND issuer = :iss AND group_name = :g"),
            {"ws": principal.workspace_id, "iss": issuer, "g": body.group_name},
        )
    ).first()
    if existing is not None:
        await db.execute(
            text("UPDATE oidc_group_mappings SET roles = :roles WHERE workspace_id = :ws AND id = :id"),
            {"roles": sorted(set(body.roles)), "ws": principal.workspace_id, "id": existing.id},
        )
        mid = existing.id
    else:
        await db.execute(
            text(
                "INSERT INTO oidc_group_mappings (id, workspace_id, issuer, group_name, roles, created_by) VALUES (:id, :ws, :iss, :g, :roles, :by)"
            ),
            {
                "id": mid,
                "ws": principal.workspace_id,
                "iss": issuer,
                "g": body.group_name,
                "roles": sorted(set(body.roles)),
                "by": principal.principal_id,
            },
        )
    await audit.record("oidc.mapping_set", resource_type="oidc_group_mapping", resource_id=mid, metadata=AuditMetadata(mapping_id=mid))
    row = (
        await db.execute(text("SELECT id, issuer, group_name, roles, created_at FROM oidc_group_mappings WHERE id = :id"), {"id": mid})
    ).one()
    return MappingOut(**dict(row._mapping))


@mappings_router.delete("/{mapping_id}", status_code=204)
@requires(Action.OIDC_MANAGE)
async def delete_mapping(mapping_id: uuid.UUID, principal: PrincipalDep, db: DbDep, audit: AuditDep) -> None:
    res = await db.execute(
        text("DELETE FROM oidc_group_mappings WHERE workspace_id = :ws AND id = :id"), {"ws": principal.workspace_id, "id": mapping_id}
    )
    if res.rowcount == 0:
        raise NotFound()
    await audit.record(
        "oidc.mapping_removed", resource_type="oidc_group_mapping", resource_id=mapping_id, metadata=AuditMetadata(mapping_id=mapping_id)
    )
