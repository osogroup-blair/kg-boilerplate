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


# ---- kg reindex (Flow F) ------------------------------------------------------------------------------------------------------------------------------
reindex_app = typer.Typer(no_args_is_help=True, help="Rebuild a workspace's graph in a separate namespace and cut over (or roll back)")
As = Annotated[str, typer.Option("--as", help="handle of the workspace admin the action is recorded as (e.g. admin@demo)")]


def _principal(workspace: str, handle: str):  # type: ignore[no-untyped-def]
    from kg_domain.enums import PrincipalKind, Role
    from kg_domain.principal import Principal

    return Principal(
        principal_id=__import__("uuid").uuid4(),
        workspace_id=workspace_id_for(workspace),
        kind=PrincipalKind.USER,
        handle=handle,
        roles=frozenset({Role.WORKSPACE_ADMIN}),
    )


async def _with_service(workspace: str, handle: str | None, fn):  # type: ignore[no-untyped-def]
    from kg_db.session import tenant_session
    from kg_governance.audit import SystemAudit
    from kg_governance.repositories import admin
    from kg_ingestion.crypto import PayloadCipher
    from kg_modelgw.config import load_config
    from kg_reconcile import ReindexService

    from kg_cli.main import knowledge_runtime

    settings = get_settings()
    engine = create_app_engine(settings, pool_size=3, max_overflow=0)
    ctx = WorkspaceContext.for_system_task(workspace_id_for(workspace), reason="cli reindex")
    try:
        async with knowledge_runtime(engine) as (knowledge, graph):
            svc = ReindexService(
                engine,
                graph,
                knowledge,
                PayloadCipher.from_settings(settings),
                load_config(settings.model_purposes_path, settings.pricing_path),
                settings,
                service="kg-cli",
            )
            principal = None
            if handle:
                async with tenant_session(engine, ctx) as conn:
                    pid = await admin.principal_id_by_handle(conn, ctx, handle)
                principal = _principal(workspace, handle).model_copy(update={"principal_id": pid})
            return await fn(engine, ctx, svc, principal, SystemAudit)
    finally:
        await engine.dispose()


def _echo(value) -> None:  # type: ignore[no-untyped-def]
    from fastapi.encoders import jsonable_encoder

    typer.echo(json.dumps(jsonable_encoder(value), indent=2, sort_keys=True))


@reindex_app.command("preview")
def reindex_preview(workspace: Workspace, mode: Annotated[str, typer.Option(help="project | reextract")] = "project") -> None:
    """What a rebuild would write and what it would cost (writes nothing)."""

    async def go(engine, ctx, svc, principal, audit):  # type: ignore[no-untyped-def]
        from kg_db.session import tenant_session

        async with tenant_session(engine, ctx) as conn:
            _echo(await svc.preview(conn, ctx, mode=mode))

    asyncio.run(_with_service(workspace, None, go))


@reindex_app.command("start")
def reindex_start(
    workspace: Workspace,
    handle: As,
    mode: Annotated[str, typer.Option(help="project | reextract")] = "project",
    key: Annotated[str, typer.Option(help="idempotency key")] = "",
) -> None:
    """Starts the build (a worker runs it). `kg reindex show` follows it; `kg reindex activate` cuts over when it is ready."""

    async def go(engine, ctx, svc, principal, audit):  # type: ignore[no-untyped-def]
        from kg_db.session import tenant_session

        async with tenant_session(engine, ctx) as conn:
            active = (
                await conn.execute(
                    __import__("sqlalchemy").text("SELECT active_version_id FROM workspace_ontology WHERE workspace_id = :ws"),
                    {"ws": ctx.workspace_id},
                )
            ).first()
            _echo(
                await svc.start(
                    conn,
                    ctx,
                    principal,
                    mode=mode,
                    ontology_version_id=active.active_version_id if active else None,
                    audit=audit(conn, ctx.workspace_id),
                    key=key or f"cli-reindex-{__import__('uuid').uuid4()}",
                )
            )

    asyncio.run(_with_service(workspace, handle, go))


@reindex_app.command("show")
def reindex_show(workspace: Workspace, run_id: str = "") -> None:
    """One run (or the latest ones): status, progress, the verification report."""

    async def go(engine, ctx, svc, principal, audit):  # type: ignore[no-untyped-def]
        from kg_db.session import tenant_session

        async with tenant_session(engine, ctx) as conn:
            _echo(
                await svc.get(conn, ctx.workspace_id, __import__("uuid").UUID(run_id))
                if run_id
                else await svc.list_runs(conn, ctx.workspace_id)
            )

    asyncio.run(_with_service(workspace, None, go))


@reindex_app.command("activate")
def reindex_activate(workspace: Workspace, run_id: str, handle: As) -> None:
    """The cutover of a READY build: one exchange in Neo4j, one pointer flip in Postgres, under the workspace's graph lock."""

    async def go(engine, ctx, svc, principal, audit):  # type: ignore[no-untyped-def]
        from kg_db.session import tenant_session

        async with tenant_session(engine, ctx) as conn:
            sink = audit(conn, ctx.workspace_id)
            out = await svc.activate(ctx, __import__("uuid").UUID(run_id), principal, key=f"cli-activate-{run_id}", audit=sink)
        _echo(out)

    asyncio.run(_with_service(workspace, handle, go))


@reindex_app.command("rollback")
def reindex_rollback(workspace: Workspace, namespace: str, handle: As) -> None:
    """Re-activates a RETIRED namespace (inside REINDEX_RETENTION_DAYS)."""

    async def go(engine, ctx, svc, principal, audit):  # type: ignore[no-untyped-def]
        from kg_db.session import tenant_session

        async with tenant_session(engine, ctx) as conn:
            _echo(
                await svc.rollback(
                    ctx,
                    namespace,
                    principal,
                    key=f"cli-rollback-{namespace}-{__import__('uuid').uuid4()}",
                    audit=audit(conn, ctx.workspace_id),
                )
            )

    asyncio.run(_with_service(workspace, handle, go))


@reindex_app.command("cancel")
def reindex_cancel(workspace: Workspace, run_id: str) -> None:
    """Cancels a run that has not been activated and clears its namespace."""

    async def go(engine, ctx, svc, principal, audit):  # type: ignore[no-untyped-def]
        _echo(await svc.cancel(ctx, __import__("uuid").UUID(run_id)))

    asyncio.run(_with_service(workspace, None, go))
