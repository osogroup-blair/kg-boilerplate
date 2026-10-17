"""A small in-repo identity provider for the OIDC suites: discovery, a JWKS with rotating keys, and a token endpoint (authorization_code with PKCE, refresh_token). Served over
httpx's ASGI transport, so nothing listens on a port. It signs whatever the test asks (valid, expired, wrong audience, wrong issuer, algorithm confusion)."""

from __future__ import annotations

import base64
import hashlib
import json
import time
import uuid
from typing import Any

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

_UNSET: Any = object()
ISSUER = "https://idp.test/realms/kg"
AUDIENCE = "kg-console"


def _b64(n: int) -> str:
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


class Key:
    def __init__(self, kid: str, alg: str = "RS256"):
        self.kid, self.alg = kid, alg
        if alg == "ES256":
            self.private = ec.generate_private_key(ec.SECP256R1())
        else:
            self.private = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def jwk(self) -> dict[str, Any]:
        pub = self.private.public_key()
        if self.alg == "ES256":
            nums = pub.public_numbers()  # type: ignore[union-attr]
            return {
                "kty": "EC",
                "crv": "P-256",
                "x": _b64(nums.x).rjust(43, "A"),
                "y": _b64(nums.y).rjust(43, "A"),
                "kid": self.kid,
                "use": "sig",
                "alg": "ES256",
            }
        nums = pub.public_numbers()  # type: ignore[union-attr]
        return {"kty": "RSA", "n": _b64(nums.n), "e": _b64(nums.e), "kid": self.kid, "use": "sig", "alg": "RS256"}


class TestIdp:
    __test__ = False  # not a pytest class

    def __init__(self, issuer: str = ISSUER, audience: str = AUDIENCE):
        self.issuer, self.audience = issuer, audience
        self.keys: list[Key] = [Key("k1")]
        self.jwks_fetches = 0
        self.codes: dict[str, dict[str, Any]] = {}
        self.refresh: dict[str, dict[str, Any]] = {}
        self.claims_for_next: dict[str, Any] = {}
        self.default_groups: list[str] = ["kg-admins"]
        self.app = Starlette(
            routes=[
                Route("/realms/kg/.well-known/openid-configuration", self._discovery),
                Route("/realms/kg/jwks", self._jwks),
                Route("/realms/kg/token", self._token, methods=["POST"]),
            ]
        )

    # ---- tokens ----------------------------------------------------------------------------------------------------------------------------
    def rotate(self, kid: str, alg: str = "RS256", *, drop_old: bool = False) -> Key:
        key = Key(kid, alg)
        self.keys = [key] if drop_old else [*self.keys, key]
        return key

    def sign(self, sub: str = "user-1", *, groups: Any = _UNSET, key: Key | None = None, ttl: int = 600, **over: Any) -> str:
        key = key or self.keys[-1]
        now = int(time.time())
        claims: dict[str, Any] = {
            "iss": self.issuer,
            "aud": self.audience,
            "sub": sub,
            "iat": now,
            "nbf": now - 1,
            "exp": now + ttl,
            "name": f"User {sub}",
            "groups": self.default_groups if groups is _UNSET else groups,
        }
        claims.update(over)
        claims = {k: v for k, v in claims.items() if v is not None}
        return jwt.encode(claims, key.private, algorithm=key.alg, headers={"kid": key.kid})

    # ---- endpoints -------------------------------------------------------------------------------------------------------------------------
    async def _discovery(self, request: Request) -> JSONResponse:
        base = self.issuer
        return JSONResponse(
            {
                "issuer": base,
                "jwks_uri": f"{base}/jwks",
                "authorization_endpoint": f"{base}/auth",
                "token_endpoint": f"{base}/token",
                "id_token_signing_alg_values_supported": ["RS256", "ES256"],
            }
        )

    async def _jwks(self, request: Request) -> JSONResponse:
        self.jwks_fetches += 1
        return JSONResponse({"keys": [k.jwk() for k in self.keys]})

    def issue_code(self, *, verifier: str, redirect_uri: str, sub: str = "user-1", groups: list[str] | None = None) -> str:
        code = uuid.uuid4().hex
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        self.codes[code] = {"challenge": challenge, "redirect_uri": redirect_uri, "sub": sub, "groups": groups}
        return code

    async def _token(self, request: Request) -> JSONResponse:
        form = dict((await request.form()).items())
        if form.get("grant_type") == "authorization_code":
            entry = self.codes.pop(str(form.get("code")), None)  # a code works once
            if entry is None or form.get("redirect_uri") != entry["redirect_uri"]:
                return JSONResponse({"error": "invalid_grant"}, status_code=400)
            verifier = str(form.get("code_verifier", ""))
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
            if challenge != entry["challenge"]:
                return JSONResponse({"error": "invalid_grant"}, status_code=400)
            refresh = uuid.uuid4().hex
            self.refresh[refresh] = {"sub": entry["sub"], "groups": entry["groups"]}
            return JSONResponse(
                {"id_token": self.sign(entry["sub"], groups=entry["groups"]), "refresh_token": refresh, "token_type": "Bearer"}
            )
        if form.get("grant_type") == "refresh_token":
            entry = self.refresh.pop(str(form.get("refresh_token")), None)  # rotated on every use
            if entry is None:
                return JSONResponse({"error": "invalid_grant"}, status_code=400)
            refresh = uuid.uuid4().hex
            self.refresh[refresh] = entry
            return JSONResponse(
                {"id_token": self.sign(entry["sub"], groups=entry["groups"]), "refresh_token": refresh, "token_type": "Bearer"}
            )
        return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url=self.issuer.split("/realms")[0])


def hs256_confusion_token(idp: TestIdp, *, sub: str = "attacker", groups: list[str] | None = None) -> str:
    """The classic attack: sign with HS256 using the IdP's PUBLIC key (PEM) as the HMAC secret, claiming the right kid. Built by hand: PyJWT itself refuses to do it."""
    import hmac

    from cryptography.hazmat.primitives import serialization

    key = idp.keys[-1]
    pem = key.private.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)  # type: ignore[union-attr]

    def enc(obj: dict[str, Any]) -> bytes:
        return base64.urlsafe_b64encode(json.dumps(obj, separators=(",", ":")).encode()).rstrip(b"=")

    now = int(time.time())
    signing_input = (
        enc({"alg": "HS256", "typ": "JWT", "kid": key.kid})
        + b"."
        + enc({"iss": idp.issuer, "aud": idp.audience, "sub": sub, "exp": now + 600, "groups": groups or idp.default_groups})
    )
    sig = base64.urlsafe_b64encode(hmac.new(pem, signing_input, hashlib.sha256).digest()).rstrip(b"=")
    return (signing_input + b"." + sig).decode()
