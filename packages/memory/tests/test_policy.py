"""P5 WP2: the pure memory policy — layered resolution and the decision table (tests/fixtures/policy/decisions.yaml), with a meta-test for coverage."""

from __future__ import annotations

import itertools
from pathlib import Path

import pytest
import yaml
from kg_memory.models import AUTHORITY_RANK, MEMORY_TYPES
from kg_memory.policy import (
    ACTORS,
    PLATFORM_DEFAULTS,
    EffectivePolicy,
    authority_for,
    decide,
    default_expiry,
    evidence_status_for,
    outranks,
    resolve_policy,
)
from kg_ontology import parse_document
from kg_ontology.spec import MemoryPolicy

from tests.support.ontology_base import BASE

pytestmark = pytest.mark.unit
TABLE = yaml.safe_load((Path(__file__).parent / "fixtures" / "policy" / "decisions.yaml").read_text())
CASES = [{**TABLE["defaults"], **c} for c in TABLE["cases"]]


def policy_of(overrides: dict) -> EffectivePolicy:
    spec, issues = parse_document({**BASE, "memory_policies": {"default": overrides}})
    assert spec is not None, issues
    return resolve_policy(spec.memory_policies, "semantic")


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_decision_table(case: dict) -> None:
    d = decide(
        origin=case["origin"],
        memory_type=case["type"],
        actor=case["actor"],
        shared=case["shared"],
        conflict=case["conflict"],
        policy=policy_of(case["policy"]),
        can_publish=case["publish"],
    )
    assert (d.status, d.reason, d.review) == (case["status"], case["reason"], case.get("review"))


def test_every_combination_that_can_occur_has_a_case() -> None:
    """The meta-test (WP2): origin x type x actor. A combination that cannot occur is listed here WITH the reason; anything else needs a row in the table."""
    covered = {(c["origin"], c["type"], c["actor"]) for c in CASES}
    cannot_occur = {("working", t, a) for t in MEMORY_TYPES if t != "working" for a in ACTORS} | {
        ("working", "working", "system")
    }  # working origin is for working type only; the system never writes working memory
    cannot_occur |= {
        (o, "working", a) for o in ("proposal", "published", "explicit") for a in ACTORS if (o, "working", a) not in covered
    }  # proposed or published working memory is refused earlier
    cannot_occur |= {(o, t, "system") for o in ("explicit", "proposal", "published") for t in MEMORY_TYPES}  # the system only extracts
    cannot_occur |= {("extracted", t, a) for t in MEMORY_TYPES for a in ("agent", "app", "admin")}  # extraction is the platform's own act
    cannot_occur |= {
        ("published", t, "admin") for t in MEMORY_TYPES
    }  # an admin's publish is an admin proposal (covered by the proposal rows)
    missing = [
        combo
        for combo in itertools.product(("explicit", "proposal", "published", "extracted", "working"), MEMORY_TYPES, ACTORS)
        if combo not in covered and combo not in cannot_occur
    ]
    assert missing == [], f"add a case for: {missing}"


def test_an_agent_can_never_activate_a_shared_procedural_memory_whatever_the_policy_says() -> None:
    lenient = policy_of(
        {
            "auto_activate": ["episodic", "semantic", "procedural"],
            "procedural_requires_review": False,
            "agent_shared_requires_review": False,
        }
    )
    for origin in ("proposal", "published", "explicit"):
        d = decide(origin=origin, memory_type="procedural", actor="agent", shared=True, conflict=False, policy=lenient, can_publish=True)
        assert d.status == "proposed" and d.review == "memory_proposal", origin


def test_the_platform_defaults_are_the_documented_ones() -> None:
    assert PLATFORM_DEFAULTS == EffectivePolicy(
        extraction="enabled",
        extraction_min_importance=0.5,
        max_memories_per_interaction=3,
        auto_activate=("episodic", "semantic"),
        procedural_requires_review=True,
        agent_shared_requires_review=True,
        working_ttl_hours=24.0,
        max_content_chars=2000,
        default_extracted_scope="subject",
        excluded_classifications=("local_only",),
        archive_after_days=None,
    )
    assert resolve_policy(None, "semantic") == PLATFORM_DEFAULTS and resolve_policy({}, "procedural") == PLATFORM_DEFAULTS


def test_a_type_override_falls_through_to_the_default_then_to_the_platform() -> None:
    spec, issues = parse_document(
        {
            **BASE,
            "memory_policies": {
                "default": {"working_ttl_hours": 6, "max_memories_per_interaction": 5},
                "procedural": {"working_ttl_hours": 1},
            },
        }
    )
    assert spec is not None, issues
    procedural = resolve_policy(spec.memory_policies, "procedural")
    semantic = resolve_policy(spec.memory_policies, "semantic")
    assert procedural.working_ttl_hours == 1 and procedural.max_memories_per_interaction == 5 and procedural.max_content_chars == 2000
    assert semantic.working_ttl_hours == 6 and semantic.auto_activate == ("episodic", "semantic")


def test_a_stored_canonical_spec_with_nulls_resolves_like_one_without() -> None:
    """The ontology service stores the canonical dump, where every unset field is null: that must not blank the platform defaults."""
    stored = MemoryPolicy().model_dump()
    assert all(v is None for v in stored.values())
    assert resolve_policy({"default": MemoryPolicy(**stored)}, "semantic") == PLATFORM_DEFAULTS


def test_authority_follows_how_the_memory_was_made() -> None:
    assert authority_for("extracted", "system") == "conversation_report"
    assert authority_for("extracted", "system", submitter_is_agent=True) == "agent_generated"
    assert authority_for("explicit", "admin") == "admin_asserted"
    assert authority_for("explicit", "app") == "user_asserted"
    assert authority_for("proposal", "agent") == "agent_generated"
    assert outranks("admin_asserted", "conversation_report") and not outranks("conversation_report", "admin_asserted")
    assert not outranks("user_asserted", "user_asserted") and set(AUTHORITY_RANK) >= {authority_for("explicit", "app")}


def test_only_a_trusted_explicit_save_is_verified() -> None:
    assert (
        evidence_status_for("explicit", "active", "app") == "verified" and evidence_status_for("explicit", "active", "admin") == "verified"
    )
    assert evidence_status_for("explicit", "active", "agent") == "unverified"
    assert evidence_status_for("explicit", "proposed", "app") == "unverified"
    assert (
        evidence_status_for("extracted", "active", "system") == "unverified"
        and evidence_status_for("proposal", "active", "app") == "unverified"
    )


def test_a_caller_may_shorten_the_working_ttl_but_never_extend_it() -> None:
    p = PLATFORM_DEFAULTS
    assert default_expiry(p, None).total_seconds() == 24 * 3600
    assert default_expiry(p, 0.5).total_seconds() == 1800
    assert default_expiry(p, 1000).total_seconds() == 24 * 3600
