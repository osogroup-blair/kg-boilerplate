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
from kg_common.errors import PlatformError
from kg_common.settings import REPO_ROOT, get_settings
from kg_db import migrate
from kg_db.engine import create_app_engine
from kg_db.session import system_session, tenant_session
from kg_domain.enums import PrincipalKind, Role
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

from kg_cli.context_cmds import context_app
from kg_cli.deletion_cmds import deletions_app
from kg_cli.memory_cmds import memories_app, scopes_app
from kg_cli.principals_cmds import principals_app
from kg_cli.release_cmds import reconcile_command, reindex_app, secrets_app
from kg_cli.seed import publish_ontology, seed_demo, workspace_id_for, write_credentials

app = typer.Typer(no_args_is_help=True, help="Knowledge & Memory Platform operator CLI")
db_app, keys_app, seed_app, openapi_app, graph_app, commands_app, interactions_app, jobs_app, payloads_app, reviews_app, extraction_app = (
    typer.Typer() for _ in range(11)
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
app.add_typer(reviews_app, name="reviews", help="Review queue: list, show, decide (identity matches, contradictions, invalidations)")
app.add_typer(extraction_app, name="extraction", help="Extraction cost evidence and the guarded purge")
app.add_typer(memories_app, name="memories", help="Curated memory: list, show, search, supersede, expire, re-embed")
app.add_typer(context_app, name="context", help="Context engine: build as an agent, read a trace, purge old traces")
app.add_typer(scopes_app, name="scopes", help="Scope grants for memory: grant, revoke, list")
app.add_typer(
    deletions_app, name="deletions", help="Deletion requests: list, show, cancel a workspace deletion, retry, journal, retention sweep"
)
app.add_typer(principals_app, name="principals", help="Agents & Access: register, update, keys, grants, revoke, activity")
app.command("reconcile")(reconcile_command)
app.add_typer(secrets_app, name="secrets", help="Secret rotation: status, generate, record, force the end of a pepper window")
app.add_typer(reindex_app, name="reindex", help="Flow F: rebuild the graph in a separate namespace, cut over, roll back")
CREDENTIALS = REPO_ROOT / ".local" / "dev-credentials.json"


@db_app.command("upgrade")
def db_upgrade(revision: str = "head") -> None:
    migrate.upgrade(get_settings().database_url_owner.get_secret_value(), revision)
    typer.echo(f"database at {revision}")


@db_app.command("downgrade")
def db_downgrade(
    revision: str = "base",
    force_drop_extracted: Annotated[
        bool, typer.Option("--force-drop-extracted", help="delete extracted entities, facts and episodes so 0004 can be undone")
    ] = False,
    force_drop_memories: Annotated[
        bool, typer.Option("--force-drop-memories", help="delete curated memories (not recoverable) so 0005 can be undone")
    ] = False,
    force_forget_privileges: Annotated[
        bool, typer.Option("--force-forget-privileges", help="drop grants that carry the publish privilege so 0007 can be undone")
    ] = False,
    force_drop_governance: Annotated[
        bool, typer.Option("--force-drop-governance", help="keep rows made by corrections as ordinary rows so 0008 can be undone")
    ] = False,
    force_drop_release: Annotated[
        bool,
        typer.Option("--force-drop-release", help="drop reindex runs that are still building so 0009 can be undone (refused otherwise)"),
    ] = False,
) -> None:
    if get_settings().is_production:
        raise typer.Exit(_fail("refusing to downgrade in production"))
    try:
        migrate.downgrade(
            get_settings().database_url_owner.get_secret_value(),
            revision,
            force_drop_extracted=force_drop_extracted,
            force_drop_memories=force_drop_memories,
            force_forget_privileges=force_forget_privileges,
            force_drop_governance=force_drop_governance,
            force_drop_release=force_drop_release,
        )
    except RuntimeError as exc:
        raise typer.Exit(_fail(str(exc))) from exc
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
    no_memory_extraction: Annotated[
        bool,
        typer.Option(
            "--no-memory-extraction",
            help="publish the ontology with memory extraction disabled (the P2-P4 demos predate memory and have no memory fixtures)",
        ),
    ] = False,
) -> None:
    settings = get_settings()
    if settings.is_production:
        raise typer.Exit(_fail("refusing to seed when ENV=production"))
    ontology_text = ontology.read_text() if ontology is not None else ""
    if no_memory_extraction:
        ontology_text = ontology_text.replace("    archive_after_days: null\n", "    archive_after_days: null\n    extraction: disabled\n")

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
        yield build_knowledge(engine, graph, router.config, service="kg-worker", settings=settings), graph
    finally:
        await sink.aclose()
        await graph.aclose()
        await router.aclose()


async def _episode_findings(engine: AsyncEngine, ctx: WorkspaceContext, knowledge: KnowledgeServices, *, repair: bool) -> dict[str, object]:
    """P4: every `graph_episodes` row has its episode node in the graph, and the graph's MENTIONS agree with `entity_mentions`."""
    graph = knowledge.projector._graph.scoped(ctx)
    findings: list[dict[str, str]] = []
    async with tenant_session(engine, ctx) as conn:
        episodes = (
            await conn.execute(
                text("SELECT interaction_id, episode_uuid FROM graph_episodes WHERE workspace_id = :ws"), {"ws": ctx.workspace_id}
            )
        ).all()
    present = {r["u"] for r in await graph.run_cypher("MATCH (e:Episodic {group_id: $group_id}) RETURN e.uuid AS u")}
    for e in episodes:
        if e.episode_uuid not in present:
            findings.append({"kind": "episode_missing", "interaction_id": str(e.interaction_id)})
    mentioned = {
        (r["e"], r["n"])
        for r in await graph.run_cypher("MATCH (e:Episodic {group_id: $group_id})-[:MENTIONS]->(n:Entity) RETURN e.uuid AS e, n.uuid AS n")
    }
    async with tenant_session(engine, ctx) as conn:
        rows = (
            await conn.execute(
                text("SELECT episode_uuid, entity_id FROM entity_mentions WHERE workspace_id = :ws"), {"ws": ctx.workspace_id}
            )
        ).all()
    wanted = {(r.episode_uuid, str(r.entity_id)) for r in rows}
    for _episode, entity in sorted(wanted - mentioned):
        findings.append({"kind": "mention_missing_in_graph", "interaction_id": "", "entity_id": entity})
    # repairs belong to the interaction's restore job: `kg interactions replay --stage graph_projection` or `kg extraction purge`
    _ = repair
    return {"checked": len(episodes), "findings": findings}


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
                episodes = await _episode_findings(engine, ctx, knowledge, repair=repair)
        finally:
            await engine.dispose()
        typer.echo(
            json.dumps(
                {
                    "episodes": episodes,
                    "entities": report.checked_entities,
                    "facts": report.checked_facts,
                    "pending": report.pending,
                    "skipped_by_policy": report.skipped_by_policy,
                    "drift": [{"kind": d.kind, "id": d.id, "problem": d.problem, "fields": d.fields} for d in report.drift],
                    "repaired": report.repaired,
                }
            )
        )
        return 0 if ((not report.drift and not episodes["findings"]) or repair) else 1

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
    stage: Annotated[
        str | None, typer.Option(help="stage to replay; default: every dead or failed stage (graph_ingest with --repin)")
    ] = None,
    repin: Annotated[
        bool, typer.Option("--repin", help="move the stage to the current pipeline version and the CURRENT active ontology (audited)")
    ] = False,
    upgrade_pipeline: Annotated[
        bool,
        typer.Option(
            "--upgrade-pipeline",
            help="repin every stage this event skipped as disabled that the current pipeline enables, e.g. memory_propose for a p4.v1 event (audited)",
        ),
    ] = False,
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
                out = await ingestion.operations.replay_interaction(
                    conn, ctx, audit, uuid.UUID(event_id), stage=stage, reason_code=reason, repin=repin, upgrade_pipeline=upgrade_pipeline
                )
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


# ---- review queue, extraction (P4) -----------------------------------------------------------------------------------------------------------------


async def _acting_principal(conn, ctx: WorkspaceContext, handle: str) -> Principal:  # type: ignore[no-untyped-def]
    """The named principal with its REAL roles: a decision from the CLI is authorised exactly like one from the API."""
    row = (
        await conn.execute(
            text(
                "SELECT p.id, p.kind, COALESCE(array_agg(r.role) FILTER (WHERE r.role IS NOT NULL), ARRAY[]::text[]) AS roles FROM principals p "
                "LEFT JOIN role_assignments r ON r.workspace_id = p.workspace_id AND r.principal_id = p.id WHERE p.workspace_id = :ws AND p.handle = :h GROUP BY p.id"
            ),
            {"ws": ctx.workspace_id, "h": handle},
        )
    ).first()
    if row is None:
        raise typer.Exit(_fail(f"no principal '{handle}' in this workspace"))
    return Principal(
        principal_id=row.id,
        workspace_id=ctx.workspace_id,
        kind=PrincipalKind(row.kind),
        handle=handle,
        roles=frozenset(Role(x) for x in row.roles),
    )


@reviews_app.command("list")
def reviews_list(
    workspace: Workspace,
    item_type: Annotated[
        str | None, typer.Option("--type", help="identity_match | contradiction | graphiti_invalidation | policy_violation")
    ] = None,
    status: Annotated[str | None, typer.Option(help="open | decided")] = "open",
) -> None:
    settings = get_settings()

    async def run() -> list[str]:
        engine = create_app_engine(settings, pool_size=2, max_overflow=0)
        ctx = WorkspaceContext.for_system_task(workspace_id_for(workspace), reason="cli reviews list")
        try:
            knowledge = build_knowledge_offline(engine)
            async with tenant_session(engine, ctx) as conn:
                page = await knowledge.reviews.list_items(conn, ctx, item_type=item_type, status=status, limit=100, cursor=None)
            return [i.model_dump_json() for i in page.items]
        finally:
            await engine.dispose()

    for line in asyncio.run(run()):
        typer.echo(line)


@reviews_app.command("show")
def reviews_show(review_id: str, workspace: Workspace) -> None:
    settings = get_settings()

    async def run() -> str:
        engine = create_app_engine(settings, pool_size=2, max_overflow=0)
        ctx = WorkspaceContext.for_system_task(workspace_id_for(workspace), reason="cli reviews show")
        try:
            knowledge = build_knowledge_offline(engine)
            async with tenant_session(engine, ctx) as conn:
                return (await knowledge.reviews.get(conn, ctx, uuid.UUID(review_id), can_see_sensitive=False)).model_dump_json()
        finally:
            await engine.dispose()

    typer.echo(asyncio.run(run()))


@reviews_app.command("decide")
def reviews_decide(
    review_id: str,
    workspace: Workspace,
    actor: Actor,
    decision: Annotated[
        str, typer.Option(help="e.g. same_entity, different_entity, keep_existing, mark_disputed, acknowledge, approve, reject")
    ],
    rationale_code: Annotated[str, typer.Option(help="closed code, e.g. same_real_world_entity, verified_with_source, other")] = "other",
    rationale_text: Annotated[str | None, typer.Option(help="optional, stored on the item only")] = None,
) -> None:
    """Decides a review item as `--actor` (a reviewer or admin of the workspace); then restores the graph for the rows the decision changed."""
    from kg_knowledge.commands import CommandContext
    from kg_knowledge.models import DecisionIn

    settings = get_settings()

    async def run() -> str:
        engine = create_app_engine(settings, pool_size=3, max_overflow=0)
        ctx = WorkspaceContext.for_system_task(workspace_id_for(workspace), reason="cli reviews decide")
        try:
            async with knowledge_runtime(engine) as (knowledge, _):
                from kg_memory.services import build_memory
                from kg_modelgw.config import load_config

                build_memory(
                    settings, knowledge, None, load_config(settings.model_purposes_path, settings.pricing_path), service="kg-cli"
                )  # registers memory decisions
                async with tenant_session(engine, ctx) as conn:
                    principal = await _acting_principal(conn, ctx, actor)
                    if not (principal.roles & {Role.WORKSPACE_ADMIN, Role.REVIEWER}):
                        raise typer.Exit(_fail(f"'{actor}' may not decide reviews (needs reviewer or workspace_admin)"))
                    current = await knowledge.reviews.get(conn, ctx, uuid.UUID(review_id), can_see_sensitive=False)
                    audit = AuditWriter(conn, principal, request_id=None, correlation_id=f"cli-{uuid.uuid4().hex[:12]}")
                    out, _ = await knowledge.reviews.decide(
                        CommandContext(conn, ctx, principal, audit),
                        uuid.UUID(review_id),
                        DecisionIn.model_validate(
                            {"decision": decision, "rationale_code": rationale_code, "rationale_text": rationale_text}
                        ),
                        key=f"cli-{review_id}-{decision}",
                        if_match=current.revision,
                    )
                if out.command_id is not None and out.projection_status != "not_applicable":
                    async with knowledge.lock.hold(ctx.workspace_id, wait_seconds=30.0):
                        out.projection_status = await knowledge.projector.project_command(ctx, out.command_id)  # type: ignore[assignment]
                return out.model_dump_json()
        finally:
            await engine.dispose()

    try:
        typer.echo(asyncio.run(run()))
    except PlatformError as exc:
        raise typer.Exit(_fail(f"{exc.code}: {exc.message}")) from exc


@extraction_app.command("stats")
def extraction_stats(workspace: Workspace) -> None:
    """Cost evidence per episode (GR-11): model calls, tokens and the estimated cost of the extractions in this workspace."""
    settings = get_settings()

    async def run() -> dict[str, object]:
        engine = create_app_engine(settings, pool_size=2, max_overflow=0)
        ctx = WorkspaceContext.for_system_task(workspace_id_for(workspace), reason="cli extraction stats")
        try:
            async with tenant_session(engine, ctx) as conn:
                agg = (
                    await conn.execute(
                        text(
                            "SELECT count(*) AS episodes, COALESCE(sum(chat_calls), 0) AS chat, COALESCE(sum(embedding_calls), 0) AS embeddings, "
                            "COALESCE(sum(prompt_tokens), 0) AS prompt, COALESCE(sum(completion_tokens), 0) AS completion FROM graph_episodes WHERE workspace_id = :ws"
                        ),
                        {"ws": ctx.workspace_id},
                    )
                ).one()
                cost = (
                    await conn.execute(
                        text(
                            "SELECT COALESCE(sum(est_cost_usd), 0) AS usd FROM model_calls WHERE workspace_id = :ws AND purpose = 'graph_extraction'"
                        ),
                        {"ws": ctx.workspace_id},
                    )
                ).scalar_one()
            n = max(int(agg.episodes), 1)
            return {
                "episodes": int(agg.episodes),
                "chat_calls": int(agg.chat),
                "embedding_calls": int(agg.embeddings),
                "prompt_tokens": int(agg.prompt),
                "completion_tokens": int(agg.completion),
                "est_cost_usd": float(cost),
                "per_episode": {"chat_calls": round(int(agg.chat) / n, 2), "est_cost_usd": round(float(cost) / n, 6)},
            }
        finally:
            await engine.dispose()

    typer.echo(json.dumps(asyncio.run(run())))


@extraction_app.command("purge")
def extraction_purge(
    workspace: Workspace,
    yes: Annotated[bool, typer.Option("--yes", help="really delete (without it the command only reports what it would delete)")] = False,
) -> None:
    """Deletes EVERYTHING extraction produced in this workspace (entities, facts, mentions, claims, episodes, extraction reviews), clears its graph and re-projects
    the command-sourced rows from Postgres. Needed before `kg db downgrade 0003`; interactions stay (replay graph_ingest with --repin to extract again)."""
    from sqlalchemy.ext.asyncio import create_async_engine

    settings = get_settings()
    if settings.is_production:
        raise typer.Exit(_fail("refusing to purge extracted data when ENV=production"))

    async def run() -> dict[str, int]:
        ws = workspace_id_for(workspace)
        owner = create_async_engine(settings.database_url_owner.get_secret_value())
        counts: dict[str, int] = {}
        try:
            async with owner.begin() as conn:
                p = {"ws": ws}
                for label, sql in (
                    ("extracted_facts", "SELECT count(*) FROM facts WHERE workspace_id = :ws AND source_type = 'extracted'"),
                    ("extracted_entities", "SELECT count(*) FROM entities WHERE workspace_id = :ws AND origin = 'extracted'"),
                    ("episodes", "SELECT count(*) FROM graph_episodes WHERE workspace_id = :ws"),
                ):
                    counts[label] = int((await conn.execute(text(sql), p)).scalar_one())
                if not yes:
                    return counts
                await conn.execute(text("DELETE FROM review_items WHERE workspace_id = :ws AND item_type <> 'policy_violation'"), p)
                await conn.execute(
                    text(
                        "DELETE FROM review_items WHERE workspace_id = :ws AND fact_id IN (SELECT id FROM facts WHERE workspace_id = :ws AND source_type = 'extracted')"
                    ),
                    p,
                )
                for table in ("graph_episodes", "attribute_claims", "entity_mentions", "identity_non_matches"):
                    await conn.execute(text(f"DELETE FROM {table} WHERE workspace_id = :ws"), p)  # noqa: S608 (constant names)
                await conn.execute(
                    text(
                        "UPDATE facts SET succeeded_by_fact_id = NULL WHERE workspace_id = :ws AND succeeded_by_fact_id IN (SELECT id FROM facts WHERE workspace_id = :ws AND source_type = 'extracted')"
                    ),
                    p,
                )
                await conn.execute(
                    text(
                        "DELETE FROM fact_evidence WHERE workspace_id = :ws AND (source_kind = 'episode' OR fact_id IN (SELECT id FROM facts WHERE workspace_id = :ws AND source_type = 'extracted'))"
                    ),
                    p,
                )
                await conn.execute(
                    text(
                        "DELETE FROM facts WHERE workspace_id = :ws AND (source_type = 'extracted' OR source_entity_id IN (SELECT id FROM entities WHERE workspace_id = :ws AND origin = 'extracted') "
                        "OR target_entity_id IN (SELECT id FROM entities WHERE workspace_id = :ws AND origin = 'extracted'))"
                    ),
                    p,
                )
                await conn.execute(
                    text(
                        "DELETE FROM entity_external_ids WHERE workspace_id = :ws AND entity_id IN (SELECT id FROM entities WHERE workspace_id = :ws AND origin = 'extracted')"
                    ),
                    p,
                )
                await conn.execute(
                    text(
                        "UPDATE entities SET merged_into_id = NULL, status = 'active', merged_at = NULL WHERE workspace_id = :ws AND origin = 'command' AND status = 'merged'"
                    ),
                    p,
                )
                await conn.execute(text("DELETE FROM entities WHERE workspace_id = :ws AND origin = 'extracted'"), p)
                await conn.execute(
                    text(
                        "UPDATE facts SET status = 'active', decision_code = NULL, decided_at = NULL WHERE workspace_id = :ws AND status IN ('disputed') AND source_type = 'command'"
                    ),
                    p,
                )
                await conn.execute(
                    text("UPDATE entities SET projection_status = 'pending', projected_revision = NULL WHERE workspace_id = :ws"), p
                )
                await conn.execute(
                    text("UPDATE facts SET projection_status = 'pending', projected_revision = NULL WHERE workspace_id = :ws"), p
                )
        finally:
            await owner.dispose()
        engine = create_app_engine(settings, pool_size=3, max_overflow=0)
        ctx = WorkspaceContext.for_system_task(ws, reason="cli extraction purge")
        try:
            async with knowledge_runtime(engine) as (knowledge, graph):
                await graph.scoped(ctx).run_cypher("MATCH (n {group_id: $group_id}) DETACH DELETE n")
                report = await knowledge.projector.reconcile(ctx, repair=True)
                counts["reprojected"] = report.repaired
        finally:
            await engine.dispose()
        return counts

    typer.echo(json.dumps(asyncio.run(run())))


def build_knowledge_offline(engine: AsyncEngine) -> KnowledgeServices:
    """Knowledge services for read-only CLI commands: no router, no graph, nothing that could call a model."""
    from kg_graph.compile import GraphCompiler
    from kg_knowledge.commands import CommandPipeline
    from kg_knowledge.corrections import Corrections
    from kg_knowledge.extracted import ExtractedKnowledge
    from kg_knowledge.reads import Reads
    from kg_knowledge.reviews import ReviewService
    from kg_knowledge.specs import SpecCache

    settings = get_settings()
    specs = SpecCache()
    reads = Reads(specs)
    extracted = ExtractedKnowledge()
    pipeline = CommandPipeline(specs)
    services = KnowledgeServices.__new__(KnowledgeServices)
    services.reviews = ReviewService(specs, extracted, pipeline, reads)
    services.reads, services.specs, services.extracted, services.pipeline = reads, specs, extracted, pipeline
    services.corrections = Corrections(
        specs,
        extracted,
        hmac_secret=settings.content_hmac_secret.get_secret_value().encode(),
        trace_retention_days=settings.context_trace_retention_days,
    )
    _ = GraphCompiler
    return services


@openapi_app.command("export")
def openapi_export(out: Path = REPO_ROOT / "contracts" / "openapi.json") -> None:
    """Delegates to kg_api (the CLI never imports the API package, see the import-linter contracts)."""
    proc = subprocess.run([sys.executable, "-m", "kg_api.openapi_export", str(out)], check=False)  # noqa: S603
    raise typer.Exit(proc.returncode)


if __name__ == "__main__":
    app()
