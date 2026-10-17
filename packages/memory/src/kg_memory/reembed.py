"""Re-embedding (PV-04 path A, ADR-013): write the vectors of every live memory under a NEW embedding version while reads stay pinned to the current one.

    kg memories reembed --workspace demo --to-version 2     # resumable: only memories without a version-2 vector are touched
    (when coverage is 100%)  set MEMORY_EMBEDDING_VERSION=2 on the api and the worker; later, --drop-version 1 removes the old vectors

A read never compares vectors of different versions, so the switch is atomic from a caller's point of view, and a failure halfway leaves the old version serving."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from kg_common.ids import uuid7_str
from kg_common.settings import Settings
from kg_db.session import tenant_session
from kg_domain.principal import WorkspaceContext
from kg_governance.audit import AuditMetadata, SystemAudit
from kg_modelgw.config import ModelConfig
from kg_modelgw.gateway import Correlation, ModelGateway
from sqlalchemy import Row, text
from sqlalchemy.ext.asyncio import AsyncEngine

from kg_memory.jobs import embed_text
from kg_memory.store import vector_literal


@dataclass
class ReembedReport:
    embedded: int = 0
    skipped_by_policy: int = 0
    remaining: int = 0
    coverage: float = 0.0
    dropped: int = 0


async def reembed_workspace(
    engine: AsyncEngine,
    gateway: ModelGateway,
    config: ModelConfig,
    settings: Settings,
    workspace_id: uuid.UUID,
    *,
    to_version: int,
    batch_size: int = 32,
    max_batches: int | None = None,
    drop_version: int | None = None,
) -> ReembedReport:
    ctx = WorkspaceContext.for_system_task(workspace_id, reason="kg memories reembed")
    report = ReembedReport()
    batches = 0
    while max_batches is None or batches < max_batches:
        async with tenant_session(engine, ctx) as conn:
            rows = (
                await conn.execute(
                    text(
                        "SELECT m.id, m.summary, m.content, m.classification FROM curated_memories m WHERE m.workspace_id = :ws "
                        "AND m.status NOT IN ('rejected','deleted','deletion_pending') AND NOT EXISTS (SELECT 1 FROM memory_embeddings e WHERE e.workspace_id = m.workspace_id "
                        "AND e.memory_id = m.id AND e.embedding_version = :v) AND m.embedding_status <> 'skipped_by_policy' ORDER BY m.recorded_at, m.id LIMIT :n"
                    ),
                    {"ws": workspace_id, "v": to_version, "n": batch_size},
                )
            ).all()
        if not rows:
            break
        # one egress class per call: the strictest in the batch (a mixed batch is split by classification)
        by_class: dict[str, list[Row[Any]]] = {}
        for r in rows:
            by_class.setdefault(r.classification, []).append(r)
        for classification, group in by_class.items():
            if not config.egress_allows(classification):
                async with tenant_session(engine, ctx) as conn:
                    for r in group:
                        await conn.execute(
                            text(
                                "UPDATE curated_memories SET embedding_status = 'skipped_by_policy' WHERE workspace_id = :ws AND id = :id"
                            ),
                            {"ws": workspace_id, "id": r.id},
                        )
                report.skipped_by_policy += len(group)
                continue
            result = await gateway.embed(
                "embedding",
                [embed_text(r.summary, r.content) for r in group],
                kind="passage",
                classification=classification,
                correlation=Correlation(uuid7_str(), str(workspace_id), "", "kg-cli"),
            )
            async with tenant_session(engine, ctx) as conn:
                for r, vector in zip(group, result.vectors, strict=True):
                    await conn.execute(
                        text(
                            "INSERT INTO memory_embeddings (memory_id, embedding_version, workspace_id, embedding, embedding_model_id, embedding_dim) "
                            "VALUES (:m, :v, :ws, CAST(:e AS vector), :model, :dim) ON CONFLICT (memory_id, embedding_version) DO NOTHING"
                        ),
                        {
                            "m": r.id,
                            "v": to_version,
                            "ws": workspace_id,
                            "e": vector_literal(vector),
                            "model": config.embedding.embedding_model_id,
                            "dim": config.embedding.dim,
                        },
                    )
            report.embedded += len(group)
        batches += 1
    async with tenant_session(engine, ctx) as conn:
        counts = (
            await conn.execute(
                text(
                    "SELECT count(*) FILTER (WHERE m.embedding_status <> 'skipped_by_policy') AS wanted, "
                    "count(*) FILTER (WHERE m.embedding_status <> 'skipped_by_policy' AND e.memory_id IS NOT NULL) AS have "
                    "FROM curated_memories m LEFT JOIN memory_embeddings e ON e.workspace_id = m.workspace_id AND e.memory_id = m.id AND e.embedding_version = :v "
                    "WHERE m.workspace_id = :ws AND m.status NOT IN ('rejected','deleted','deletion_pending')"
                ),
                {"ws": workspace_id, "v": to_version},
            )
        ).one()
        report.remaining = int(counts.wanted) - int(counts.have)
        report.coverage = round(int(counts.have) / int(counts.wanted), 4) if counts.wanted else 1.0
        if drop_version is not None and report.remaining == 0:
            deleted = await conn.execute(
                text("DELETE FROM memory_embeddings WHERE workspace_id = :ws AND embedding_version = :v"),
                {"ws": workspace_id, "v": drop_version},
            )
            report.dropped = int(deleted.rowcount or 0)
        await SystemAudit(conn, workspace_id, correlation_id="cli-memories-reembed").record(
            "memory.reembedded",
            resource_type="workspace",
            resource_id=workspace_id,
            metadata=AuditMetadata(count_memories=report.embedded, count=to_version),
        )
    return report
