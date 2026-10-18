"""Release-hardening commands (phase 9): `kg reconcile` (the unified drift scan and safe repairs)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
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


# ---- kg secrets (rotation) -----------------------------------------------------------------------------------------------------------------------------
secrets_app = typer.Typer(
    no_args_is_help=True,
    help="Secret rotation: status of every ring, generate a secret, record a rotation, force the end of a pepper window",
)
KINDS = ("content_hmac", "auth_pepper", "jwt", "payload_key", "router_key", "neo4j_password", "postgres_password")


@secrets_app.command("generate")
def secrets_generate(nbytes: Annotated[int, typer.Option("--bytes", min=32, max=96)] = 48) -> None:
    """A fresh random secret (URL-safe base64) for a ring entry or a pepper. Printed once; the platform never stores it."""
    import secrets as pysecrets

    typer.echo(pysecrets.token_urlsafe(nbytes))


@secrets_app.command("status")
def secrets_status() -> None:
    """Every ring as the running configuration holds it (versions and fingerprints only, never a secret), how many live API keys sit on each pepper, and the journal's key versions."""
    import hashlib

    from kg_governance.authn.keys import UNLABELED, pepper_fingerprint

    settings = get_settings()

    async def run() -> dict[str, object]:
        from kg_db.session import system_session
        from sqlalchemy import text

        engine = create_app_engine(settings, pool_size=2, max_overflow=0)
        try:
            async with system_session(engine) as conn:
                counts = {r.pepper_id: int(r.keys) for r in (await conn.execute(text("SELECT * FROM ops.key_pepper_counts()"))).all()}
                recorded = [
                    dict(r._mapping)
                    for r in (
                        await conn.execute(text("SELECT kind, version, status, created_at, retire_after FROM ops.secret_status()"))
                    ).all()
                ]
        finally:
            await engine.dispose()
        cur, prev = pepper_fingerprint(settings.auth_key_pepper.get_secret_value()), settings.auth_key_pepper_previous.get_secret_value()
        prev_fp = pepper_fingerprint(prev) if prev else None
        journal_kids: dict[str, int] = {}
        from pathlib import Path

        path = Path(settings.deletion_journal_dir or ".local/deletion-journal") / "deletions.jsonl"
        if path.exists():
            for line in path.read_text().splitlines():
                if line.strip():
                    kid = json.loads(line).get("kid", "v1")
                    journal_kids[kid] = journal_kids.get(kid, 0) + 1

        def fp(b: bytes) -> str:
            return hashlib.sha256(b"kg-secret-id|" + b).hexdigest()[:8]

        return {
            "jwt": {
                "active": settings.jwt_ring().active,
                "kids": settings.jwt_ring().versions,
                "fingerprints": {k: fp(v) for k, v in settings.jwt_ring().secrets.items()},
            },
            "content_hmac": {
                "active": settings.content_hmac_ring().active,
                "versions": settings.content_hmac_ring().versions,
                "journal_lines_by_version": journal_kids,
            },
            "auth_pepper": {
                "current": cur,
                "previous": prev_fp,
                "live_keys_by_pepper": {
                    ("current" if k == cur else "previous" if k == prev_fp else "unlabeled" if k == UNLABELED else f"retired:{k}"): v
                    for k, v in counts.items()
                },
            },
            "payload_keys": {
                "active": settings.payload_active_kid,
                "kids": sorted(p.split(":")[0] for p in settings.payload_keys.get_secret_value().split(",") if p),
            },
            "recorded_rotations": recorded,
        }

    typer.echo(json.dumps(asyncio.run(run()), indent=2, default=str))


@secrets_app.command("record")
def secrets_record(
    kind: Annotated[str, typer.Option(help="|".join(KINDS))],
    version: str,
    status: Annotated[str, typer.Option(help="active | retiring | retired")] = "active",
    retire_after_days: Annotated[int | None, typer.Option(help="when a retiring version is due to be removed")] = None,
) -> None:
    """Records a rotation step in `secret_versions` (a label, a status and dates: never the secret). Run it as each step of docs/runbooks/rotate-secrets.md is done."""
    if kind not in KINDS or status not in ("active", "retiring", "retired"):
        raise typer.Exit(_bad("unknown kind or status"))
    import psycopg

    url = get_settings().database_url_owner.get_secret_value().replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO secret_versions (kind, version, status, retire_after, retired_at) VALUES (%s, %s, %s, now() + make_interval(days => %s::int), CASE WHEN %s = 'retired' THEN now() END) "
            "ON CONFLICT (kind, version) DO UPDATE SET status = EXCLUDED.status, retire_after = COALESCE(EXCLUDED.retire_after, secret_versions.retire_after), "
            "retired_at = CASE WHEN EXCLUDED.status = 'retired' THEN now() ELSE secret_versions.retired_at END",
            (kind, version, status, retire_after_days, status),
        )
    typer.echo(f"recorded {kind}/{version}: {status}")


@secrets_app.command("expire-keys")
def secrets_expire_keys(
    pepper: Annotated[str, typer.Option(help="a pepper fingerprint from `kg secrets status`, or `unlabeled`")],
    confirm: Annotated[bool, typer.Option("--confirm", help="without it: only count")] = False,
) -> None:
    """The end of a pepper rotation window: API keys still hashed under that pepper stop working now (audited in each workspace). Count first, then --confirm."""
    from kg_db.session import system_session
    from sqlalchemy import text

    async def run() -> int:
        engine = create_app_engine(get_settings(), pool_size=2, max_overflow=0)
        try:
            async with system_session(engine) as conn:
                if not confirm:
                    return int(
                        (
                            await conn.execute(
                                text("SELECT COALESCE(sum(keys), 0) FROM ops.key_pepper_counts() WHERE pepper_id = :p"), {"p": pepper}
                            )
                        ).scalar_one()
                    )
                return int((await conn.execute(text("SELECT ops.expire_keys_on_pepper(:p)"), {"p": pepper})).scalar_one())
        finally:
            await engine.dispose()

    n = asyncio.run(run())
    typer.echo(f"{n} keys " + ("expired" if confirm else "would be expired (use --confirm)"))


def _bad(message: str) -> int:
    typer.echo(f"error: {message}", err=True)
    return 1


# ---- kg workspaces (P9 WP10): the first-boot bootstrap that `kg seed demo` refuses to be in production ------------------------------------------------------------------------
workspaces_app = typer.Typer(no_args_is_help=True, help="Workspaces: create one with its first administrator (production first boot)")


@workspaces_app.command("create")
def workspaces_create(
    slug: Annotated[str, typer.Option(help="lowercase letters, digits and hyphens; 2-40 characters")],
    name: Annotated[str, typer.Option(help="display name")],
    ontology: Annotated[Path | None, typer.Option(help="publish and activate this ontology YAML (idempotent)")] = None,
) -> None:
    """Creates the workspace and its first administrator with ONE break-glass API key, printed once (the platform keeps only its hash). Idempotent: a repeat creates nothing and
    prints no key. With OIDC enabled, the next step is `POST /v1/oidc-group-mappings` (or the console), after which the key can be revoked."""
    import re

    from kg_domain.enums import PrincipalKind, Role

    from kg_cli.seed import SeedPrincipal, SeedResult, publish_ontology, seed_workspace

    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,38}[a-z0-9]", slug):
        raise typer.BadParameter("slug: 2-40 characters, lowercase letters, digits and hyphens, not starting or ending with a hyphen")
    settings = get_settings()

    async def run() -> None:
        engine = create_app_engine(settings, pool_size=2, max_overflow=0)
        result = SeedResult(new_keys={})
        try:
            await seed_workspace(
                engine,
                slug,
                name,
                [SeedPrincipal(f"admin@{slug}", PrincipalKind.USER, Role.WORKSPACE_ADMIN, f"{name} administrator")],
                pepper=settings.auth_key_pepper.get_secret_value(),
                rotate_keys=False,
                result=result,
            )
            if ontology is not None:
                published = await publish_ontology(engine, ontology.read_text(), slug=slug)
                typer.echo(f"ontology {ontology.name}: " + ("published and activated" if published else "already published (skipped)"))
        finally:
            await engine.dispose()
        typer.echo(f"workspace {slug}: {result.created_workspaces} created, {result.created_principals} administrator(s) created")
        for handle, key in sorted((result.new_keys or {}).items()):
            typer.echo(f"{handle.split('/')[-1]}: {key}")
            typer.echo("^ shown once. Store it in the password manager; in production it works only while ALLOW_BREAKGLASS_KEYS=true.")
        if not result.new_keys:
            typer.echo("no new key (the administrator already has one; mint another with `kg keys create`)")

    asyncio.run(run())
