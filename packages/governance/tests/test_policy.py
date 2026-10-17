import uuid

import pytest
from kg_common.errors import Forbidden
from kg_domain import Principal, PrincipalKind, Role
from kg_governance.authz.policy import (
    ROLE_ACTIONS,
    Action,
    authenticated,
    enforce,
    is_allowed,
    policy_of,
    public,
    requires,
    unpoliced_routes,
)

pytestmark = pytest.mark.unit

# The documented matrix (phase-1 plan §6.3 + phase-2 plan §6.3 + phase-3 plan §6.3 + phase-4 plan §6.3 + phase-5 plan §6.3 + phase-6 plan §6.3). If this table has to change, the plan
# and the review have to change with it. No P1 cell changed in P2, no P1/P2 cell in P3, no P1-P3 cell in P4, no P1-P4 cell in P5 and no P1-P5 cell in P6: actions were only added.
P1 = {
    Role.WORKSPACE_ADMIN: {"workspace.read", "principal.read", "audit.read", "operations.read", "diagnostics.run"},
    Role.OPERATOR: {"workspace.read", "operations.read", "diagnostics.run"},
    Role.REVIEWER: {"workspace.read"},
    Role.APP_WRITER: {"workspace.read"},
    Role.AGENT: {"workspace.read"},
    Role.READER: {"workspace.read"},
}
P2 = {
    Role.WORKSPACE_ADMIN: {
        "ontology.read",
        "ontology.write",
        "ontology.publish",
        "entity.read",
        "entity.read_sensitive",
        "entity.write",
        "command.read",
    },
    Role.OPERATOR: {"ontology.read", "entity.read", "command.read"},
    Role.REVIEWER: {"ontology.read", "entity.read", "command.read"},
    Role.APP_WRITER: {"ontology.read", "entity.read", "entity.write", "command.read"},
    Role.AGENT: {"ontology.read", "entity.read"},
    Role.READER: {"ontology.read", "entity.read"},
}
P3 = {
    Role.WORKSPACE_ADMIN: {"interaction.write", "interaction.read", "interaction.read_content", "operations.repair"},
    Role.OPERATOR: {"interaction.read", "operations.repair"},
    Role.REVIEWER: {"interaction.read"},
    Role.APP_WRITER: {"interaction.write", "interaction.read"},
    Role.AGENT: {"interaction.write", "interaction.read"},  # an agent reads only its OWN submissions (ownership predicate in the query)
    Role.READER: set(),
}
P4 = {
    Role.WORKSPACE_ADMIN: {"review.read", "review.decide", "review.read_evidence", "graph.search"},
    Role.OPERATOR: {"graph.search"},
    Role.REVIEWER: {"review.read", "review.decide", "review.read_evidence", "graph.search"},
    Role.APP_WRITER: set(),
    Role.AGENT: set(),  # agents get graph-backed context through P6, never raw graph search
    Role.READER: set(),
}
# P5 (phase-5 plan §6.3): memory actions only; every cell is a pure addition. Scope grants (kg_memory) restrict WHOSE memory each may touch.
P5 = {
    Role.WORKSPACE_ADMIN: {"memory.read", "memory.save", "memory.propose", "memory.write_own", "memory.curate", "memory.read_private"},
    Role.OPERATOR: {"memory.read"},
    Role.REVIEWER: {"memory.read", "memory.curate"},
    Role.APP_WRITER: {"memory.read", "memory.save", "memory.propose", "memory.write_own"},
    Role.AGENT: {"memory.read", "memory.propose", "memory.write_own"},
    Role.READER: {"memory.read"},
}
# P6 (phase-6 plan §6.3): context actions only. Building and reading one's own builds is for roles that consume context; the Lab (act-as) and debug snapshots are admin-only.
P6 = {
    Role.WORKSPACE_ADMIN: {"context.build", "context.read", "context.trace", "context.lab", "context.debug"},
    Role.OPERATOR: set(),
    Role.REVIEWER: {"context.build", "context.read", "context.trace"},
    Role.APP_WRITER: {"context.build", "context.read", "context.trace"},
    Role.AGENT: {"context.build", "context.read", "context.trace"},
    Role.READER: set(),
}
# P7 (phase-7 plan §6.3): registering, granting and revoking agents and apps is a workspace admin's act and nobody else's.
P7 = {
    Role.WORKSPACE_ADMIN: {"principal.manage"},
    Role.OPERATOR: set(),
    Role.REVIEWER: set(),
    Role.APP_WRITER: set(),
    Role.AGENT: set(),
    Role.READER: set(),
}
# P8 (phase-8 plan §6.3): corrections (admin, reviewer), deletion (admin for every kind; a trusted-source app for a subject or an interaction, checked again in the service),
# legal holds and workspace settings (admin only). Reviewers request a MEMORY deletion through memory.curate, which they already hold.
P8 = {
    Role.WORKSPACE_ADMIN: {
        "correction.apply",
        "correction.read",
        "deletion.request",
        "deletion.request_subject",
        "deletion.read",
        "governance.hold",
        "workspace.admin",
    },
    Role.OPERATOR: {"correction.read", "deletion.read"},
    Role.REVIEWER: {"correction.apply", "correction.read"},
    Role.APP_WRITER: {"deletion.request_subject", "deletion.read"},
    Role.AGENT: set(),
    Role.READER: set(),
}
EXPECTED = {role: P1[role] | P2[role] | P3[role] | P4[role] | P5[role] | P6[role] | P7[role] | P8[role] for role in Role}


def _p(*roles: Role) -> Principal:
    return Principal(principal_id=uuid.uuid4(), workspace_id=uuid.uuid4(), kind=PrincipalKind.USER, handle="h", roles=frozenset(roles))


def test_role_action_matrix_is_exactly_the_documented_one():
    assert {r: {a.value for a in acts} for r, acts in ROLE_ACTIONS.items()} == EXPECTED
    assert set(ROLE_ACTIONS) == set(Role)


@pytest.mark.parametrize("role", list(Role))
@pytest.mark.parametrize("action", list(Action))
def test_each_role_times_action(role: Role, action: Action):
    assert is_allowed(_p(role), action) == (action.value in EXPECTED[role])
    if action.value in EXPECTED[role]:
        enforce(_p(role), action)
    else:
        with pytest.raises(Forbidden):
            enforce(_p(role), action)


def test_earlier_phase_cells_did_not_widen():
    for role, p1 in P1.items():
        have = {a.value for a in ROLE_ACTIONS[role]}
        assert p1 <= have and (P1[role] | P2[role]) <= have and (P1[role] | P2[role] | P3[role]) <= have
        assert have - p1 == P2[role] | P3[role] | P4[role] | P5[role] | P6[role] | P7[role] | P8[role]
        assert have - (P1[role] | P2[role]) == P3[role] | P4[role] | P5[role] | P6[role] | P7[role] | P8[role]
        assert have - (P1[role] | P2[role] | P3[role]) == P4[role] | P5[role] | P6[role] | P7[role] | P8[role]
        assert have - (P1[role] | P2[role] | P3[role] | P4[role]) == P5[role] | P6[role] | P7[role] | P8[role]
        assert have - (P1[role] | P2[role] | P3[role] | P4[role] | P5[role]) == P6[role] | P7[role] | P8[role]
        assert have - (P1[role] | P2[role] | P3[role] | P4[role] | P5[role] | P6[role]) == P7[role] | P8[role]
        assert have - (P1[role] | P2[role] | P3[role] | P4[role] | P5[role] | P6[role] | P7[role]) == P8[role]


def test_only_admins_and_reviewers_decide_reviews_and_read_their_evidence():
    for action in (Action.REVIEW_READ, Action.REVIEW_DECIDE, Action.REVIEW_READ_EVIDENCE):
        assert {r for r, acts in ROLE_ACTIONS.items() if action in acts} == {Role.WORKSPACE_ADMIN, Role.REVIEWER}
    assert {r for r, acts in ROLE_ACTIONS.items() if Action.GRAPH_SEARCH in acts} == {Role.WORKSPACE_ADMIN, Role.OPERATOR, Role.REVIEWER}


def test_only_admins_read_decrypted_content_and_only_admins_and_operators_repair():
    assert [r for r, acts in ROLE_ACTIONS.items() if Action.INTERACTION_READ_CONTENT in acts] == [Role.WORKSPACE_ADMIN]
    assert {r for r, acts in ROLE_ACTIONS.items() if Action.OPERATIONS_REPAIR in acts} == {Role.WORKSPACE_ADMIN, Role.OPERATOR}
    assert Role.READER not in {r for r, acts in ROLE_ACTIONS.items() if Action.INTERACTION_READ in acts}


def test_agents_read_but_never_write_commands_and_only_admins_see_sensitive_attributes():
    assert Action.ENTITY_WRITE not in ROLE_ACTIONS[Role.AGENT] and Action.ENTITY_READ in ROLE_ACTIONS[Role.AGENT]
    assert [r for r, acts in ROLE_ACTIONS.items() if Action.ENTITY_READ_SENSITIVE in acts] == [Role.WORKSPACE_ADMIN]
    assert [r for r, acts in ROLE_ACTIONS.items() if Action.ONTOLOGY_PUBLISH in acts] == [Role.WORKSPACE_ADMIN]


def test_memory_actions_p5():
    assert [r for r, acts in ROLE_ACTIONS.items() if Action.MEMORY_READ_PRIVATE in acts] == [Role.WORKSPACE_ADMIN]
    assert {r for r, acts in ROLE_ACTIONS.items() if Action.MEMORY_CURATE in acts} == {Role.WORKSPACE_ADMIN, Role.REVIEWER}
    assert {r for r, acts in ROLE_ACTIONS.items() if Action.MEMORY_SAVE in acts} == {Role.WORKSPACE_ADMIN, Role.APP_WRITER}
    # an agent proposes and keeps its own notes; it can never save shared memory, curate it or decide a review (AT-14)
    assert Action.MEMORY_SAVE not in ROLE_ACTIONS[Role.AGENT] and Action.MEMORY_CURATE not in ROLE_ACTIONS[Role.AGENT]
    assert Action.REVIEW_DECIDE not in ROLE_ACTIONS[Role.AGENT]
    assert all(Action.MEMORY_READ in acts for acts in ROLE_ACTIONS.values())


def test_no_roles_means_no_actions():
    assert not any(is_allowed(_p(), a) for a in Action)


def test_policy_markers_and_registry():
    @requires(Action.AUDIT_READ)
    def a(): ...

    @public("liveness")
    def b(): ...

    @authenticated("who am I")
    def c(): ...

    def naked(): ...

    assert policy_of(a).action is Action.AUDIT_READ and policy_of(b).kind == "public" and policy_of(c).kind == "authenticated"

    class R:
        def __init__(self, path, endpoint):
            self.path, self.endpoint = path, endpoint

    assert unpoliced_routes([R("/x", a), R("/y", naked), R("/openapi.json", naked), R("/mount", None)]) == ["/y"]
    with pytest.raises(ValueError):
        public("")


def test_context_actions_p6():
    assert {r for r, acts in ROLE_ACTIONS.items() if Action.CONTEXT_BUILD in acts} == {
        Role.WORKSPACE_ADMIN,
        Role.REVIEWER,
        Role.APP_WRITER,
        Role.AGENT,
    }
    assert [r for r, acts in ROLE_ACTIONS.items() if Action.CONTEXT_LAB in acts] == [Role.WORKSPACE_ADMIN]  # act-as is an admin power
    assert [r for r, acts in ROLE_ACTIONS.items() if Action.CONTEXT_DEBUG in acts] == [Role.WORKSPACE_ADMIN]
    assert Role.READER not in {r for r, acts in ROLE_ACTIONS.items() if Action.CONTEXT_BUILD in acts}


def test_principal_management_is_an_admin_only_power_p7():
    assert [r for r, acts in ROLE_ACTIONS.items() if Action.PRINCIPAL_MANAGE in acts] == [Role.WORKSPACE_ADMIN]
    assert [r for r, acts in ROLE_ACTIONS.items() if Action.PRINCIPAL_READ in acts] == [Role.WORKSPACE_ADMIN]
