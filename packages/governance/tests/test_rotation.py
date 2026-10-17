"""Rotation of the JWT keys and the deletion journal's MAC key (P9 WP6)."""

import datetime as dt
import uuid

import pytest
from kg_common.secrets import SecretRing
from kg_governance.authn.jwt import LocalSigner, TokenError
from kg_governance.deletion.journal import DeletionJournal, JournalCorrupt

pytestmark = pytest.mark.unit
A, B = "a" * 40, "b" * 40


def ids():
    return dict(principal_id=uuid.uuid4(), workspace_id=uuid.uuid4(), key_id=uuid.uuid4(), kind="agent")


def test_a_token_signed_before_the_rotation_verifies_after_it_until_the_old_key_leaves_the_ring():
    before = LocalSigner(SecretRing({"k1": A.encode()}, "k1"))
    token = before.issue(**ids())
    during = LocalSigner(SecretRing({"k1": A.encode(), "k2": B.encode()}, "k2"))
    assert during.verify(token).principal_id and during.active_kid == "k2" and during.kids == ["k2", "k1"]
    fresh = during.issue(**ids())
    assert LocalSigner(SecretRing({"k2": B.encode()}, "k2")).verify(fresh)  # the new key alone verifies new tokens
    with pytest.raises(TokenError):
        LocalSigner(SecretRing({"k2": B.encode()}, "k2")).verify(token)  # and the retired one no longer verifies old ones


def test_a_token_with_an_unknown_kid_or_the_wrong_secret_is_refused():
    a = LocalSigner(SecretRing({"k1": A.encode()}, "k1"))
    forged = LocalSigner(SecretRing({"k1": B.encode()}, "k1")).issue(**ids())  # right kid, wrong secret
    with pytest.raises(TokenError):
        a.verify(forged)
    other = LocalSigner(SecretRing({"zz": A.encode()}, "zz")).issue(**ids())
    with pytest.raises(TokenError, match="unknown_kid"):
        a.verify(other)


def test_the_journal_survives_a_rotation_and_breaks_loudly_if_a_version_is_dropped_too_early(tmp_path):
    one = DeletionJournal(tmp_path, SecretRing({"v1": b"1" * 40}, "v1"))
    for _ in range(2):
        one.append(
            request_id=uuid.uuid4(),
            workspace_id=uuid.uuid4(),
            kind="subject",
            completed_at=dt.datetime.now(dt.UTC),
            ids={"entities": [str(uuid.uuid4())]},
            suppression_hmacs=[],
        )
    first_line = (tmp_path / "deletions.jsonl").read_text().splitlines()[0]
    assert '"kid"' not in first_line  # a v1 line keeps the exact bytes it always had
    two = DeletionJournal(tmp_path, SecretRing({"v1": b"1" * 40, "v2": b"2" * 40}, "v2"))
    two.append(
        request_id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        kind="subject",
        completed_at=dt.datetime.now(dt.UTC),
        ids={"entities": [str(uuid.uuid4())]},
        suppression_hmacs=[],
    )
    assert '"kid":"v2"' in (tmp_path / "deletions.jsonl").read_text().splitlines()[-1]
    assert two.verify() == 3  # the chain crosses the rotation
    only_new = DeletionJournal(tmp_path, SecretRing({"v2": b"2" * 40}, "v2"))
    with pytest.raises(JournalCorrupt):
        only_new.verify()  # v1 was dropped while two lines still depend on it
