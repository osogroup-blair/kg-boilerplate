"""P5 WP1: scope grants and the visibility predicate builder (pure). The predicate is executed against Postgres in tests/integration/test_p5_scopes.py."""

from __future__ import annotations

import uuid

import pytest
from kg_domain import Principal, PrincipalKind, Role
from kg_memory.models import ScopeIn
from kg_memory.scopes import DEFAULT_CEILING, InvalidScope, ScopeGrants, is_shared, visibility_sql

pytestmark = pytest.mark.unit
SARAH, OTHER = str(uuid.uuid4()), str(uuid.uuid4())


def principal(*roles: Role, kind: PrincipalKind = PrincipalKind.AGENT) -> Principal:
    return Principal(principal_id=uuid.uuid4(), workspace_id=uuid.uuid4(), kind=kind, handle="h", roles=frozenset(roles))


def grants(rows: list[tuple[str, str]], *roles: Role) -> ScopeGrants:
    return ScopeGrants.from_rows(principal(*(roles or (Role.AGENT,))), rows)


def test_a_principal_without_grants_sees_only_the_workspace_scope_and_its_own_private_ones() -> None:
    g = grants([])
    assert g.can_use("workspace", "*") and not g.can_use("subject", SARAH) and not g.can_use("project", "newco")
    assert g.can_use("agent", g.self_ref) and not g.can_use("agent", str(uuid.uuid4()))
    assert g.can_use("session", f"{g.self_ref}/r1") and not g.can_use("session", f"{uuid.uuid4()}/r1")


def test_grants_are_exact_and_wildcards_are_grants_never_scope_values() -> None:
    g = grants([("subject", SARAH), ("project", "newco-plan")])
    assert g.can_use("subject", SARAH) and not g.can_use("subject", OTHER)
    assert g.can_use("project", "newco-plan") and not g.can_use("project", "other")
    star = grants([("subject", "*"), ("project", "*")])
    assert star.can_use("subject", OTHER) and star.can_use("project", "anything")
    assert not star.can_use("subject", "*") and not star.can_use("project", "*")  # a memory is never scoped to "*"


def test_there_is_no_inheritance_between_scope_kinds() -> None:
    projects_only = grants([("project", "*")])
    assert not projects_only.can_use("subject", SARAH) and not projects_only.all_subjects
    subjects_only = grants([("subject", "*")])
    assert not subjects_only.can_use("project", "newco-plan") and not subjects_only.all_projects


def test_the_classification_ceiling_defaults_to_confidential_and_only_a_grant_or_a_curation_role_raises_it() -> None:
    assert grants([]).ceiling == DEFAULT_CEILING == "confidential"
    assert grants([("classification", "restricted")]).ceiling == "restricted"
    assert grants([("classification", "internal")]).ceiling == "confidential"  # a grant never LOWERS the default
    assert grants([], Role.REVIEWER).ceiling == "local_only" and grants([], Role.WORKSPACE_ADMIN).ceiling == "local_only"
    assert grants([], Role.OPERATOR).ceiling == "confidential" and grants([], Role.APP_WRITER).ceiling == "confidential"
    assert grants([("classification", "restricted")]).classifications() == ["public", "internal", "confidential", "restricted"]


def test_writable_scopes_normalise_self_and_refuse_what_the_caller_cannot_see() -> None:
    g = grants([("subject", SARAH)])
    assert g.writable(
        [
            ScopeIn(kind="subject", ref=SARAH),
            ScopeIn(kind="agent", ref="self"),
            ScopeIn(kind="session", ref="self/r1"),
            ScopeIn(kind="workspace"),
        ]
    ) == [
        ("subject", SARAH),
        ("agent", g.self_ref),
        ("session", f"{g.self_ref}/r1"),
        ("workspace", "*"),
    ]
    assert g.writable([ScopeIn(kind="agent", ref=g.self_ref), ScopeIn(kind="agent", ref="self")]) == [
        ("agent", g.self_ref)
    ]  # de-duplicated
    for bad in (
        ScopeIn(kind="subject", ref=OTHER),  # not granted
        ScopeIn(kind="project", ref="newco-plan"),  # not granted
        ScopeIn(kind="agent", ref=str(uuid.uuid4())),  # someone else's private scope
        ScopeIn(kind="session", ref=f"{uuid.uuid4()}/r1"),
        ScopeIn(kind="session", ref="self"),  # no run
    ):
        with pytest.raises(InvalidScope):
            g.writable([bad])


def test_the_same_answer_for_a_scope_that_does_not_exist_and_one_that_is_not_yours() -> None:
    g = grants([])
    errors = set()
    for scope in (ScopeIn(kind="subject", ref=OTHER), ScopeIn(kind="subject", ref=SARAH), ScopeIn(kind="project", ref="nope")):
        with pytest.raises(InvalidScope) as exc:
            g.writable([scope])
        errors.add((exc.value.code, exc.value.http_status, exc.value.message))
    assert len(errors) == 1


def test_scope_models_validate_their_refs() -> None:
    assert ScopeIn(kind="workspace").ref == "*"
    assert ScopeIn.model_validate({"kind": "subject", "id": SARAH}).ref == SARAH  # blueprint payloads say `id`
    for bad in (
        {"kind": "workspace", "ref": "x"},
        {"kind": "subject"},
        {"kind": "subject", "ref": "not-a-uuid"},
        {"kind": "project", "ref": "has space"},
        {"kind": "galaxy"},
    ):
        with pytest.raises(ValueError):  # noqa: PT011
            ScopeIn.model_validate(bad)


def test_shared_means_workspace_subject_or_project() -> None:
    assert is_shared([("workspace", "*")]) and is_shared([("agent", "a"), ("subject", SARAH)])
    assert not is_shared([("agent", "a"), ("session", "a/r1")]) and not is_shared([])


def test_the_predicate_binds_every_value_and_embeds_no_literal_from_the_caller() -> None:
    g = grants([("subject", SARAH), ("project", "newco-plan")])
    sql, params = visibility_sql(g, run_id="r1; DROP TABLE x", all_runs=False)
    assert SARAH not in sql and "newco-plan" not in sql and "DROP" not in sql and g.self_ref not in sql
    assert params["v_subjects"] == [SARAH] and params["v_projects"] == ["newco-plan"]
    assert (
        params["v_session_ref"] == f"{g.self_ref}/r1; DROP TABLE x"
    )  # carried as a bound value (the API validates run ids to [A-Za-z0-9_-])
    assert "memory_scopes" in sql and "m.workspace_id" in sql


def test_private_reveal_needs_both_the_permission_and_the_request() -> None:
    plain = ScopeGrants.from_rows(principal(Role.WORKSPACE_ADMIN), [], may_reveal_private=False)
    allowed = ScopeGrants.from_rows(principal(Role.WORKSPACE_ADMIN), [], may_reveal_private=True)
    assert visibility_sql(plain, reveal_private=True)[1]["v_reveal"] is False
    assert visibility_sql(allowed, reveal_private=False)[1]["v_reveal"] is False
    assert visibility_sql(allowed, reveal_private=True)[1]["v_reveal"] is True


def test_prefixes_keep_two_predicates_in_one_statement_apart() -> None:
    g = grants([])
    a, pa = visibility_sql(g, prefix="a", memory="x")
    b, pb = visibility_sql(g, prefix="b", memory="y")
    assert set(pa).isdisjoint(pb) and ":a_self_ref" in a and ":b_self_ref" in b and "x.workspace_id" in a and "y.workspace_id" in b
