"""Test-only projection driver. The P2 sweeper is gone (P4 WP0); tests that need "project whatever is due now, honouring backoff" use this."""

from __future__ import annotations

import uuid

from kg_db.session import tenant_session
from kg_domain.principal import WorkspaceContext
from sqlalchemy import text


async def project_due(api, ws: uuid.UUID) -> int:  # type: ignore[no-untyped-def]
    """Projects the workspace's pending, due entity and fact rows once (entities first). Returns how many rows it touched."""
    ctx = WorkspaceContext.for_system_task(ws, reason="test projection driver")
    projector = api.app.state.kg.knowledge.projector
    async with tenant_session(api.engine, ctx) as c:
        entities = [
            r.id
            for r in (
                await c.execute(
                    text(
                        "SELECT id FROM entities WHERE workspace_id = :w AND projection_status = 'pending' AND projection_next_at <= now() ORDER BY recorded_at, id"
                    ),
                    {"w": ws},
                )
            ).all()
        ]
        facts = [
            r.id
            for r in (
                await c.execute(
                    text(
                        "SELECT id FROM facts WHERE workspace_id = :w AND projection_status = 'pending' AND projection_next_at <= now() ORDER BY recorded_at, id"
                    ),
                    {"w": ws},
                )
            ).all()
        ]
    for eid in entities:
        await projector.project_entity(ctx, eid)
    for fid in facts:
        await projector.project_fact(ctx, fid)
    if entities or facts:  # what the sweeper also did: derive each waiting command's status from its rows
        async with tenant_session(api.engine, ctx) as c:
            waiting = [
                r.id
                for r in (
                    await c.execute(
                        text(
                            "SELECT id FROM commands WHERE workspace_id = :w AND status = 'applied' AND projection_status IN ('pending','failed') ORDER BY created_at LIMIT 100"
                        ),
                        {"w": ws},
                    )
                ).all()
            ]
        for cid in waiting:
            await projector.refresh_command(ctx, cid)
    return len(entities) + len(facts)
