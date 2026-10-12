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

# The documented matrix (phase-1 plan §6.3 + phase-2 plan §6.3 + phase-3 plan §6.3 + phase-4 plan §6.3). If this table has to change, the plan and the review
# have to change with it. No P1 cell changed in P2, no P1/P2 cell in P3 and no P1-P3 cell in P4: actions were only added.
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
EXPECTED = {role: P1[role] | P2[role] | P3[role] | P4[role] for role in Role}


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
        assert have - p1 == P2[role] | P3[role] | P4[role]
        assert have - (P1[role] | P2[role]) == P3[role] | P4[role]
        assert have - (P1[role] | P2[role] | P3[role]) == P4[role]


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
