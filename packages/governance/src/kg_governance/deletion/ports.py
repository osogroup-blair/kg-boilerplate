"""What the deletion package asks of the rest of the platform (it imports none of it: kg_governance sits below the adapters)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Protocol

from kg_domain.principal import WorkspaceContext
from sqlalchemy.ext.asyncio import AsyncConnection


class ManifestCipher(Protocol):
    """Encrypts the deletion manifest with the payload keyring, bound to (workspace, request) so a copy elsewhere does not open."""

    def seal(self, plaintext: bytes, *, workspace_id: uuid.UUID, request_id: uuid.UUID) -> tuple[str, bytes, bytes]:
        """(kid, nonce, ciphertext)."""
        ...

    def open(self, kid: str, nonce: bytes, ciphertext: bytes, *, workspace_id: uuid.UUID, request_id: uuid.UUID) -> bytes: ...


class JobEnqueuer(Protocol):
    """Enqueues the next stage of a deletion on the caller's connection (the outbox write is part of the transaction)."""

    async def __call__(
        self,
        conn: AsyncConnection,
        workspace_id: uuid.UUID,
        *,
        stage: str,
        request_id: uuid.UUID,
        generation: int = 0,
        delay_seconds: float = 0.0,
    ) -> None: ...


class RefHasher(Protocol):
    def __call__(self, workspace_id: uuid.UUID, ref_type: str, system: str, value: str) -> str: ...


@dataclass
class GraphListing:
    """What the graph holds that belongs to a deletion: ids to remove, and neighbours whose derived text may cite the subject."""

    episode_uuids: list[str] = field(default_factory=list)
    node_uuids: list[str] = field(default_factory=list)
    edge_uuids: list[str] = field(default_factory=list)
    summary_nodes: list[str] = field(
        default_factory=list
    )  # surviving nodes whose summary mentions a manifest term: cleared, not regenerated


@dataclass
class GraphResidual:
    location: str  # a label such as "node.name" / "edge.fact" / "episode.content": never the text
    count: int = 1


@dataclass
class GraphPurgeCounts:
    episodes: int = 0
    nodes: int = 0
    edges: int = 0
    summaries_cleared: int = 0
    dangling_pruned: int = 0
    orphans_swept: int = 0
    reprojected: int = 0


class GraphPurgePort(Protocol):
    """The ONLY deletion code that touches the graph (implemented in kg_knowledge.graph_purge, registered by the composition root)."""

    async def list_for(
        self,
        ctx: WorkspaceContext,
        *,
        episode_uuids: list[str],
        node_uuids: list[str],
        edge_uuids: list[str],
        terms: list[str],
    ) -> GraphListing: ...

    async def purge(
        self,
        ctx: WorkspaceContext,
        *,
        episode_uuids: list[str],
        node_uuids: list[str],
        edge_uuids: list[str],
        summary_nodes: list[str],
        reproject_nodes: list[str],
        reproject_edges: list[str],
    ) -> GraphPurgeCounts:
        """ADR-018 (b)-(f), idempotent: running it again after a crash finishes the work. `reproject_*` are survivors the removal may have damaged; they are made equal to
        Postgres again from the final Postgres state."""
        ...

    async def scan(self, ctx: WorkspaceContext, *, terms: list[str], ids: list[str]) -> list[GraphResidual]:
        """ADR-018 (g): nothing in this workspace's group still holds a term or an id."""
        ...

    async def clear_workspace(self, ctx: WorkspaceContext) -> int:
        """`clear_data([group])` for a workspace deletion."""
        ...
