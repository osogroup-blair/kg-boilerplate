"""ADR-027: the authority-aware conflict policy. Pure fixtures; every branch of `resolve` has a case."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import pytest
import yaml
from kg_ontology.conflicts import (
    EndEffect,
    FactView,
    Incoming,
    Invalidation,
    PolicyInputs,
    evidence_status,
    rank,
    resolve,
    succession_evidence,
)
from kg_ontology.spec import AUTHORITY_RANK
from kg_ontology.values import parse_instant

pytestmark = pytest.mark.unit
FIXTURES = Path(__file__).parent / "fixtures" / "conflicts"
BRANCHES = {
    "hard_reject",
    "no_conflict",
    "higher_authority",
    "equal_authority",
    "lower_authority",
    "dated_succession",
    "succession_effect",
    "policy_review",
    "identity_hold",
}


def _t(value: Any) -> dt.datetime | None:
    if value is None:
        return None
    if isinstance(value, dt.date) and not isinstance(value, dt.datetime):
        return dt.datetime(value.year, value.month, value.day, tzinfo=dt.UTC)
    parsed = parse_instant(value)
    assert parsed is not None
    return parsed


def _cases() -> list[tuple[str, dict[str, Any]]]:
    out = []
    for path in sorted(FIXTURES.glob("*.yaml")):
        for case in yaml.safe_load(path.read_text())["cases"]:
            out.append((case["name"], case))
    return out


CASES = _cases()


def _build(case: dict[str, Any]) -> tuple[Incoming, list[FactView], PolicyInputs]:
    n = case["new"]
    new = Incoming(n["authority"], _t(n.get("valid_from")), _t(n.get("valid_to")))
    existing = [
        FactView(f["id"], f["authority"], _t(f.get("valid_from")), _t(f.get("valid_to")), f.get("status", "active"))
        for f in case.get("existing", [])
    ]
    i = case.get("inputs", {})
    inputs = PolicyInputs(
        hard_reject_code=i.get("hard_reject"),
        review_codes=tuple(i.get("review", [])),
        hold_code=i.get("hold"),
        rule_conflict=bool(i.get("rule_conflict", False)),
        succession_effects=tuple(EndEffect(e["fact"], _t(e["valid_to"])) for e in i.get("effects", [])),  # type: ignore[arg-type]
        invalidated=tuple(Invalidation(x["fact"], _t(x.get("invalid_at"))) for x in i.get("invalidated", [])),
    )
    return new, existing, inputs


@pytest.mark.parametrize(("label", "case"), CASES, ids=[c[0][:70] for c in CASES])
def test_fixture_case(label: str, case: dict[str, Any]) -> None:
    new, existing, inputs = _build(case)
    got = resolve(new, existing, inputs)
    want = case["expect"]
    assert got.status == want["status"], got
    assert got.code == want["code"], got
    assert got.review == want.get("review"), got
    assert sorted(got.competing) == sorted(want.get("competing", [])), got
    assert sorted(got.disputed) == sorted(want.get("disputed", [])), got
    assert sorted((e.fact_id, e.valid_to) for e in got.end_facts) == sorted((e["fact"], _t(e["valid_to"])) for e in want.get("end", [])), (
        got
    )


def test_every_branch_has_a_fixture() -> None:
    assert {c["branch"] for _, c in CASES} >= BRANCHES


def test_the_ranking_is_the_documented_order() -> None:
    order = ["agent_generated", "conversation_report", "user_asserted", "admin_asserted", "authoritative_system"]
    assert sorted(AUTHORITY_RANK, key=rank) == order
    assert rank("something_unknown") == 0


def test_a_model_never_outranks_a_person_or_a_system() -> None:
    for model in ("conversation_report", "agent_generated"):
        for trusted in ("user_asserted", "admin_asserted", "authoritative_system"):
            assert rank(model) < rank(trusted)


def test_evidence_status_follows_authority() -> None:
    assert {a: evidence_status(a) for a in AUTHORITY_RANK} == {
        "agent_generated": "unverified",
        "conversation_report": "unverified",
        "user_asserted": "unverified",
        "admin_asserted": "verified",
        "authoritative_system": "verified",
    }


def test_succession_evidence_needs_two_dates_and_a_strictly_later_start() -> None:
    old = FactView("o", "conversation_report", _t("2024-01-01"))
    assert succession_evidence(Incoming("conversation_report", _t("2025-01-01")), old)
    assert not succession_evidence(Incoming("conversation_report", _t("2024-01-01")), old)  # same instant
    assert not succession_evidence(Incoming("conversation_report", None), old)  # no start stated
    assert not succession_evidence(Incoming("conversation_report", _t("2025-01-01")), FactView("o", "x", None))  # the old one has no start
    ended = FactView("o", "x", _t("2024-01-01"), _t("2024-06-01"))
    assert not succession_evidence(Incoming("conversation_report", _t("2025-01-01")), ended)  # already ended before: nothing to succeed


def test_resolution_is_deterministic_regardless_of_the_order_of_the_inputs() -> None:
    new = Incoming("conversation_report")
    a, b = FactView("a", "conversation_report"), FactView("b", "conversation_report")
    one = resolve(new, [a, b], PolicyInputs(rule_conflict=True))
    two = resolve(new, [b, a], PolicyInputs(rule_conflict=True))
    assert one.status == two.status == "disputed" and sorted(one.disputed) == sorted(two.disputed) == ["a", "b"]
