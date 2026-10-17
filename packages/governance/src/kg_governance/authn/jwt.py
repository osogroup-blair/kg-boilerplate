"""Short-lived JWTs issued after an API-key exchange (HS256, local signer). Every request still re-checks key and principal status (L5)."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Protocol

import jwt
from kg_common.secrets import SecretRing

ISSUER = "kg-platform"
AUDIENCE = "kg-api"
REQUIRED_CLAIMS = ["exp", "iat", "nbf", "sub", "aud", "iss", "jti", "workspace_id", "key_id", "kind"]


class TokenError(Exception):
    """Invalid token. The message is for logs/metrics only; callers see a generic 401."""


@dataclass(frozen=True)
class TokenClaims:
    principal_id: uuid.UUID
    workspace_id: uuid.UUID
    key_id: uuid.UUID
    kind: str
    jti: str
    expires_at: int


class JwksVerifier(Protocol):
    """OIDC path (ADR-020), documented but NOT wired in P1: verify an IdP-signed token against its JWKS and map its subject to a principal."""

    def verify(self, token: str) -> TokenClaims: ...


class LocalSigner:
    """HS256 over a key RING (P9 rotation): tokens are signed with the active kid and verified with whichever kid their header names, so adding a key, making it active and later
    removing the old one never invalidates a token that is still inside its lifetime. A bare secret is the one-key ring `local-1`, which is what every token before P9 carries."""

    def __init__(self, secret: str | SecretRing, ttl_seconds: int = 900):
        self._ring = secret if isinstance(secret, SecretRing) else SecretRing({"local-1": secret.encode()}, "local-1")
        self.ttl_seconds = ttl_seconds

    @property
    def kids(self) -> list[str]:
        return self._ring.versions

    @property
    def active_kid(self) -> str:
        return self._ring.active

    def issue(self, *, principal_id: uuid.UUID, workspace_id: uuid.UUID, key_id: uuid.UUID, kind: str, now: float | None = None) -> str:
        issued = int(now if now is not None else time.time())
        claims = {
            "iss": ISSUER,
            "aud": AUDIENCE,
            "sub": str(principal_id),
            "iat": issued,
            "nbf": issued,
            "exp": issued + self.ttl_seconds,
            "jti": str(uuid.uuid4()),
            "workspace_id": str(workspace_id),
            "kind": kind,
            "key_id": str(key_id),
        }
        return jwt.encode(claims, self._ring.current, algorithm="HS256", headers={"kid": self._ring.active})

    def verify(self, token: str) -> TokenClaims:
        try:
            kid = jwt.get_unverified_header(token).get("kid")
            secret = self._ring.get(kid) if isinstance(kid, str) else None
            if secret is None:
                raise TokenError("unknown_kid")
            c = jwt.decode(
                token, secret, algorithms=["HS256"], audience=AUDIENCE, issuer=ISSUER, leeway=0, options={"require": REQUIRED_CLAIMS}
            )
            return TokenClaims(
                uuid.UUID(c["sub"]), uuid.UUID(c["workspace_id"]), uuid.UUID(c["key_id"]), str(c["kind"]), str(c["jti"]), int(c["exp"])
            )
        except TokenError:
            raise
        except (jwt.PyJWTError, ValueError, KeyError, TypeError) as exc:
            raise TokenError(type(exc).__name__) from exc
