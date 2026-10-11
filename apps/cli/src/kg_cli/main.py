from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

import typer
from kg_common.settings import REPO_ROOT, get_settings
from kg_db import migrate
from kg_db.engine import create_app_engine
from kg_db.session import system_session, tenant_session
from kg_domain.enums import PrincipalKind
from kg_domain.principal import Principal, WorkspaceContext
from kg_governance.audit import AuditWriter
from kg_governance.repositories import admin
from kg_graph.service import GraphService
from kg_ingestion.reencrypt import reencrypt_workspace
from kg_ingestion.services import build_ingestion
from kg_jobs import reads as job_reads
from kg_knowledge.services import KnowledgeServices, build_knowledge
from kg_modelgw.calllog import DbCallSink
from kg_modelgw.factory import build_router_stack
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from kg_cli.seed import publish_ontology, seed_demo, workspace_id_for, write_credentials

app = typer.Typer(no_args_is_help=True, help="Knowledge & Memory Platform operator CLI")
db_app, keys_app, seed_app, openapi_app, graph_app, commands_app, interactions_app, jobs_app, payloads_app = (
    typer.Typer() for _ in range(9)
)
app.add_typer(db_app, name="db", help="Database migrations (runs as kg_owner)")
app.add_typer(keys_app, name="keys", help="API keys")
app.add_typer(seed_app, name="seed", help="Seed data")
app.add_typer(openapi_app, name="openapi", help="OpenAPI contract")
app.add_typer(graph_app, name="graph", help="Graph projection checks and repair")
app.add_typer(commands_app, name="commands", help="Command ledger operations")
app.add_typer(interactions_app, name="interactions", help="Interaction ledger: replay a stage, reconcile stuck events")
app.add_typer(jobs_app, name="jobs", help="Outbox jobs: list, show, retry, cancel")
app.add_typer(payloads_app, name="payloads", help="Payload encryption keys")
CREDENTIALS = REPO_ROOT / ".local" / "dev-credentials.json"


@db_app.command("upgrade")
def db_upgrade(revision: str = "head") -> None:
    migrate.upgrade(get_settings().database_url_owner.get_secret_value(), revision)
    typer.echo(f"database at {revision}")


@db_app.command("downgrade")
def db_downgrade(revision: str = "base") -> None:
    if get_settings().is_production:
        raise typer.Exit(_fail("refusing to downgrade in production"))
    migrate.downgrade(get_settings().database_url_owner.get_secret_value(), revision)
    typer.echo(f"database at {revision}")


@db_app.command("current")
def db_current() -> None:
    migrate.current(get_settings().database_url_owner.get_secret_value())


def _fail(message: str) -> int:
    typer.echo(f"error: {message}", err=True)
    return 1


@seed_app.command("demo")
def seed_demo_cmd(
    rotate_keys: Annotated[bool, typer.Option("--rotate-keys", help="revoke existing seed keys and mint new ones")] = False,
    ontology: Annotated[
        Path | None, typer.Option("--ontology", help="publish and activate this ontology YAML for the demo workspace (idempotent)")
    ] = None,
) -> None:
    settings = get_settings()
    if settings.is_production:
        raise typer.Exit(_fail("refusing to seed when ENV=production"))
    ontology_text = ontology.read_text() if ontology is not None else ""

    async def run() -> None:
        engine = create_app_engine(settings, pool_size=2, max_overflow=0)
        try:
            result = await seed_demo(engine, pepper=settings.auth_key_pepper.get_secret_value(), rotate_keys=rotate_keys)
        finally:
            await engine.dispose()
        typer.echo(f"seed ok: {result.created_workspaces} workspaces and {result.created_principals} principals created")
        if ontology is not None:
            engine = create_app_engine(settings, pool_size=2, max_overflow=0)
            try:
                published = await publish_ontology(engine, ontology_text)
            finally:
                await engine.dispose()
            typer.echo(
                f"ontology {ontology.name}: "
                + ("published and activated for demo" if published else "already published for demo (skipped)")
            )
        if result.new_keys:
            write_credentials(CREDENTIALS, result.new_keys)
            typer.echo(f"minted {len(result.new_keys)} API keys, written once to {CREDENTIALS.relative_to(REPO_ROOT)} (mode 0600):")
            for name in sorted(result.new_keys):
                typer.echo(f"  {name}: {result.new_keys[name]}")
        else:
            typer.echo("no new keys (already seeded; use --rotate-keys to mint new ones)")

    asyncio.run(run())


@keys_app.command("create")
def keys_create(
    workspace: Annotated[str, typer.Option(help="workspace slug")],
    principal: Annotated[str, typer.Option(help="principal handle")],
    ttl_seconds: Annotated[int | None, typer.Option(help="expire the key after this many seconds")] = None,
) -> None:
    settings = get_settings()

    async def run() -> None:
        engine = create_app_engine(settings, pool_size=2, max_overflow=0)
        ctx = WorkspaceContext.for_system_task(workspace_id_for(workspace), reason="cli keys create")
        try:
            async with tenant_session(engine, ctx) as conn:
                pid = await admin.principal_id_by_handle(conn, ctx, principal)
                _, minted = await admin.create_key(
                    conn, ctx, principal_id=pid, pepper=settings.auth_key_pepper.get_secret_value(), expires_in_seconds=ttl_seconds
                )
        finally:
            await engine.dispose()
        typer.echo(minted.plaintext)

    asyncio.run(run())


@keys_app.command("revoke")
def keys_revoke(prefix: str) -> None:
    settings = get_settings()

    async def run() -> bool:
        engine = create_app_engine(settings, pool_size=2, max_overflow=0)
        try:
            async with system_session(engine) as conn:
                row = (await conn.execute(text("SELECT workspace_id FROM auth.lookup_credential(:p)"), {"p": prefix})).first()
            if row is None:
                return False
            ctx = WorkspaceContext.for_system_task(row.workspace_id, reason="cli keys revoke")
            async with tenant_session(engine, ctx) as conn:
                return await admin.revoke_key(conn, ctx, prefix=prefix)
        finally:
            await engine.dispose()

    if not asyncio.run(run()):
        raise typer.Exit(_fail(f"no active key with prefix '{prefix}'"))
    typer.echo(f"revoked {prefix}")


@keys_app.command("list")
def keys_list(workspace: Annotated[str, typer.Option(help="workspace slug")]) -> None:
    settings = get_settings()

    async def run() -> list[dict[str, object]]:
        engine = create_app_engine(settings, pool_size=2, max_overflow=0)
        ctx = WorkspaceContext.for_system_task(workspace_id_for(workspace), reason="cli keys list")
        try:
            async with tenant_session(engine, ctx) as conn:
                rows = (
                    await conn.execute(
                        text("""
                    SELECT k.prefix, p.handle, k.created_at, k.revoked_at, k.last_used_at FROM api_keys k
                    JOIN principals p ON p.id = k.principal_id AND p.workspace_id = k.workspace_id
                    WHERE k.workspace_id = :ws ORDER BY p.handle, k.created_at"""),
                        {"ws": ctx.workspace_id},
                    )
                ).all()
                return [
                    {
                        "prefix": r.prefix,
                        "principal": r.handle,
                        "revoked": r.revoked_at is not None,
                        "last_used_at": str(r.last_used_at or ""),
                    }
                    for r in rows
                ]
        finally:
            await engine.dispose()

    for row in asyncio.run(run()):
        typer.echo(json.dumps(row))


@asynccontextmanager
async def knowledge_runtime(engine: AsyncEngine) -> AsyncIterator[tuple[KnowledgeServices, GraphService]]:
    """Router stack + graph + knowledge services for operator commands that project (embeddings need the router)."""
    settings = get_settings()
    sink = DbCallSink(engine)
    router = build_router_stack(settings, service="kg-worker", sink=sink)
    graph = GraphService(settings, router.transport, router.config)
    try:
        yield build_knowledge(engine, graph, router.config, service="kg-worker"), graph
    finally:
        await sink.aclose()
        await graph.aclose()
        await router.aclose()


@graph_app.command("reconcile")
def graph_reconcile(
    workspace: Annotated[str, typer.Option(help="workspace slug")],
    repair: Annotated[bool, typer.Option("--repair", help="re-project drifted rows and delete orphans")] = False,
) -> None:
    """Compares every Postgres entity and fact with its Neo4j node or edge. Exit code 1 when drift remains."""
    settings = get_settings()

    async def run() -> int:
        engine = create_app_engine(settings, pool_size=3, max_overflow=0)
        ctx = WorkspaceContext.for_system_task(workspace_id_for(workspace), reason="cli graph reconcile")
        try:
            async with knowledge_runtime(engine) as (knowledge, _):
                report = await knowledge.projector.reconcile(ctx, repair=repair)
        finally:
            await engine.dispose()
        typer.echo(
            json.dumps(
                {
                    "entities": report.checked_entities,
                    "facts": report.checked_facts,
                    "pending": report.pending,
                    "skipped_by_policy": report.skipped_by_policy,
                    "drift": [{"kind": d.kind, "id": d.id, "problem": d.problem, "fields": d.fields} for d in report.drift],
                    "repaired": report.repaired,
                }
            )
        )
        return 0 if (not report.drift or repair) else 1

    raise typer.Exit(asyncio.run(run()))


@commands_app.command("retry-projection")
def commands_retry_projection(command_id: str, workspace: Annotated[str, typer.Option(help="workspace slug")]) -> None:
    """Makes a command's failed projection rows due again and projects them now."""
    settings = get_settings()

    async def run() -> str:
        engine = create_app_engine(settings, pool_size=3, max_overflow=0)
        ctx = WorkspaceContext.for_system_task(workspace_id_for(workspace), reason="cli commands retry-projection")
        try:
            async with knowledge_runtime(engine) as (knowledge, _):
                return await knowledge.projector.retry_command(ctx, uuid.UUID(command_id))
        finally:
            await engine.dispose()

    typer.echo(f"projection status: {asyncio.run(run())}")


# ---- interaction ledger and outbox (P3) ---------------------------------------------------------------------------------------------------------


async def _acting_as(conn, ctx: WorkspaceContext, handle: str) -> AuditWriter:  # type: ignore[no-untyped-def]
    """CLI repairs are attributed to a named principal of the workspace (`--actor`), so the audit trail says who ran them."""
    pid = await admin.principal_id_by_handle(conn, ctx, handle)
    principal = Principal(principal_id=pid, workspace_id=ctx.workspace_id, kind=PrincipalKind.USER, handle=handle, roles=frozenset())
    return AuditWriter(conn, principal, request_id=None, correlation_id=f"cli-{uuid.uuid4().hex[:12]}")


Workspace = Annotated[str, typer.Option(help="workspace slug")]
Actor = Annotated[str, typer.Option(help="principal handle the action is attributed to (audited)")]
Reason = Annotated[str, typer.Option(help="reason code, lower_snake (audited)")]


@interactions_app.command("replay")
def interactions_replay(
    event_id: str,
    workspace: Workspace,
    actor: Actor,
    reason: Reason,
    stage: Annotated[str | None, typer.Option(help="stage to replay; default: every dead or failed stage")] = None,
) -> None:
    """Bumps the generation of a dead / failed stage and enqueues it again, with an audit entry (ING-05)."""
    settings = get_settings()

    async def run() -> str:
        engine = create_app_engine(settings, pool_size=2, max_overflow=0)
        ctx = WorkspaceContext.for_system_task(workspace_id_for(workspace), reason="cli interactions replay")
        try:
            ingestion = build_ingestion(settings)
            async with tenant_session(engine, ctx) as conn:
                audit = await _acting_as(conn, ctx, actor)
                out = await ingestion.operations.replay_interaction(conn, ctx, audit, uuid.UUID(event_id), stage=stage, reason_code=reason)
            return out.model_dump_json()
        finally:
            await engine.dispose()

    typer.echo(asyncio.run(run()))


@interactions_app.command("reconcile")
def interactions_reconcile(
    workspace: Workspace, repair: Annotated[bool, typer.Option("--repair", help="re-enqueue what is missing")] = False
) -> None:
    """Finds events and commands the outbox lost track of. Exit code 1 while unrepaired findings remain."""
    settings = get_settings()

    async def run() -> int:
        engine = create_app_engine(settings, pool_size=2, max_overflow=0)
        ctx = WorkspaceContext.for_system_task(workspace_id_for(workspace), reason="cli interactions reconcile")
        try:
            ingestion = build_ingestion(settings)
            async with tenant_session(engine, ctx) as conn:
                report = await ingestion.reconciler.run(conn, ctx, repair=repair)
        finally:
            await engine.dispose()
        typer.echo(
            json.dumps(
                {
                    "checked_interactions": report.checked_interactions,
                    "findings": [
                        {
                            "kind": f.kind,
                            "subject_type": f.subject_type,
                            "subject_id": str(f.subject_id),
                            "stage": f.stage,
                            "repaired": f.repaired,
                        }
                        for f in report.findings
                    ],
                    "repaired": report.repaired,
                }
            )
        )
        return 0 if report.clean else 1

    raise typer.Exit(asyncio.run(run()))


@jobs_app.command("list")
def jobs_list(
    workspace: Workspace,
    status: Annotated[str | None, typer.Option()] = None,
    stage: Annotated[str | None, typer.Option()] = None,
    limit: Annotated[int, typer.Option()] = 50,
) -> None:
    settings = get_settings()

    async def run() -> list[dict[str, object]]:
        engine = create_app_engine(settings, pool_size=2, max_overflow=0)
        ctx = WorkspaceContext.for_system_task(workspace_id_for(workspace), reason="cli jobs list")
        try:
            async with tenant_session(engine, ctx) as conn:
                rows = await job_reads.list_jobs(
                    conn, ctx.workspace_id, status=status, stage=stage, subject_type=None, subject_id=None, size=limit, before=None
                )
            return [json.loads(json.dumps(job_reads.job_summary(r), default=str)) for r in rows[:limit]]
        finally:
            await engine.dispose()

    for row in asyncio.run(run()):
        typer.echo(json.dumps(row))


@jobs_app.command("show")
def jobs_show(job_id: str, workspace: Workspace) -> None:
    settings = get_settings()

    async def run() -> dict[str, object]:
        engine = create_app_engine(settings, pool_size=2, max_overflow=0)
        ctx = WorkspaceContext.for_system_task(workspace_id_for(workspace), reason="cli jobs show")
        try:
            async with tenant_session(engine, ctx) as conn:
                job = await job_reads.get_job(conn, ctx.workspace_id, uuid.UUID(job_id))
                attempts = await job_reads.job_attempts(conn, ctx.workspace_id, job.id)
            shown: dict[str, object] = json.loads(
                json.dumps({**job_reads.job_summary(job), "attempts_log": [dict(a._mapping) for a in attempts]}, default=str)  # noqa: SLF001
            )
            return shown
        finally:
            await engine.dispose()

    typer.echo(json.dumps(asyncio.run(run()), indent=2))


def _job_action(name: str, job_id: str, workspace: str, actor: str, reason: str) -> None:
    settings = get_settings()

    async def run() -> str:
        engine = create_app_engine(settings, pool_size=2, max_overflow=0)
        ctx = WorkspaceContext.for_system_task(workspace_id_for(workspace), reason=f"cli jobs {name}")
        try:
            ingestion = build_ingestion(settings)
            async with tenant_session(engine, ctx) as conn:
                audit = await _acting_as(conn, ctx, actor)
                action = ingestion.operations.retry_job if name == "retry" else ingestion.operations.cancel_job
                out = await action(conn, ctx, audit, uuid.UUID(job_id), reason_code=reason)
            return out.model_dump_json()
        finally:
            await engine.dispose()

    typer.echo(asyncio.run(run()))


@jobs_app.command("retry")
def jobs_retry(job_id: str, workspace: Workspace, actor: Actor, reason: Reason) -> None:
    """A dead job becomes a new job one generation higher (audited)."""
    _job_action("retry", job_id, workspace, actor, reason)


@jobs_app.command("cancel")
def jobs_cancel(job_id: str, workspace: Workspace, actor: Actor, reason: Reason) -> None:
    """A pending or dead job is cancelled; its stage becomes dead (cancelled_by_operator) (audited)."""
    _job_action("cancel", job_id, workspace, actor, reason)


@payloads_app.command("reencrypt")
def payloads_reencrypt(
    workspace: Workspace,
    to: Annotated[str, typer.Option("--to", help="target key id; it must be in PAYLOAD_KEYS")],
    actor: Actor,
    batch_size: Annotated[int, typer.Option(help="rows per transaction")] = 100,
) -> None:
    """Re-encrypts stored contents under another key id, in resumable batches (payload key rotation)."""
    settings = get_settings()

    async def run() -> int:
        engine = create_app_engine(settings, pool_size=2, max_overflow=0)
        ctx = WorkspaceContext.for_system_task(workspace_id_for(workspace), reason="cli payloads reencrypt")
        try:
            ingestion = build_ingestion(settings)
            async with tenant_session(engine, ctx) as conn:
                principal_id = await admin.principal_id_by_handle(conn, ctx, actor)
            principal = Principal(
                principal_id=principal_id, workspace_id=ctx.workspace_id, kind=PrincipalKind.USER, handle=actor, roles=frozenset()
            )
            return await reencrypt_workspace(
                engine,
                ctx,
                ingestion.cipher,
                lambda conn: AuditWriter(conn, principal, request_id=None, correlation_id=f"cli-{uuid.uuid4().hex[:12]}"),
                to_kid=to,
                batch_size=batch_size,
            )
        finally:
            await engine.dispose()

    typer.echo(f"re-encrypted {asyncio.run(run())} contents under '{to}'")


@openapi_app.command("export")
def openapi_export(out: Path = REPO_ROOT / "contracts" / "openapi.json") -> None:
    """Delegates to kg_api (the CLI never imports the API package, see the import-linter contracts)."""
    proc = subprocess.run([sys.executable, "-m", "kg_api.openapi_export", str(out)], check=False)  # noqa: S603
    raise typer.Exit(proc.returncode)


if __name__ == "__main__":
    app()
