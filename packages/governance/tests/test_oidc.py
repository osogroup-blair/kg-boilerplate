"""OIDC verification (phase-9 WP7): what the platform accepts from an IdP and, more to the point, what it refuses."""

from __future__ import annotations

import time

import jwt
import pytest
from kg_governance.authn.oidc import OidcConfig, OidcError, OidcVerifier

from tests.support.idp import AUDIENCE, ISSUER, TestIdp, hs256_confusion_token

pytestmark = pytest.mark.unit


def verifier(idp: TestIdp, **over) -> OidcVerifier:
    cfg = OidcConfig(issuer=ISSUER, audience=AUDIENCE, **over)
    return OidcVerifier(cfg, http=idp.client(), clock=time.monotonic)


async def test_a_valid_token_yields_its_claims():
    idp = TestIdp()
    v = verifier(idp)
    claims = await v.verify(idp.sign("alice", groups=["kg-admins", "x"], kg_workspace="acme"))
    assert (claims.subject, claims.groups, claims.workspace_slug, claims.issuer) == ("alice", ("kg-admins", "x"), "acme", ISSUER)
    assert claims.display_name == "User alice"
    assert v.looks_like_issuer(idp.sign("alice")) and not v.looks_like_issuer(idp.sign("alice", iss="https://other"))


async def test_es256_is_accepted_when_configured_and_the_key_is_chosen_by_kid():
    idp = TestIdp()
    idp.rotate("ec1", "ES256")
    v = verifier(idp, algorithms=("RS256", "ES256"))
    assert (await v.verify(idp.sign("bob", key=idp.keys[-1]))).subject == "bob"
    assert (await v.verify(idp.sign("carol", key=idp.keys[0]))).subject == "carol"  # the older RS256 key, same JWKS


@pytest.mark.parametrize(
    "what",
    [
        "expired",
        "wrong_audience",
        "wrong_issuer",
        "no_exp",
        "no_sub",
        "not_yet_valid",
        "unknown_key",
        "tampered",
        "alg_none",
        "hs256_confusion",
        "no_kid",
        "garbage",
    ],
)
async def test_everything_else_is_refused(what):
    idp = TestIdp()
    v = verifier(idp)
    good = idp.sign("alice")
    now = int(time.time())
    token = {
        "expired": lambda: idp.sign(ttl=-120),
        "wrong_audience": lambda: idp.sign(aud="someone-else"),
        "wrong_issuer": lambda: idp.sign(iss="https://evil.example/realms/kg"),
        "no_exp": lambda: idp.sign(exp=None),
        "no_sub": lambda: idp.sign(sub=""),
        "not_yet_valid": lambda: idp.sign(nbf=now + 3600),
        "unknown_key": lambda: idp.sign(key=__import__("tests.support.idp", fromlist=["Key"]).Key("rogue")),
        "tampered": lambda: good[:-4] + ("AAAA" if not good.endswith("AAAA") else "BBBB"),
        "alg_none": lambda: jwt.encode(
            {"iss": ISSUER, "aud": AUDIENCE, "sub": "x", "exp": now + 60}, None, algorithm="none", headers={"kid": "k1"}
        ),
        "hs256_confusion": lambda: hs256_confusion_token(idp),
        "no_kid": lambda: jwt.encode({"iss": ISSUER, "aud": AUDIENCE, "sub": "x", "exp": now + 60}, idp.keys[0].private, algorithm="RS256"),
        "garbage": lambda: "not.a.jwt",
    }[what]()
    with pytest.raises(OidcError):
        await v.verify(token)


async def test_hmac_and_none_can_never_be_configured():
    for bad in (("HS256",), ("none",), ("RS256", "HS512"), ()):
        with pytest.raises(ValueError):
            OidcConfig(issuer=ISSUER, audience=AUDIENCE, algorithms=bad)
    with pytest.raises(ValueError):
        OidcConfig(issuer="", audience=AUDIENCE)


async def test_a_key_rollover_is_picked_up_without_a_restart_and_refetches_are_rate_limited():
    idp = TestIdp()
    clock = {"t": 0.0}
    v = OidcVerifier(OidcConfig(issuer=ISSUER, audience=AUDIENCE), http=idp.client(), clock=lambda: clock["t"])
    assert (await v.verify(idp.sign("a"))).subject == "a"
    assert idp.jwks_fetches == 1
    new = idp.rotate("k2")  # the IdP rolls its signing key
    clock["t"] += 31
    assert (await v.verify(idp.sign("b", key=new))).subject == "b"  # unknown kid -> one re-fetch
    assert idp.jwks_fetches == 2
    rogue_fetches = idp.jwks_fetches
    from tests.support.idp import Key

    for _ in range(5):  # a flood of tokens with unknown kids cannot turn the platform into a JWKS hammer
        with pytest.raises(OidcError):
            await v.verify(idp.sign(key=Key("nope")))
    assert idp.jwks_fetches - rogue_fetches <= 1
    old = idp.keys[0]
    idp.rotate("k3", drop_old=True)  # k1 and k2 are gone from the IdP; cached keys still verify until the TTL passes
    assert (await v.verify(idp.sign("c", key=old))).subject == "c"


async def test_groups_may_be_a_string_a_list_or_garbage():
    idp = TestIdp()
    v = verifier(idp)
    assert (await v.verify(idp.sign(groups="solo"))).groups == ("solo",)
    assert (await v.verify(idp.sign(groups=[1, "ok", None, "x" * 500]))).groups == ("ok",)
    assert (await v.verify(idp.sign(groups={"a": 1}))).groups == ()
