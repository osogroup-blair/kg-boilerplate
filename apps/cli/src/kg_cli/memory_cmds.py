"""`kg memories` and `kg scopes` (phase 5): operator access to curated memory, attributed to a named principal (`--actor`) and authorised exactly like the API.

Reads and writes go through the same services the API uses, so a CLI call sees only what that principal's scope grants allow; there is no back door."""

from __future__ import annotations

import asyncio
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
from kg_domain.principal import Principal, WorkspaceContext
from kg_governance.audit import AuditMetadata, AuditWriter
from kg_governance.authz.policy import Action, is_allowed
from kg_governance.repositories import admin
from kg_memory.models import ReasonIn, SearchIn, SupersedeIn
from kg_memory.reads import Viewer
from kg_memory.reembed import reembed_workspace
from kg_memory.scopes import ScopeGrants
from kg_memory.services import MemoryServices, build_memory
from kg_memory.writers import WriteContext
from kg_modelgw.calllog import DbCallSink
from kg_modelgw.config import ModelConfig
from kg_modelgw.factory import build_router_stack
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from kg_cli.seed import workspace_id_for

memories_app = typer.Typer(
    no_args_is_help=True, help="Curated memory: list, show, search, supersede, expire, re-embed (scoped to --actor's grants)"
)
scopes_app = typer.Typer(
    no_args_is_help=True, help="Scope grants: which subjects, projects and classifications a principal may read in memory"
)

Workspace = Annotated[str, typer.Option(help="workspace slug")]
Actor = Annotated[
    str, typer.Option(help="principal handle the action is attributed to; its roles and grants decide what it can see and do")
]


def _fail(message: str) -> int:
    typer.echo(f"error: {message}", err=True)
    return 1


@asynccontextmanager
async def memory_runtime(
    engine: AsyncEngine, settings: Settings, *, offline: bool = False
) -> AsyncIterator[tuple[MemoryServices, Settings]]:
    """The memory services for a CLI command. `offline` builds no router (a search is then lexical-only)."""
    from kg_cli.main import build_knowledge_offline

    knowledge = build_knowledge_offline(engine)
    if offline:
        yield build_memory(settings, knowledge, None, _config(settings), service="kg-cli"), settings
        return
    sink = DbCallSink(engine)
    router = build_router_stack(settings, service="kg-cli", sink=sink)
    sink.start()
    try:
        yield build_memory(settings, knowledge, router.gateway, router.config, service="kg-cli"), settings
    finally:
        await sink.aclose()
        await router.aclose()


def _config(settings: Settings) -> ModelConfig:
    from kg_modelgw.config import load_config

    return load_config(settings.model_purposes_path, settings.pricing_path)


async def _principal(conn: AsyncConnection, ctx: WorkspaceContext, handle: str) -> Principal:
    from kg_cli.main import _acting_principal

    return await _acting_principal(conn, ctx, handle)


def _run[T](coro: Coroutine[Any, Any, T]) -> T:
    try:
        return asyncio.run(coro)
    except PlatformError as exc:
        raise typer.Exit(_fail(f"{exc.code}: {exc.message}")) from exc


@memories_app.command("list")
def memories_list(
    workspace: Workspace,
    actor: Actor,
    type_: Annotated[str | None, typer.Option("--type", help="working | episodic | semantic | procedural")] = None,
    status: Annotated[str | None, typer.Option(help="proposed | active | superseded | disputed | expired | rejected")] = None,
    limit: int = 50,
) -> None:
    settings = get_settings()

    async def run() -> list[str]:
        engine = create_app_engine(settings, pool_size=2, max_overflow=0)
        ctx = WorkspaceContext.for_system_task(workspace_id_for(workspace), reason="cli memories list")
        try:
            async with memory_runtime(engine, settings, offline=True) as (memory, _), tenant_session(engine, ctx) as conn:
                principal = await _principal(conn, ctx, actor)
                grants = await ScopeGrants.for_principal(conn, principal)
                v = Viewer(principal, grants, curator=is_allowed(principal, Action.MEMORY_CURATE))
                page = await memory.reads.list(
                    conn,
                    v,
                    type_=type_,
                    status=status,
                    subject_id=None,
                    project=None,
                    origin=None,
                    classification=None,
                    created_by=None,
                    updated_since=None,
                    limit=limit,
                    cursor=None,
                )
                return [i.model_dump_json() for i in page.items]
        finally:
            await engine.dispose()

    for line in _run(run()):
        typer.echo(line)


@memories_app.command("show")
def memories_show(memory_id: str, workspace: Workspace, actor: Actor) -> None:
    settings = get_settings()

    async def run() -> str:
        engine = create_app_engine(settings, pool_size=2, max_overflow=0)
        ctx = WorkspaceContext.for_system_task(workspace_id_for(workspace), reason="cli memories show")
        try:
            async with memory_runtime(engine, settings, offline=True) as (memory, _), tenant_session(engine, ctx) as conn:
                principal = await _principal(conn, ctx, actor)
                v = Viewer(principal, await ScopeGrants.for_principal(conn, principal), curator=is_allowed(principal, Action.MEMORY_CURATE))
                return (await memory.reads.get(conn, v, uuid.UUID(memory_id))).model_dump_json()
        finally:
            await engine.dispose()

    typer.echo(_run(run()))


@memories_app.command("search")
def memories_search(
    query: str,
    workspace: Workspace,
    actor: Actor,
    limit: int = 10,
    subject_id: Annotated[
        list[str] | None, typer.Option("--subject-id", help="restrict to memories about this entity (repeatable)")
    ] = None,
    run_id: Annotated[str | None, typer.Option(help="the agent run whose session memory is visible")] = None,
) -> None:
    settings = get_settings()

    async def run() -> str:
        engine = create_app_engine(settings, pool_size=2, max_overflow=0)
        ctx = WorkspaceContext.for_system_task(workspace_id_for(workspace), reason="cli memories search")
        try:
            async with memory_runtime(engine, settings) as (memory, _), tenant_session(engine, ctx) as conn:
                principal = await _principal(conn, ctx, actor)
                v = Viewer(principal, await ScopeGrants.for_principal(conn, principal), curator=is_allowed(principal, Action.MEMORY_CURATE))
                body = SearchIn(
                    query=query, limit=limit, subject_ids=[uuid.UUID(s) for s in subject_id] if subject_id else None, run_id=run_id
                )
                out = await memory.search.run(conn, v, body)
                return out.model_dump_json()
        finally:
            await engine.dispose()

    typer.echo(_run(run()))


def _write_context(conn: AsyncConnection, ctx: WorkspaceContext, principal: Principal, grants: ScopeGrants) -> WriteContext:
    audit = AuditWriter(conn, principal, request_id=None, correlation_id=f"cli-{uuid.uuid4().hex[:12]}")
    return WriteContext(conn, ctx, principal, audit, grants, curator=is_allowed(principal, Action.MEMORY_CURATE))


@memories_app.command("supersede")
def memories_supersede(
    memory_id: str,
    workspace: Workspace,
    actor: Actor,
    content: Annotated[str, typer.Option(help="the new wording")],
    reason: Annotated[str, typer.Option(help="reason code, lower_snake (audited)")] = "corrected",
) -> None:
    """Replaces a memory with a new active one; the old one is kept as `superseded` with its evidence and lineage."""
    settings = get_settings()

    async def run() -> str:
        engine = create_app_engine(settings, pool_size=2, max_overflow=0)
        ctx = WorkspaceContext.for_system_task(workspace_id_for(workspace), reason="cli memories supersede")
        try:
            async with memory_runtime(engine, settings, offline=True) as (memory, _), tenant_session(engine, ctx) as conn:
                principal = await _principal(conn, ctx, actor)
                grants = await ScopeGrants.for_principal(conn, principal)
                w = _write_context(conn, ctx, principal, grants)
                current = await memory.reads.get(conn, w.viewer, uuid.UUID(memory_id))
                written = await memory.writer.supersede(
                    w,
                    uuid.UUID(memory_id),
                    SupersedeIn(content=content, reason_code=reason),
                    f"cli-supersede-{memory_id}-{uuid.uuid4().hex[:8]}",
                    current.revision,
                )
                return written.out.model_dump_json()
        finally:
            await engine.dispose()

    typer.echo(_run(run()))


@memories_app.command("expire")
def memories_expire(
    memory_id: str,
    workspace: Workspace,
    actor: Actor,
    reason: Annotated[str, typer.Option(help="reason code, lower_snake (audited)")] = "no_longer_relevant",
) -> None:
    settings = get_settings()

    async def run() -> str:
        engine = create_app_engine(settings, pool_size=2, max_overflow=0)
        ctx = WorkspaceContext.for_system_task(workspace_id_for(workspace), reason="cli memories expire")
        try:
            async with memory_runtime(engine, settings, offline=True) as (memory, _), tenant_session(engine, ctx) as conn:
                principal = await _principal(conn, ctx, actor)
                grants = await ScopeGrants.for_principal(conn, principal)
                w = _write_context(conn, ctx, principal, grants)
                current = await memory.reads.get(conn, w.viewer, uuid.UUID(memory_id))
                written = await memory.writer.expire(
                    w,
                    uuid.UUID(memory_id),
                    ReasonIn(reason_code=reason),
                    f"cli-expire-{memory_id}-{uuid.uuid4().hex[:8]}",
                    current.revision,
                )
                return written.out.model_dump_json()
        finally:
            await engine.dispose()

    typer.echo(_run(run()))


@memories_app.command("reembed")
def memories_reembed(
    workspace: Workspace,
    to_version: Annotated[
        int, typer.Option(help="the embedding version to write (reads stay pinned to MEMORY_EMBEDDING_VERSION until you switch)")
    ],
    batch_size: int = 32,
    drop_version: Annotated[
        int | None, typer.Option(help="after 100% coverage of --to-version, delete this older version's vectors")
    ] = None,
) -> None:
    """Writes a new embedding version for every live memory (resumable: only memories without that version are touched)."""
    settings = get_settings()

    async def run() -> str:
        engine = create_app_engine(settings, pool_size=2, max_overflow=0)
        try:
            sink = DbCallSink(engine)
            router = build_router_stack(settings, service="kg-cli", sink=sink)
            sink.start()
            try:
                report = await reembed_workspace(
                    engine,
                    router.gateway,
                    router.config,
                    settings,
                    workspace_id_for(workspace),
                    to_version=to_version,
                    batch_size=batch_size,
                    drop_version=drop_version,
                )
            finally:
                await sink.aclose()
                await router.aclose()
            return json.dumps(report.__dict__)
        finally:
            await engine.dispose()

    typer.echo(_run(run()))


# ---- scope grants -----------------------------------------------------------------------------------------------------------------------------


@scopes_app.command("grant")
def scopes_grant(
    workspace: Workspace,
    principal: Annotated[str, typer.Option(help="principal handle that receives the grant")],
    kind: Annotated[str, typer.Option(help="subject | project | classification")],
    ref: Annotated[str, typer.Option(help="an entity id, a project key, a classification name, or * for every subject / project")],
    actor: Actor,
) -> None:
    """Gives a principal read access to memory of a subject or project (audited). Grants widen READS only; nothing here lets a principal write more."""
    settings = get_settings()

    async def run() -> str:
        engine = create_app_engine(settings, pool_size=2, max_overflow=0)
        ctx = WorkspaceContext.for_system_task(workspace_id_for(workspace), reason="cli scopes grant")
        try:
            async with tenant_session(engine, ctx) as conn:
                acting = await _principal(conn, ctx, actor)
                if not is_allowed(acting, Action.PRINCIPAL_READ):
                    raise typer.Exit(_fail(f"'{actor}' may not manage scope grants (needs workspace_admin)"))
                target = await admin.principal_id_by_handle(conn, ctx, principal)
                created = await admin.grant_scope(conn, ctx, principal_id=target, kind=kind, ref=ref)
                audit = AuditWriter(conn, acting, request_id=None, correlation_id=f"cli-{uuid.uuid4().hex[:12]}")
                await audit.record(
                    "scope.granted",
                    resource_type="principal",
                    resource_id=target,
                    metadata=AuditMetadata(principal_id=target, scope_kind=kind if kind != "agent" else None, count=int(created)),
                )
                return "granted" if created else "already granted"
        finally:
            await engine.dispose()

    typer.echo(_run(run()))


@scopes_app.command("revoke")
def scopes_revoke(
    workspace: Workspace,
    principal: Annotated[str, typer.Option(help="principal handle")],
    kind: Annotated[str, typer.Option(help="subject | project | classification")],
    ref: Annotated[str, typer.Option(help="the grant's ref")],
    actor: Actor,
) -> None:
    settings = get_settings()

    async def run() -> str:
        engine = create_app_engine(settings, pool_size=2, max_overflow=0)
        ctx = WorkspaceContext.for_system_task(workspace_id_for(workspace), reason="cli scopes revoke")
        try:
            async with tenant_session(engine, ctx) as conn:
                acting = await _principal(conn, ctx, actor)
                if not is_allowed(acting, Action.PRINCIPAL_READ):
                    raise typer.Exit(_fail(f"'{actor}' may not manage scope grants (needs workspace_admin)"))
                target = await admin.principal_id_by_handle(conn, ctx, principal)
                removed = await admin.revoke_scope(conn, ctx, principal_id=target, kind=kind, ref=ref)
                audit = AuditWriter(conn, acting, request_id=None, correlation_id=f"cli-{uuid.uuid4().hex[:12]}")
                await audit.record(
                    "scope.revoked",
                    resource_type="principal",
                    resource_id=target,
                    metadata=AuditMetadata(principal_id=target, scope_kind=kind if kind != "agent" else None, count=int(removed)),
                )
                return "revoked" if removed else "no such grant"
        finally:
            await engine.dispose()

    typer.echo(_run(run()))


@scopes_app.command("list")
def scopes_list(workspace: Workspace) -> None:
    settings = get_settings()

    async def run() -> list[tuple[str, str, str]]:
        engine = create_app_engine(settings, pool_size=2, max_overflow=0)
        ctx = WorkspaceContext.for_system_task(workspace_id_for(workspace), reason="cli scopes list")
        try:
            async with tenant_session(engine, ctx) as conn:
                return await admin.list_scopes(conn, ctx)
        finally:
            await engine.dispose()

    for handle, kind, ref in _run(run()):
        typer.echo(f"{handle}\t{kind}\t{ref}")
