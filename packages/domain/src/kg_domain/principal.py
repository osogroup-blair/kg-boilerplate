from __future__ import annotations

import uuid
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict

from kg_domain.enums import PrincipalKind, Role


class AuthMethod(StrEnum):
    API_KEY = "api_key"
    JWT = "jwt"
    OIDC = "oidc"


class Principal(BaseModel):
    """The authenticated caller. Built only by kg_governance.authn from a key record or verified token, never from request input."""

    model_config = ConfigDict(frozen=True)

    principal_id: uuid.UUID
    workspace_id: uuid.UUID
    kind: PrincipalKind
    handle: str
    roles: frozenset[Role]
    key_id: uuid.UUID | None = None
    auth_method: AuthMethod = AuthMethod.API_KEY


_SENTINEL = object()


class WorkspaceContext:
    """Tenant binding for DB sessions and the graph adapter. Obtainable only from a Principal (`of`) or, for system tasks (seed, worker),
    via the explicitly named `for_system_task`; there is no constructor taking a bare string or an arbitrary request value."""

    __slots__ = ("group_id", "workspace_id")

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise TypeError("use WorkspaceContext.of(principal)")

    workspace_id: uuid.UUID
    group_id: str

    @classmethod
    def _make(cls, workspace_id: uuid.UUID) -> WorkspaceContext:
        if not isinstance(workspace_id, uuid.UUID):
            raise TypeError("workspace_id must be a UUID")
        ctx = object.__new__(cls)
        object.__setattr__(ctx, "workspace_id", workspace_id)
        object.__setattr__(ctx, "group_id", f"ws-{workspace_id}")
        return ctx

    @classmethod
    def of(cls, principal: Principal) -> WorkspaceContext:
        return cls._make(principal.workspace_id)

    @classmethod
    def for_system_task(cls, workspace_id: uuid.UUID, *, reason: str) -> WorkspaceContext:
        """Explicit escape hatch for seed / maintenance code that acts on a known workspace. `reason` documents the call site."""
        if not reason:
            raise ValueError("reason is required")
        return cls._make(workspace_id)

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("WorkspaceContext is immutable")

    def __repr__(self) -> str:
        return f"WorkspaceContext({self.workspace_id})"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, WorkspaceContext) and other.workspace_id == self.workspace_id

    def __hash__(self) -> int:
        return hash(self.workspace_id)
