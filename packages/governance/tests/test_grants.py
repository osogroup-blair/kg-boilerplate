"""P7 WP0: ScopeGrants with privileges, and the SubjectFilter that decides which entities (and so which facts) a principal sees. Pure: no database."""

from __future__ import annotations

import uuid

import pytest
from kg_domain.enums import PrincipalKind, Role
from kg_domain.principal import Principal
from kg_governance.grants import (
    DEFAULT_PRIVILEGES,
    InvalidScope,
    ScopeGrants,
    SubjectFilter,
    entity_visible_sql,
    fact_visible_sql,
)

pytestmark = pytest.mark.unit
SARAH, MICHAEL = str(uuid.uuid4()), str(uuid.uuid4())


class Scope:
    def __init__(self, kind: str, ref: str | None):
        self.kind, self.ref = kind, ref


def principal(*roles: Role) -> Principal:
    return Principal(principal_id=uuid.uuid4(), workspace_id=uuid.uuid4(), kind=PrincipalKind.AGENT, handle="a", roles=frozenset(roles))


def grants(*rows: tuple, role: Role = Role.AGENT) -> ScopeGrants:
    return ScopeGrants.from_rows(principal(role), list(rows))


def test_a_two_part_row_is_the_default_privileges_and_publish_is_never_implicit() -> None:
    g = grants(("project", "newco-plan"))
    assert g.can_use("project", "newco-plan") and g.can_propose("project", "newco-plan")
    assert not g.can_publish("project", "newco-plan")
    assert DEFAULT_PRIVILEGES == ("read", "propose")


def test_privileges_are_per_grant() -> None:
    g = grants(("project", "newco-plan", ("read", "propose", "publish")), ("subject", SARAH, ("read",)))
    assert g.can_publish("project", "newco-plan") and g.can_propose("project", "newco-plan")
    assert g.can_use("subject", SARAH) and not g.can_propose("subject", SARAH) and not g.can_publish("subject", SARAH)
    assert not g.can_use("subject", MICHAEL)


def test_a_wildcard_grant_carries_its_privileges_but_a_memory_is_never_scoped_to_a_wildcard() -> None:
    g = grants(("subject", "*", ("read", "propose")), ("project", "*", ("read", "propose", "publish")))
    assert g.can_use("subject", SARAH) and g.can_propose("subject", SARAH) and not g.can_publish("subject", SARAH)
    assert g.can_publish("project", "anything")
    assert not g.can_use("subject", "*") and not g.can_propose("project", "*")  # a wildcard is a grant, never a value


def test_scopes_do_not_inherit_a_project_grant_reveals_no_subject() -> None:
    g = grants(("project", "newco-plan", ("read", "propose", "publish")))
    assert not g.can_use("subject", SARAH) and not g.can_propose("subject", SARAH)
    f = g.subject_filter({"Person"})
    assert not f.allows("Person", SARAH)


def test_writable_needs_the_propose_privilege_not_just_read() -> None:
    g = grants(("project", "newco-plan", ("read",)), ("subject", SARAH, ("read", "propose")))
    assert g.writable([Scope("subject", SARAH)]) == [("subject", SARAH)]
    with pytest.raises(InvalidScope):
        g.writable([Scope("project", "newco-plan")])  # read-only: it can see the project, not write into it
    with pytest.raises(InvalidScope):
        g.writable([Scope("subject", MICHAEL)])


def test_publish_must_be_held_on_every_target_scope() -> None:
    g = grants(("project", "newco-plan", ("read", "propose", "publish")), ("subject", SARAH, ("read", "propose")))
    assert g.may_publish_all([("project", "newco-plan")])
    assert not g.may_publish_all([("project", "newco-plan"), ("subject", SARAH)])
    assert not g.may_publish_all([("agent", g.self_ref)]) and not g.may_publish_all([])
    assert not g.can_publish("workspace", "*")
    assert grants(("workspace", "*", ("read", "propose", "publish"))).can_publish("workspace", "*")


def test_the_subject_filter_enforces_only_scoped_types_and_only_for_principals_without_the_grant() -> None:
    assert not grants().subject_filter(set()).enforced  # no scoped type: the P2-P6 behaviour
    f = grants(("subject", SARAH, ("read",))).subject_filter({"Person"})
    assert f.enforced
    assert f.allows("Person", SARAH) and not f.allows("Person", MICHAEL)
    assert f.allows("Organization", MICHAEL)  # an open type needs no grant


def test_a_subject_wildcard_and_the_full_view_roles_see_every_subject() -> None:
    assert not grants(("subject", "*")).subject_filter({"Person"}).enforced
    for role in (Role.WORKSPACE_ADMIN, Role.REVIEWER):
        f = grants(role=role).subject_filter({"Person"})
        assert not f.enforced and f.allows("Person", MICHAEL), role
    for role in (Role.OPERATOR, Role.APP_WRITER, Role.READER, Role.AGENT):
        assert grants(role=role).subject_filter({"Person"}).enforced, role  # no implicit grants: only admins and reviewers see by role
    assert SubjectFilter.unrestricted().allows("Person", MICHAEL)


def test_entity_sql_hides_a_frozen_entity_from_everyone_and_binds_arrays_when_a_filter_is_enforced() -> None:
    sql, params = entity_visible_sql(SubjectFilter(scoped_types=frozenset({"Person"}), full=True))
    assert (sql, params) == (
        "e.status <> 'deletion_pending'",
        {},
    )  # nothing is scoped for this caller, but a frozen subject is still gone (SEC-05)
    assert entity_visible_sql(SubjectFilter.unrestricted(), alias="x")[0] == "x.status <> 'deletion_pending'"
    sql, params = entity_visible_sql(grants(("subject", SARAH)).subject_filter({"Person", "Pet"}), alias="x", prefix="p")
    assert "x.type_key <> ALL(CAST(:p_types AS text[]))" in sql and "x.id = ANY(CAST(:p_ids AS uuid[]))" in sql
    assert "x.status <> 'deletion_pending'" in sql
    assert params == {"p_types": ["Person", "Pet"], "p_ids": [SARAH]}
    assert SARAH not in sql  # the values are bound, never in the text


def test_a_fact_needs_both_endpoints_visible() -> None:
    f = grants(("subject", SARAH)).subject_filter({"Person"})
    sql, params = fact_visible_sql(f, source="s", target="t")
    assert sql.count("<> ALL") == 2 and "s.type_key" in sql and "t.type_key" in sql and " AND " in sql
    assert "s.status <> 'deletion_pending'" in sql and "t.status <> 'deletion_pending'" in sql
    assert set(params) == {"sfs_types", "sfs_ids", "sft_types", "sft_ids"}  # the two ends bind separately: they compose into one statement
