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


# The matrix (phase-1 plan §6.3, extended by phase-2 and phase-3 plan §6.3). Later phases add actions; they never widen an existing cell silently
# (test_policy asserts the whole table, so every change shows up in review).
_READ_KNOWLEDGE = frozenset({Action.WORKSPACE_READ, Action.ONTOLOGY_READ, Action.ENTITY_READ})
ROLE_ACTIONS: dict[Role, frozenset[Action]] = {
    Role.WORKSPACE_ADMIN: frozenset(Action),
    Role.OPERATOR: _READ_KNOWLEDGE
    | {Action.OPERATIONS_READ, Action.DIAGNOSTICS_RUN, Action.COMMAND_READ, Action.INTERACTION_READ, Action.OPERATIONS_REPAIR},
    Role.REVIEWER: _READ_KNOWLEDGE | {Action.COMMAND_READ, Action.INTERACTION_READ},
    Role.APP_WRITER: _READ_KNOWLEDGE | {Action.ENTITY_WRITE, Action.COMMAND_READ, Action.INTERACTION_WRITE, Action.INTERACTION_READ},
    # an agent reads only the interactions it submitted itself (an ownership predicate in the query; P7 generalises it to scopes)
    Role.AGENT: _READ_KNOWLEDGE | {Action.INTERACTION_WRITE, Action.INTERACTION_READ},
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
