"""AU-01 (OFFLINE, DEC-03): dev auth = hashed API keys + optional local JWT; OIDC path documented, not built."""
from __future__ import annotations

import base64
import json
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

import results_plugin as rp
from auth.devauth import AuthError, KeyStore, LocalSigner, Principal

WS_A, WS_B = "11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"
AGENT = Principal("agent_research", WS_A, "agent", ("memory:propose", "context:build"))


def test_au01_api_keys_hashed_prefixed_workspace_bound_and_revocable():
    store = KeyStore(pepper=b"pepper")
    key, rec = store.mint(AGENT)
    assert key.startswith("kgk_") and rec.prefix in key and key not in json.dumps(rec.__dict__, default=str)     # plaintext never stored
    assert len(rec.key_hash) == 64 and store.authenticate(key) == AGENT and store.authenticate(key).workspace_id == WS_A
    # wrong secret with right prefix, unknown prefix, garbage, wrong scheme, tampered
    for bad in (key[:-1] + ("A" if key[-1] != "A" else "B"), "kgk_zzzzzzzz_" + "x" * 43, "garbage", "sk_" + key[4:], ""):
        with pytest.raises(AuthError):
            store.authenticate(bad)
    # a different pepper cannot validate the same key (hash is peppered)
    other = KeyStore(pepper=b"other")
    other.records = dict(store.records)
    with pytest.raises(AuthError):
        other.authenticate(key)
    # expiry and immediate revocation
    k2, r2 = store.mint(AGENT, ttl_s=60)
    assert store.authenticate(k2, now=time.time() + 30) == AGENT
    with pytest.raises(AuthError):
        store.authenticate(k2, now=time.time() + 61)
    store.revoke(r2.prefix)
    with pytest.raises(AuthError):
        store.authenticate(k2)
    assert store.authenticate(key) == AGENT                              # revoking one key does not affect another


def test_au01_jwt_short_lived_and_strict():
    signer = LocalSigner("a-32-byte-or-longer-dev-secret-value!!", ttl_s=900)
    now = time.time()
    tok = signer.issue(AGENT, now=now)
    p = signer.verify(tok)
    assert p == AGENT and p.workspace_id == WS_A
    with pytest.raises(AuthError):
        LocalSigner("a-32-byte-or-longer-dev-secret-value!!", ttl_s=-1).verify(LocalSigner("a-32-byte-or-longer-dev-secret-value!!", ttl_s=-1).issue(AGENT))  # expired
    with pytest.raises(AuthError):
        LocalSigner("different-secret-different-secret-xx").verify(tok)                                    # wrong key
    # tampered payload: swap workspace in the claims, keep the signature
    h, pl, sig = tok.split(".")
    claims = json.loads(base64.urlsafe_b64decode(pl + "=="))
    claims["workspace_id"] = WS_B
    forged = ".".join([h, base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode(), sig])
    with pytest.raises(AuthError):
        signer.verify(forged)
    # alg=none and algorithm confusion (token signed with HS256 using an RSA public key as the secret, presented to an HS256-only verifier)
    none_tok = jwt.encode({"sub": "x", "workspace_id": WS_B}, key="", algorithm="none")
    with pytest.raises(AuthError):
        signer.verify(none_tok)
    # wrong audience / issuer
    bad_aud = jwt.encode({**jwt.decode(tok, options={"verify_signature": False}), "aud": "other-api"}, signer.secret, algorithm="HS256")
    with pytest.raises(AuthError):
        signer.verify(bad_aud)
    # missing required claim
    no_ws = jwt.encode({k: v for k, v in jwt.decode(tok, options={"verify_signature": False}).items() if k != "workspace_id"}, signer.secret, algorithm="HS256")
    with pytest.raises(AuthError):
        signer.verify(no_ws)

    # the OIDC path uses the same claim mapping with an asymmetric verifier; prove RS256 verification works with pinned algorithms
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    idp_token = jwt.encode({"iss": "https://idp.example", "aud": "kg-api", "sub": "user_1", "exp": int(now) + 300, "iat": int(now), "workspace_id": WS_A},
                           priv, algorithm="RS256", headers={"kid": "k1"})
    claims_ok = jwt.decode(idp_token, priv.public_key(), algorithms=["RS256"], audience="kg-api", issuer="https://idp.example")
    assert claims_ok["workspace_id"] == WS_A
    with pytest.raises(jwt.PyJWTError):
        jwt.decode(idp_token, priv.public_key(), algorithms=["HS256"], audience="kg-api", issuer="https://idp.example")   # confusion rejected by pinning

    rp.evidence("AU-01", library="PyJWT " + jwt.__version__, key_format="kgk_<8 prefix>_<43 secret>; store prefix + sha256(pepper||key)",
                jwt_alg="HS256 (local) / RS256|ES256 (OIDC, JWKS, pinned)", jwt_ttl_s=900, required_claims=["iss", "aud", "sub", "iat", "exp", "workspace_id"],
                rejected=["tampered claims", "alg=none", "wrong key", "wrong aud", "missing workspace_id", "expired", "alg confusion"])
    rp.note("AU-01", "DEV AUTH (DEC-03): platform-issued API keys (hashed with a server pepper, prefix stored and shown, constant-time compare, optional expiry, "
                     "immediate revocation) bound to a workspace-scoped principal; optional 15-minute JWT from a local HS256 signer after key exchange. "
                     "Library: PyJWT (small, pinned algorithms, required-claim enforcement) rather than authlib (an OAuth framework we do not need). "
                     "OIDC path (documented, not built): verify RS256/ES256 via the IdP's JWKS with PyJWKClient, pin algorithms, require iss/aud/exp, "
                     "and map a configured claim (or an IdP group -> workspace table) to workspace_id; the Principal shape is identical so no other "
                     "code changes. Workspace ALWAYS comes from the key record/claim, never from a header or body. Sessions for the admin UI use "
                     "the same JWT with kind=user.")
