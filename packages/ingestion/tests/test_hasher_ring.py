import uuid

import pytest
from kg_common.secrets import SecretRing
from kg_ingestion.crypto import ContentHasher

pytestmark = pytest.mark.unit
WS = uuid.uuid4()


def test_a_rotated_ring_still_recognises_the_hashes_made_before_it():
    old = ContentHasher(SecretRing({"v1": b"1" * 40}, "v1"))
    new = ContentHasher(SecretRing({"v1": b"1" * 40, "v2": b"2" * 40}, "v2"))
    stored = old.digest(WS, b"hello")
    fresh = new.digest(WS, b"hello")
    assert stored != fresh  # new work uses the new version...
    assert (
        stored in new.digest_all(WS, b"hello") and new.digest_all(WS, b"hello")[0] == fresh
    )  # ...and the old one is still found, the active one first
    assert stored not in ContentHasher(SecretRing({"v2": b"2" * 40}, "v2")).digest_all(
        WS, b"hello"
    )  # once v1 leaves the ring its hashes are not recognised any more


def test_refs_and_json_follow_the_same_rule():
    old = ContentHasher(SecretRing({"v1": b"1" * 40}, "v1"))
    new = ContentHasher(SecretRing({"v1": b"1" * 40, "v2": b"2" * 40}, "v2"))
    assert old.ref(WS, "Person", "hr", "E-1") in new.ref_all(WS, "Person", "hr", "E-1")
    assert old.digest_json(WS, {"a": 1}) in new.digest_json_all(WS, {"a": 1})
    assert new.versions == ["v2", "v1"]


def test_a_bare_secret_still_works_as_before():
    assert ContentHasher("x" * 40).digest(WS, b"d") == ContentHasher(SecretRing({"v1": b"x" * 40}, "v1")).digest(WS, b"d")
