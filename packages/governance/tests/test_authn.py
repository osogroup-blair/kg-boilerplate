"""AU-01 negative set (unit level; DB-backed revocation lives in tests/security)."""

import time
import uuid

import jwt
import pytest
from kg_governance.authn import keys
from kg_governance.authn.jwt import AUDIENCE, ISSUER, LocalSigner, TokenError

pytestmark = pytest.mark.unit
SECRET = "unit-test-secret-0123456789abcdef0123456789"


def _ids():
    return dict(principal_id=uuid.uuid4(), workspace_id=uuid.uuid4(), key_id=uuid.uuid4(), kind="agent")


def _claims(**over):
    now = int(time.time())
    c = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": str(uuid.uuid4()),
        "iat": now,
        "nbf": now,
        "exp": now + 600,
        "jti": "j",
        "workspace_id": str(uuid.uuid4()),
        "key_id": str(uuid.uuid4()),
        "kind": "agent",
    }
    c.update(over)
    return {k: v for k, v in c.items() if v is not None}


def test_roundtrip():
    s = LocalSigner(SECRET)
    ids = _ids()
    claims = s.verify(s.issue(**ids))
    assert (claims.principal_id, claims.workspace_id, claims.key_id) == (ids["principal_id"], ids["workspace_id"], ids["key_id"])


@pytest.mark.parametrize(
    "make",
    [
        lambda: jwt.encode(_claims(), SECRET, algorithm="HS256")[:-3] + "abc",  # tampered signature
        lambda: jwt.encode(_claims(), "x" * 40, algorithm="HS256"),  # wrong key
        lambda: jwt.encode(_claims(), SECRET, algorithm="HS384"),  # algorithm confusion
        lambda: jwt.encode(_claims(), SECRET, algorithm="HS512"),
        lambda: jwt.encode(_claims(aud="other"), SECRET, algorithm="HS256"),  # wrong audience
        lambda: jwt.encode(_claims(iss="evil"), SECRET, algorithm="HS256"),
        lambda: jwt.encode(_claims(workspace_id=None), SECRET, algorithm="HS256"),  # missing workspace_id
        lambda: jwt.encode(_claims(key_id=None), SECRET, algorithm="HS256"),
        lambda: jwt.encode(_claims(exp=int(time.time()) - 5), SECRET, algorithm="HS256"),  # expired
        lambda: jwt.encode(_claims(nbf=int(time.time()) + 600), SECRET, algorithm="HS256"),  # not yet valid
        lambda: jwt.encode(_claims(workspace_id="not-a-uuid"), SECRET, algorithm="HS256"),
        lambda: jwt.encode(_claims(), key="", algorithm="none"),  # alg=none
        lambda: "not.a.jwt",
    ],
)
def test_bad_tokens_are_rejected(make):
    with pytest.raises(TokenError):
        LocalSigner(SECRET).verify(make())


def test_tampered_workspace_claim_invalidates_signature():
    s = LocalSigner(SECRET)
    good = s.issue(**_ids())
    header, _payload, sig = good.split(".")
    forged_payload = jwt.utils.base64url_encode(b'{"workspace_id":"%s"}' % str(uuid.uuid4()).encode()).decode()
    with pytest.raises(TokenError):
        s.verify(f"{header}.{forged_payload}.{sig}")


def test_key_format_and_hash():
    m = keys.mint_key("pepper")
    assert m.plaintext.startswith(f"kgk_{m.prefix}_") and keys.parse_prefix(m.plaintext) == m.prefix and len(m.prefix) == 8
    assert m.key_hash == keys.hash_key("pepper", m.plaintext) != keys.hash_key("other", m.plaintext)
    assert m.plaintext not in m.key_hash
    assert keys.parse_prefix("kgk_short_x") is None and keys.parse_prefix("abc") is None and keys.parse_prefix("xxx_abcdefgh_s") is None
    assert keys.constant_time_equal("a", "a") and not keys.constant_time_equal("a", "b")


def test_secret_part_may_contain_underscores():
    assert keys.parse_prefix("kgk_abcdefgh_se_cr_et") == "abcdefgh"
