"""Dev auth prototype (DEC-03): platform-issued API keys + optional short-lived JWT from a local signer. Disposable; P1 rebuilds in packages/.

Key format: kgk_<8-char public prefix>_<43-char secret>. Only SHA-256(pepper || key) is stored; the prefix is stored and displayed.
Principals are workspace-bound: the workspace comes from the key record (or token claim), never from the request body or a header.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

import jwt

ISSUER, AUDIENCE = "kg-platform-dev", "kg-api"


@dataclass(frozen=True)
class Principal:
    principal_id: str
    workspace_id: str
    kind: str                                   # user | app | agent
    scopes: tuple[str, ...]


@dataclass
class KeyRecord:
    key_id: str
    prefix: str
    key_hash: str
    principal: Principal
    revoked_at: Optional[float] = None
    expires_at: Optional[float] = None


class AuthError(Exception):
    pass


@dataclass
class KeyStore:
    pepper: bytes
    records: dict[str, KeyRecord] = field(default_factory=dict)          # by prefix (the lookup index; in Postgres: unique index on prefix)

    def _hash(self, key: str) -> str:
        return hashlib.sha256(self.pepper + key.encode()).hexdigest()

    def mint(self, principal: Principal, ttl_s: Optional[float] = None) -> tuple[str, KeyRecord]:
        prefix = secrets.token_urlsafe(6)[:8].replace("_", "x")
        key = f"kgk_{prefix}_{secrets.token_urlsafe(32)}"
        rec = KeyRecord(str(uuid.uuid4()), prefix, self._hash(key), principal, expires_at=time.time() + ttl_s if ttl_s else None)
        self.records[prefix] = rec
        return key, rec                                                  # the plaintext key is shown ONCE

    def authenticate(self, presented: str, now: Optional[float] = None) -> Principal:
        now = now or time.time()
        try:
            scheme, prefix, _ = presented.split("_", 2)
        except ValueError:
            raise AuthError("malformed key")
        rec = self.records.get(prefix)
        # always hash + compare, even for an unknown prefix, so timing does not reveal which prefixes exist
        candidate = self._hash(presented)
        expected = rec.key_hash if rec else self._hash("kgk_none_none")
        ok = hmac.compare_digest(candidate, expected)
        if scheme != "kgk" or rec is None or not ok:
            raise AuthError("invalid key")
        if rec.revoked_at is not None and rec.revoked_at <= now:
            raise AuthError("revoked")
        if rec.expires_at is not None and rec.expires_at <= now:
            raise AuthError("expired")
        return rec.principal

    def revoke(self, prefix: str) -> None:
        self.records[prefix].revoked_at = time.time()                    # immediate: no cache in this layer (AGT-05)


class LocalSigner:
    """Issues short-lived JWTs after an API-key exchange. HS256 for local dev; the same verify() works for RS256/ES256 via an IdP's JWKS."""

    def __init__(self, secret: str, ttl_s: int = 900):
        self.secret, self.ttl_s = secret, ttl_s

    def issue(self, p: Principal, now: Optional[float] = None) -> str:
        now = int(now or time.time())
        claims = {"iss": ISSUER, "aud": AUDIENCE, "sub": p.principal_id, "iat": now, "nbf": now, "exp": now + self.ttl_s, "jti": str(uuid.uuid4()),
                  "workspace_id": p.workspace_id, "kind": p.kind, "scopes": list(p.scopes)}
        return jwt.encode(claims, self.secret, algorithm="HS256", headers={"kid": "dev-1"})

    def verify(self, token: str, now: Optional[float] = None) -> Principal:
        try:
            c = jwt.decode(token, self.secret, algorithms=["HS256"], audience=AUDIENCE, issuer=ISSUER,
                           options={"require": ["exp", "iat", "sub", "aud", "iss", "workspace_id"]}, leeway=0)
        except jwt.PyJWTError as e:
            raise AuthError(f"invalid token: {type(e).__name__}")
        return Principal(c["sub"], c["workspace_id"], c["kind"], tuple(c["scopes"]))
