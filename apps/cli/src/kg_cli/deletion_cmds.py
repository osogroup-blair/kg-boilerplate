"""`kg deletions` (phase 8): operator access to deletion requests, for the cases the API cannot serve (a suspended workspace has no credentials) and for checks that belong to the
platform rather than a tenant (the out-of-band journal). Everything goes through the same `DeletionService` the API and the worker use."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import uuid
from collections.abc import AsyncIterator, Coroutine
from contextlib import asynccontextmanager
from typing import Annotated, Any

import typer
from kg_common.errors import PlatformError
from kg_common.settings import Settings, get_settings
from kg_db.engine import create_app_engine
from kg_db.session import tenant_session
from kg_domain.principal import WorkspaceContext
from kg_governance.audit import SystemAudit
from kg_governance.deletion.journal import DeletionJournal, JournalCorrupt
from kg_graph.service import GraphService
from kg_ingestion.crypto import ContentHasher, PayloadCipher
from kg_memory.deletion import DeletionServices, RetentionSweeper, build_deletion
from kg_memory.services import build_memory
from kg_modelgw.calllog import DbCallSink
from kg_modelgw.factory import build_router_stack
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from kg_cli.seed import workspace_id_for

deletions_app = typer.Typer(
    no_args_is_help=True,
    help="Deletion requests: list, show, cancel a workspace deletion, retry a failed one, verify the journal, sweep retention",
)
Workspace = Annotated[str, typer.Option(help="workspace slug")]


def _run(coro: Coroutine[Any, Any, int]) -> None:
    try:
        raise typer.Exit(asyncio.run(coro))
    except PlatformError as exc:
        typer.echo(f"error: {exc.code}: {exc.message}", err=True)
        raise typer.Exit(1) from exc


@asynccontextmanager
async def deletion_runtime(
    engine: AsyncEngine, settings: Settings, *, with_graph: bool = False
) -> AsyncIterator[tuple[DeletionServices, GraphService | None]]:
    from kg_cli.main import build_knowledge_offline

    knowledge = build_knowledge_offline(engine)
    memory = build_memory(settings, knowledge, None, _config(settings), service="kg-cli")
    cipher, hasher = PayloadCipher.from_settings(settings), ContentHasher.from_settings(settings)
    if not with_graph:
        yield build_deletion(settings, engine, knowledge, None, cipher, hasher, memory.writer), None
        return
    sink = DbCallSink(engine)
    router = build_router_stack(settings, service="kg-worker", sink=sink)
    graph = GraphService(settings, router.transport, router.config)
    sink.start()
    try:
        from kg_knowledge.services import build_knowledge

        full = build_knowledge(engine, graph, router.config, service="kg-worker", settings=settings)
        memory = build_memory(settings, full, router.gateway, router.config, service="kg-worker")
        yield build_deletion(settings, engine, full, graph, cipher, hasher, memory.writer), graph
    finally:
        await sink.aclose()
        await graph.aclose()
        await router.aclose()


def _config(settings: Settings):  # type: ignore[no-untyped-def]
    from kg_modelgw.config import load_config

    return load_config(settings.model_purposes_path, settings.pricing_path)


@deletions_app.command("list")
def deletions_list(workspace: Workspace, status: str | None = None) -> None:
    """Every deletion request of the workspace, newest first."""

    async def go() -> int:
        s = get_settings()
        engine = create_app_engine(s)
        try:
            async with deletion_runtime(engine, s) as (d, _):
                ctx = WorkspaceContext.for_system_task(workspace_id_for(workspace), reason="cli deletions list")
                async with tenant_session(engine, ctx) as conn:
                    for r in await d.service.list_requests(conn, ctx.workspace_id, status=status):
                        typer.echo(f"{r.request_id}  {r.kind:<14} {r.status:<26} {r.received_at.isoformat()}")
            return 0
        finally:
            await engine.dispose()

    _run(go())


@deletions_app.command("show")
def deletions_show(workspace: Workspace, request_id: uuid.UUID) -> None:
    """One request: state, plan counts and the receipt."""

    async def go() -> int:
        s = get_settings()
        engine = create_app_engine(s)
        try:
            async with deletion_runtime(engine, s) as (d, _):
                ctx = WorkspaceContext.for_system_task(workspace_id_for(workspace), reason="cli deletions show")
                async with tenant_session(engine, ctx) as conn:
                    typer.echo(json.dumps((await d.service.get(conn, ctx.workspace_id, request_id)).model_dump(mode="json"), indent=2))
            return 0
        finally:
            await engine.dispose()

    _run(go())


@deletions_app.command("cancel")
def deletions_cancel(workspace: Workspace, request_id: uuid.UUID) -> None:
    """Cancels a workspace deletion that is still cooling off, and makes the workspace active again (a suspended workspace cannot do this itself)."""

    async def go() -> int:
        s = get_settings()
        engine = create_app_engine(s)
        try:
            async with deletion_runtime(engine, s) as (d, _):
                ctx = WorkspaceContext.for_system_task(workspace_id_for(workspace), reason="cli deletions cancel")
                async with tenant_session(engine, ctx) as conn:
                    out = await d.service.cancel(conn, ctx.workspace_id, request_id, SystemAudit(conn, ctx.workspace_id))
                typer.echo(f"{out.request_id} {out.status}")
            return 0
        finally:
            await engine.dispose()

    _run(go())


@deletions_app.command("retry")
def deletions_retry(workspace: Workspace, request_id: uuid.UUID) -> None:
    """Resumes a failed request from the step it failed in (the steps are idempotent)."""

    async def go() -> int:
        s = get_settings()
        engine = create_app_engine(s)
        try:
            async with deletion_runtime(engine, s) as (d, _):
                ctx = WorkspaceContext.for_system_task(workspace_id_for(workspace), reason="cli deletions retry")
                async with tenant_session(engine, ctx) as conn:
                    out = await d.service.retry(conn, ctx.workspace_id, request_id, SystemAudit(conn, ctx.workspace_id))
                typer.echo(f"{out.request_id} {out.status}")
            return 0
        finally:
            await engine.dispose()

    _run(go())


@deletions_app.command("journal")
def deletions_journal(
    since: Annotated[str | None, typer.Option(help="ISO time: print what a restore from a backup taken then must re-apply")] = None,
) -> None:
    """Verifies the out-of-band deletion journal's chain; with --since, lists the lines a restore must re-apply (P9)."""
    s = get_settings()
    from pathlib import Path

    from kg_common.settings import REPO_ROOT

    journal = DeletionJournal(
        Path(s.deletion_journal_dir) if s.deletion_journal_dir else REPO_ROOT / ".local" / "deletion-journal",
        s.content_hmac_ring(),
    )
    try:
        lines = journal.verify()
    except JournalCorrupt as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"journal ok: {lines} lines ({journal.path})")
    if since:
        for entry in journal.since(dt.datetime.fromisoformat(since).astimezone(dt.UTC)):
            typer.echo(json.dumps(entry))


@deletions_app.command("reapply")
def deletions_reapply(
    since: Annotated[
        str, typer.Option(help="ISO time of the backup that was restored: every journal line completed at or after it is applied again")
    ],
    workspace: Annotated[str | None, typer.Option(help="only this workspace slug")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="verify the journal and list what would be removed; change nothing")] = False,
    graph: Annotated[bool, typer.Option("--graph/--no-graph", help="also purge and scan Neo4j (needs the graph up; the default)")] = True,
) -> None:
    """After a restore: verify the deletion journal's HMAC chain, then re-apply every deletion completed since the backup (P9, ADR-045).

    Idempotent, and it only ever removes data. Exit 0 when every line is applied (or was already), 1 when one needs a person, 2 when the journal does not verify."""
    when = dt.datetime.fromisoformat(since)
    when = when.astimezone(dt.UTC) if when.tzinfo else when.replace(tzinfo=dt.UTC)

    async def go() -> int:
        from kg_governance.deletion.reapply import Reapplier

        s = get_settings()
        engine = create_app_engine(s)
        try:
            async with deletion_runtime(engine, s, with_graph=graph) as (d, _):
                try:
                    results = await Reapplier(d.service, d.journal, engine).reapply(
                        when, dry_run=dry_run, workspace_id=workspace_id_for(workspace) if workspace else None
                    )
                except JournalCorrupt as exc:
                    typer.echo(f"error: {exc}", err=True)
                    return 2
            for r in results:
                typer.echo(json.dumps(r.as_dict(), sort_keys=True))
            attention = [r for r in results if r.outcome == "attention"]
            typer.echo(json.dumps({"lines": len(results), "attention": len(attention), "dry_run": dry_run}, sort_keys=True))
            return 1 if attention else 0
        finally:
            await engine.dispose()

    _run(go())


@deletions_app.command("retention-sweep")
def deletions_retention_sweep() -> None:
    """Runs the retention sweep once, for every workspace that has something expired (the worker does this on a timer)."""

    async def go() -> int:
        s = get_settings()
        engine = create_app_engine(s)
        try:
            async with deletion_runtime(engine, s, with_graph=True) as (d, graph):
                done = await RetentionSweeper(engine, d.service, graph).sweep()
                typer.echo(json.dumps(done, sort_keys=True))
            return 0
        finally:
            await engine.dispose()

    _run(go())


_ = text
