"""Authorization: a role -> action matrix (code-reviewed data) and a deny-by-default route policy marker.

Cross-workspace resources never reach this layer as 403: repositories filter by the caller's workspace and a miss is a 404 (no existence oracle).
"""

from __future__ import annotations

import enum
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, TypeVar

from kg_common.errors import Forbidden
from kg_domain.enums import Role
from kg_domain.principal import Principal

F = TypeVar("F", bound=Callable[..., Any])


class Action(enum.StrEnum):
    WORKSPACE_READ = "workspace.read"
    PRINCIPAL_READ = "principal.read"
    AUDIT_READ = "audit.read"
    OPERATIONS_READ = "operations.read"
    DIAGNOSTICS_RUN = "diagnostics.run"
    # P2 (phase-2 plan §6.3)
    ONTOLOGY_READ = "ontology.read"
    ONTOLOGY_WRITE = "ontology.write"
    ONTOLOGY_PUBLISH = "ontology.publish"
    ENTITY_READ = "entity.read"
    ENTITY_READ_SENSITIVE = "entity.read_sensitive"
    ENTITY_WRITE = "entity.write"
    COMMAND_READ = "command.read"
    # P3 (phase-3 plan §6.3)
    INTERACTION_WRITE = "interaction.write"
    INTERACTION_READ = "interaction.read"
    INTERACTION_READ_CONTENT = "interaction.read_content"
    OPERATIONS_REPAIR = "operations.repair"
    # P4 (phase-4 plan §6.3)
    REVIEW_READ = "review.read"
    REVIEW_DECIDE = "review.decide"
    REVIEW_READ_EVIDENCE = "review.read_evidence"
    GRAPH_SEARCH = "graph.search"
    # P5 (phase-5 plan §6.3). Scope grants further restrict every one of these: an action says WHAT a role may do, a grant says to WHOSE memory.
    MEMORY_READ = "memory.read"
    MEMORY_SAVE = "memory.save"
    MEMORY_PROPOSE = "memory.propose"
    MEMORY_WRITE_OWN = "memory.write_own"
    MEMORY_CURATE = "memory.curate"
    MEMORY_READ_PRIVATE = "memory.read_private"
    # P6 (phase-6 plan §6.3). `context.read` / `context.trace` reach only the caller's OWN builds; a workspace admin reaches every build of the workspace.
    CONTEXT_BUILD = "context.build"
    CONTEXT_READ = "context.read"
    CONTEXT_TRACE = "context.trace"
    CONTEXT_LAB = "context.lab"  # act as an agent or app principal of the same workspace (audited)
    CONTEXT_DEBUG = "context.debug"  # opt-in encrypted debug snapshots
    # P7 (phase-7 plan §6.3). Registering, granting and revoking agents and apps: workspace admins only. `principal.read` (P1) stays admin-only as well.
    PRINCIPAL_MANAGE = "principal.manage"
    # P8 (phase-8 plan §6.3). Corrections, deletion, legal holds and workspace settings. `deletion.request_subject` is for trusted-source apps (checked again in the service).
    CORRECTION_APPLY = "correction.apply"
    CORRECTION_READ = "correction.read"
    DELETION_REQUEST = "deletion.request"
    DELETION_REQUEST_SUBJECT = "deletion.request_subject"
    DELETION_READ = "deletion.read"
    GOVERNANCE_HOLD = "governance.hold"
    WORKSPACE_ADMIN = "workspace.admin"
    # P9 (phase-9 plan §6). Cost views for operators and admins; reindexing and the OIDC group mapping are admin only. `operations.repair` is reused for reconcile repairs.
    OPERATIONS_COST_READ = "operations.cost_read"
    REINDEX_RUN = "reindex.run"
    OIDC_MANAGE = "oidc.manage"


# The matrix (phase-1 plan §6.3, extended by phase-2 and phase-3 plan §6.3). Later phases add actions; they never widen an existing cell silently
# (test_policy asserts the whole table, so every change shows up in review).
_READ_KNOWLEDGE = frozenset({Action.WORKSPACE_READ, Action.ONTOLOGY_READ, Action.ENTITY_READ, Action.MEMORY_READ})
ROLE_ACTIONS: dict[Role, frozenset[Action]] = {
    Role.WORKSPACE_ADMIN: frozenset(Action),
    Role.OPERATOR: _READ_KNOWLEDGE
    | {
        Action.OPERATIONS_READ,
        Action.DIAGNOSTICS_RUN,
        Action.COMMAND_READ,
        Action.INTERACTION_READ,
        Action.OPERATIONS_REPAIR,
        Action.GRAPH_SEARCH,
        Action.CORRECTION_READ,
        Action.DELETION_READ,
        Action.OPERATIONS_COST_READ,
    },
    Role.REVIEWER: _READ_KNOWLEDGE
    | {
        Action.COMMAND_READ,
        Action.INTERACTION_READ,
        Action.REVIEW_READ,
        Action.REVIEW_DECIDE,
        Action.REVIEW_READ_EVIDENCE,
        Action.GRAPH_SEARCH,
        Action.MEMORY_CURATE,
        Action.CONTEXT_BUILD,
        Action.CONTEXT_READ,
        Action.CONTEXT_TRACE,
        Action.CORRECTION_APPLY,
        Action.CORRECTION_READ,
    },
    Role.APP_WRITER: _READ_KNOWLEDGE
    | {
        Action.ENTITY_WRITE,
        Action.COMMAND_READ,
        Action.INTERACTION_WRITE,
        Action.INTERACTION_READ,
        Action.MEMORY_SAVE,
        Action.MEMORY_PROPOSE,
        Action.MEMORY_WRITE_OWN,
        Action.CONTEXT_BUILD,
        Action.CONTEXT_READ,
        Action.CONTEXT_TRACE,
        Action.DELETION_REQUEST_SUBJECT,
        Action.DELETION_READ,
    },
    # an agent reads only the interactions it submitted itself (an ownership predicate in the query; P7 generalises it to scopes)
    Role.AGENT: _READ_KNOWLEDGE
    | {
        Action.INTERACTION_WRITE,
        Action.INTERACTION_READ,
        Action.MEMORY_PROPOSE,
        Action.MEMORY_WRITE_OWN,
        Action.CONTEXT_BUILD,
        Action.CONTEXT_READ,
        Action.CONTEXT_TRACE,
    },
    Role.READER: _READ_KNOWLEDGE,
}
# Roles that see every interaction of the workspace; any other role holding interaction.read sees only its own submissions.
INTERACTION_READ_ALL_ROLES = frozenset({Role.WORKSPACE_ADMIN, Role.OPERATOR, Role.REVIEWER, Role.APP_WRITER})


def allowed_actions(roles: Iterable[Role]) -> frozenset[Action]:
    out: set[Action] = set()
    for role in roles:
        out |= ROLE_ACTIONS.get(role, frozenset())
    return frozenset(out)


def is_allowed(principal: Principal, action: Action) -> bool:
    return action in allowed_actions(principal.roles)


def enforce(principal: Principal, action: Action) -> None:
    if not is_allowed(principal, action):
        raise Forbidden("Action not permitted", details={"action": action.value})


@dataclass(frozen=True)
class RoutePolicy:
    kind: str  # "requires" | "authenticated" | "public"
    action: Action | None = None
    reason: str = ""


_ATTR = "__kg_policy__"


def requires(action: Action) -> Callable[[F], F]:
    def mark(fn: F) -> F:
        setattr(fn, _ATTR, RoutePolicy("requires", action))
        return fn

    return mark


def authenticated(reason: str) -> Callable[[F], F]:
    """Any authenticated principal; no action needed (e.g. 'who am I')."""

    def mark(fn: F) -> F:
        setattr(fn, _ATTR, RoutePolicy("authenticated", reason=reason))
        return fn

    return mark


def public(reason: str) -> Callable[[F], F]:
    if not reason:
        raise ValueError("a public route must state why")

    def mark(fn: F) -> F:
        setattr(fn, _ATTR, RoutePolicy("public", reason=reason))
        return fn

    return mark


def policy_of(endpoint: Callable[..., Any] | None) -> RoutePolicy | None:
    return getattr(endpoint, _ATTR, None)


EXEMPT_PATHS = frozenset({"/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json"})


def unpoliced_routes(routes: Iterable[Any]) -> list[str]:
    """Paths of routes with an endpoint but no policy marker (deny by default). Framework doc routes are exempt."""
    missing = []
    for route in routes:
        endpoint = getattr(route, "endpoint", None)
        path = getattr(route, "path", "")
        if endpoint is None or path in EXEMPT_PATHS:
            continue
        if policy_of(endpoint) is None:
            missing.append(path)
    return sorted(missing)
