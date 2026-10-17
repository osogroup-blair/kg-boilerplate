"""OIDC for human users (phase-9 plan WP7, ADR-020 path, ADR-036 deferred human identity here).

The IdP signs an ID token; this module verifies it and nothing else:

* the algorithm is PINNED (`OIDC_ALGORITHMS`, RS256/ES256 by default). `none` and every HMAC algorithm are refused whatever the configuration says, so an attacker cannot make the
  IdP's public key act as an HMAC secret (alg confusion);
* `iss`, `aud`, `exp` and `sub` are required; `nbf` is honoured when present; the signing key is chosen by `kid` from the IdP's JWKS (cached; an unknown `kid` re-fetches the set
  once, at most every 30 s, which is how a key rollover works without a restart);
* the claims the platform uses are the subject, the groups (`OIDC_GROUPS_CLAIM`), the optional workspace slug (`OIDC_WORKSPACE_CLAIM`) and a display name.

Mapping those claims to a workspace, roles and a `user` principal is a database function (`auth.oidc_resolve`, migration 0009), so the rules sit with the data they read. A token
the mapping does not cover gets no principal at all: it is a generic 401, like every other credential failure."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

import httpx
import jwt
from jwt.algorithms import get_default_algorithms

FORBIDDEN_ALGORITHMS = frozenset({"none", "HS256", "HS384", "HS512"})
_JWKS_REFETCH_MIN_SECONDS = 30.0
_MAX_GROUPS = 200


class OidcError(Exception):
    """An OIDC token or the IdP could not be trusted. The message is for logs and metrics only; callers see a generic 401."""


@dataclass(frozen=True)
class OidcClaims:
    issuer: str
    subject: str
    groups: tuple[str, ...]
    workspace_slug: str | None
    display_name: str
    expires_at: int


@dataclass(frozen=True)
class OidcConfig:
    issuer: str
    audience: str
    algorithms: tuple[str, ...] = ("RS256", "ES256")
    groups_claim: str = "groups"
    workspace_claim: str = "kg_workspace"
    jwks_url: str = ""
    jwks_ttl_seconds: float = 600.0
    leeway_seconds: int = 10

    def __post_init__(self) -> None:
        bad = [a for a in self.algorithms if a in FORBIDDEN_ALGORITHMS or a not in get_default_algorithms()]
        if bad or not self.algorithms:
            raise ValueError(f"OIDC algorithms must be asymmetric signature algorithms, got {list(self.algorithms)}")
        if not self.issuer or not self.audience:
            raise ValueError("OIDC needs an issuer and an audience")


class OidcVerifier:
    def __init__(self, config: OidcConfig, *, http: httpx.AsyncClient | None = None, clock: Any = time.monotonic):
        self._cfg, self._clock = config, clock
        self._http = http or httpx.AsyncClient(timeout=5.0)
        self._owns_http = http is None
        self._keys: dict[str, dict[str, Any]] = {}
        self._fetched_at = float("-inf")
        self._last_forced = float("-inf")
        self._lock = asyncio.Lock()
        self._jwks_url = config.jwks_url
        self._doc: dict[str, Any] | None = None

    @property
    def config(self) -> OidcConfig:
        return self._cfg

    @property
    def http(self) -> httpx.AsyncClient:
        """The client used for the IdP's endpoints (discovery, JWKS and, from the login router, the token endpoint)."""
        return self._http

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    # ---- the IdP's keys --------------------------------------------------------------------------------------------------------------------
    async def discovery(self) -> dict[str, Any]:
        """The IdP's discovery document (cached for the process; its `issuer` must be the configured one)."""
        if self._doc is None:
            resp = await self._http.get(self._cfg.issuer.rstrip("/") + "/.well-known/openid-configuration")
            resp.raise_for_status()
            doc = resp.json()
            if str(doc.get("issuer", "")).rstrip("/") != self._cfg.issuer.rstrip("/"):
                raise OidcError("discovery_issuer_mismatch")
            self._doc = doc
        return self._doc

    async def _discover(self) -> str:
        if self._jwks_url:
            return self._jwks_url
        self._jwks_url = str((await self.discovery())["jwks_uri"])
        return self._jwks_url

    async def _refresh(self) -> None:
        resp = await self._http.get(await self._discover())
        resp.raise_for_status()
        keys: dict[str, dict[str, Any]] = {}
        for jwk in resp.json().get("keys", []):
            if jwk.get("use", "sig") == "sig" and jwk.get("kid"):
                keys[str(jwk["kid"])] = jwk
        if not keys:
            raise OidcError("empty_jwks")
        self._keys, self._fetched_at = keys, self._clock()

    async def _key_for(self, kid: str) -> Any:
        async with self._lock:
            now = self._clock()
            if not self._keys or now - self._fetched_at >= self._cfg.jwks_ttl_seconds:
                try:
                    await self._refresh()
                except Exception as exc:
                    if not self._keys:
                        raise OidcError("jwks_unavailable") from exc  # nothing cached to fall back on
            if kid not in self._keys and now - self._last_forced >= _JWKS_REFETCH_MIN_SECONDS:
                self._last_forced = now  # an unknown kid: the IdP may have rolled its keys
                try:
                    await self._refresh()
                except Exception as exc:
                    raise OidcError("jwks_unavailable") from exc
            jwk = self._keys.get(kid)
        if jwk is None:
            raise OidcError("unknown_kid")
        return jwt.PyJWK.from_dict(jwk).key

    # ---- the token -------------------------------------------------------------------------------------------------------------------------
    async def verify(self, token: str) -> OidcClaims:
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise OidcError("malformed") from exc
        alg, kid = header.get("alg"), header.get("kid")
        if alg not in self._cfg.algorithms:
            raise OidcError("algorithm_not_allowed")  # includes none / HS*: they are never in the configured set
        if not isinstance(kid, str) or not kid:
            raise OidcError("no_kid")
        key = await self._key_for(kid)
        try:
            claims = jwt.decode(
                token,
                key,
                algorithms=[alg],
                audience=self._cfg.audience,
                issuer=self._cfg.issuer,
                leeway=self._cfg.leeway_seconds,
                options={"require": ["exp", "iss", "aud", "sub"]},
            )
        except jwt.PyJWTError as exc:
            raise OidcError(type(exc).__name__) from exc
        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject or len(subject) > 300:
            raise OidcError("bad_subject")
        raw_groups = claims.get(self._cfg.groups_claim, [])
        if isinstance(raw_groups, str):
            raw_groups = [raw_groups]
        if not isinstance(raw_groups, list):
            raw_groups = []
        groups = tuple(g for g in raw_groups if isinstance(g, str) and 0 < len(g) <= 200)[:_MAX_GROUPS]
        slug = claims.get(self._cfg.workspace_claim)
        name = claims.get("name") or claims.get("preferred_username") or claims.get("email") or ""
        return OidcClaims(
            issuer=self._cfg.issuer,
            subject=subject,
            groups=groups,
            workspace_slug=slug if isinstance(slug, str) and 0 < len(slug) <= 100 else None,
            display_name=str(name)[:100],
            expires_at=int(claims["exp"]),
        )

    def looks_like_issuer(self, token: str) -> bool:
        """True when the (UNVERIFIED) `iss` of a JWT is this IdP. Only used to choose which verifier gets the token; verification itself decides everything."""
        try:
            iss = jwt.decode(token, options={"verify_signature": False}).get("iss")
        except jwt.PyJWTError:
            return False
        return isinstance(iss, str) and iss.rstrip("/") == self._cfg.issuer.rstrip("/")
