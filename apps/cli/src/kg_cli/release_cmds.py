"""Release-hardening commands (phase 9): `kg reconcile` (the unified drift scan and safe repairs)."""

from __future__ import annotations

import asyncio
import json
from typing import Annotated

import typer
from kg_common.errors import PlatformError
from kg_common.settings import get_settings
from kg_db.engine import create_app_engine
from kg_domain.principal import WorkspaceContext

from kg_cli.seed import workspace_id_for

Workspace = Annotated[str, typer.Option(help="workspace slug")]


def reconcile_command(
    workspace: Workspace,
    scope: Annotated[str, typer.Option(help="all | graph | memory | ledger | deletions")] = "all",
    repair: Annotated[bool, typer.Option("--repair", help="apply the SAFE repairs (no model calls unless --allow-reingest)")] = False,
    allow_reingest: Annotated[
        bool,
        typer.Option(
            "--allow-reingest",
            help="with --repair: re-run graph_ingest for missing episodes (spends model calls; the cost preview is printed)",
        ),
    ] = False,
) -> None:
    """Compares the ledger with the graph, the memory store and the deletion records. Lists drift by class (ids only); with --repair applies the safe repairs.

    Exit code 0 when nothing is left, 1 when drift remains (classes that need a person or a cost confirmation remain by design)."""
    from kg_reconcile import Reconcile

    from kg_cli.main import knowledge_runtime

    settings = get_settings()

    async def run() -> int:
        engine = create_app_engine(settings, pool_size=3, max_overflow=0)
        ctx = WorkspaceContext.for_system_task(workspace_id_for(workspace), reason="cli reconcile")
        try:
            async with knowledge_runtime(engine) as (knowledge, graph):
                result = await Reconcile(engine, knowledge, graph, embedding_version=settings.memory_embedding_version).run(
                    ctx, scope=scope, repair=repair, allow_reingest=allow_reingest, trigger="manual"
                )
        finally:
            await engine.dispose()
        typer.echo(json.dumps(result.as_dict(), sort_keys=True))
        return 0 if (result.status == "completed" and not any(result.drift.values())) else 1

    try:
        raise typer.Exit(asyncio.run(run()))
    except PlatformError as exc:
        typer.echo(f"error: {exc.code}: {exc.message}", err=True)
        raise typer.Exit(1) from exc
