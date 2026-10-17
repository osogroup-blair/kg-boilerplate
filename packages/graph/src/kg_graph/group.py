"""GroupId: the Graphiti partition key. Neo4j Community has one database, so group_id IS the tenant boundary (ADR-015, GR-04)."""

from __future__ import annotations

import re
from typing import Any

from kg_domain.principal import WorkspaceContext

_VALID = re.compile(r"^ws-[A-Za-z0-9_-]+$")


def reindex_namespace(ctx: WorkspaceContext, n: int) -> str:
    """The name of the n-th rebuild of a workspace's graph: `ws-<uuid>-r<n>` (n >= 1)."""
    if n < 1 or n > 999_999:
        raise ValueError("invalid reindex number")
    return f"{ctx.group_id}-r{n}"


class GroupId:
    __slots__ = ("value",)
    value: str

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise TypeError("use GroupId.for_workspace(ctx)")

    @classmethod
    def for_workspace(cls, ctx: WorkspaceContext) -> GroupId:
        if not isinstance(ctx, WorkspaceContext):
            raise TypeError("GroupId can only be built from a WorkspaceContext")
        if not ctx.group_id or not _VALID.fullmatch(ctx.group_id):
            raise ValueError("invalid group id")
        gid = object.__new__(cls)
        object.__setattr__(gid, "value", ctx.group_id)
        return gid

    @classmethod
    def for_namespace(cls, ctx: WorkspaceContext, namespace: str) -> GroupId:
        """A named namespace of this workspace's graph (P9, Flow F): the workspace's own group or a reindex build `<group>-r<n>`. Anything else (another workspace's group,
        a free-form string) is refused, so a namespace read from the database can never point outside the workspace."""
        if not isinstance(ctx, WorkspaceContext):
            raise TypeError("GroupId can only be built from a WorkspaceContext")
        if namespace != ctx.group_id and not re.fullmatch(re.escape(ctx.group_id) + r"-r[0-9]{1,6}", namespace or ""):
            raise ValueError("namespace does not belong to this workspace")
        gid = object.__new__(cls)
        object.__setattr__(gid, "value", namespace)
        return gid

    @classmethod
    def for_simulation(cls, ctx: WorkspaceContext, run_id: str) -> GroupId:
        """The staging namespace of an ontology simulation (P8, ADR-046): the workspace's own group plus `-sim-<run>`, on the same database. It is a different group, so
        nothing the simulation writes is ever visible to a read of the real one, and it is removed when the run ends."""
        if not isinstance(ctx, WorkspaceContext):
            raise TypeError("GroupId can only be built from a WorkspaceContext")
        if not re.fullmatch(r"[A-Za-z0-9_-]{4,40}", run_id or ""):
            raise ValueError("invalid simulation run id")
        value = f"{ctx.group_id}-sim-{run_id}"
        if not _VALID.fullmatch(value):
            raise ValueError("invalid group id")
        gid = object.__new__(cls)
        object.__setattr__(gid, "value", value)
        return gid

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("GroupId is immutable")

    def __str__(self) -> str:
        return self.value

    def __repr__(self) -> str:
        return f"GroupId({self.value})"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, GroupId) and other.value == self.value

    def __hash__(self) -> int:
        return hash(self.value)
