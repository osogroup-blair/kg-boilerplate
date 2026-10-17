import pytest
from kg_common.secrets import RingError, SecretRing

pytestmark = pytest.mark.unit
A, B = "a" * 40, "b" * 40


def test_a_single_legacy_secret_is_a_one_version_ring():
    ring = SecretRing.parse("", "", legacy=A, legacy_version="v1")
    assert ring.versions == ["v1"] and ring.current == A.encode() and ring.active == "v1"


def test_the_active_version_comes_first_and_every_version_stays_readable():
    ring = SecretRing.parse(f"v1:{A},v2:{B}", "v2")
    assert ring.versions == ["v2", "v1"] and ring.current == B.encode() and ring.get("v1") == A.encode() and ring.get("v9") is None


@pytest.mark.parametrize("spec", ["v1", "v1:", ":x", "v 1:abc", "v1:" + "a" * 40 + ",v1:" + "b" * 40])
def test_malformed_rings_are_refused(spec):
    with pytest.raises(RingError):
        SecretRing.parse(spec, "", name="X")


def test_an_active_version_must_be_in_the_ring_and_short_secrets_are_refused():
    with pytest.raises(RingError, match="not in"):
        SecretRing.parse(f"v1:{A}", "v2")
    with pytest.raises(RingError, match="shorter"):
        SecretRing.parse("v1:short", "")


def test_errors_name_versions_never_secrets():
    with pytest.raises(RingError) as exc:
        SecretRing.parse("v1:short", "")
    assert "short" not in str(exc.value).replace("shorter", "")
