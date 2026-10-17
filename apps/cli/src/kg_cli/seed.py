"""Idempotent demo seed. Workspace and principal ids are deterministic (uuid5), so tests and demos can name them."""

from __future__ import annotations

import json
import os
import re
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path

from kg_db.session import tenant_session
from kg_domain.enums import PrincipalKind, Role
from kg_domain.principal import Principal, WorkspaceContext
from kg_governance.audit import AuditWriter
from kg_governance.repositories import admin
from kg_graph.compile import GraphCompiler
from kg_knowledge.ontology_service import OntologyService
from kg_knowledge.specs import SpecCache
from kg_ontology import content_hash, parse_text
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

NAMESPACE = uuid.UUID("6b9d7e0a-3c1f-4e51-9a77-0c5a2f4b9d10")


def workspace_id_for(slug: str) -> uuid.UUID:
    return uuid.uuid5(NAMESPACE, f"workspace:{slug}")


def principal_id_for(slug: str, handle: str) -> uuid.UUID:
    return uuid.uuid5(NAMESPACE, f"principal:{slug}:{handle}")


@dataclass(frozen=True)
class SeedPrincipal:
    handle: str
    kind: PrincipalKind
    role: Role
    display_name: str
    trusted_source: bool = False  # an app that is a system of record: its commands carry authoritative_system authority (P7, ADR-036)


SEED: dict[str, tuple[str, list[SeedPrincipal]]] = {
    "demo": (
        "Demo Workspace",
        [
            SeedPrincipal("admin@demo", PrincipalKind.USER, Role.WORKSPACE_ADMIN, "Demo Admin"),
            SeedPrincipal("reviewer@demo", PrincipalKind.USER, Role.REVIEWER, "Demo Reviewer"),
            SeedPrincipal("operator@demo", PrincipalKind.USER, Role.OPERATOR, "Demo Operator"),
            SeedPrincipal("conversation_app", PrincipalKind.APP, Role.APP_WRITER, "Conversation App", trusted_source=True),
            SeedPrincipal("agent_research", PrincipalKind.AGENT, Role.AGENT, "Research Agent"),
            SeedPrincipal("agent_planning", PrincipalKind.AGENT, Role.AGENT, "Planning Agent"),
            SeedPrincipal(
                "agent_support", PrincipalKind.AGENT, Role.AGENT, "Support Agent"
            ),  # P5: in the workspace, with NO grants (the "unauthorised agent")
        ],
    ),
    "other_demo": (
        "Other Demo Workspace",
        [
            SeedPrincipal("admin@other", PrincipalKind.USER, Role.WORKSPACE_ADMIN, "Other Admin"),
            SeedPrincipal("agent_other", PrincipalKind.AGENT, Role.AGENT, "Other Agent"),
        ],
    ),
}
# Scope grants (P5, enforced for memory from now on; managed by `kg scopes`, an API comes in P7). Subject grants for individual people (Sarah) are made by the
# demo once those people exist; `agent_support` has none on purpose.
SEED_GRANTS: dict[str, dict[str, list[tuple[str, str]]]] = {
    "demo": {
        "admin@demo": [("subject", "*"), ("project", "*")],
        "reviewer@demo": [("subject", "*"), ("project", "*")],
        "conversation_app": [("subject", "*")],
        "agent_research": [("project", "newco-plan")],
        "agent_planning": [("project", "newco-plan")],
    },
    "other_demo": {"admin@other": [("subject", "*"), ("project", "*")]},
}


@dataclass
class SeedResult:
    created_workspaces: int = 0
    created_principals: int = 0
    new_keys: dict[str, str] | None = None  # "<slug>/<handle>" -> plaintext (only keys minted by THIS run)


async def seed_workspace(
    engine: AsyncEngine,
    slug: str,
    display_name: str,
    principals: list[SeedPrincipal],
    *,
    pepper: str,
    rotate_keys: bool,
    result: SeedResult,
    grants: dict[str, list[tuple[str, str]]] | None = None,
) -> None:
    """One workspace with its principals and (when a principal has no active key) one key each. Idempotent."""
    assert result.new_keys is not None
    ctx = WorkspaceContext.for_system_task(workspace_id_for(slug), reason="seed demo workspace")
    async with tenant_session(engine, ctx) as conn:
        result.created_workspaces += int(await admin.ensure_workspace(conn, ctx, slug=slug, display_name=display_name))
        for p in principals:
            pid, created = await admin.ensure_principal(
                conn,
                ctx,
                handle=p.handle,
                kind=p.kind,
                display_name=p.display_name,
                roles=[p.role],
                deterministic_id=principal_id_for(slug, p.handle),
                trusted_source=p.trusted_source,
            )
            result.created_principals += int(created)
            for kind, ref in (grants or {}).get(p.handle, []):
                await admin.grant_scope(conn, ctx, principal_id=pid, kind=kind, ref=ref)
            if rotate_keys:
                await conn.execute(
                    text("UPDATE api_keys SET revoked_at = now() WHERE workspace_id = :ws AND principal_id = :p AND revoked_at IS NULL"),
                    {"ws": ctx.workspace_id, "p": pid},
                )
            active = (
                await conn.execute(
                    text("SELECT count(*) FROM api_keys WHERE workspace_id = :ws AND principal_id = :p AND revoked_at IS NULL"),
                    {"ws": ctx.workspace_id, "p": pid},
                )
            ).scalar_one()
            if not active:
                _, minted = await admin.create_key(conn, ctx, principal_id=pid, pepper=pepper)
                result.new_keys[f"{slug}/{p.handle}"] = minted.plaintext


async def seed_demo(engine: AsyncEngine, *, pepper: str, rotate_keys: bool = False) -> SeedResult:
    result = SeedResult(new_keys={})
    for slug, (display_name, principals) in SEED.items():
        await seed_workspace(
            engine, slug, display_name, principals, pepper=pepper, rotate_keys=rotate_keys, result=result, grants=SEED_GRANTS.get(slug)
        )
    return result


def write_credentials(path: Path, new_keys: dict[str, str]) -> None:
    """Merge into .local/dev-credentials.json (mode 0600). Keys are never logged."""
    path.parent.mkdir(parents=True, exist_ok=True)
    existing: dict[str, str] = json.loads(path.read_text()) if path.exists() else {}
    existing.update(new_keys)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(fd, "w") as f:
        json.dump(existing, f, indent=2, sort_keys=True)
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)


async def publish_ontology(engine: AsyncEngine, content: str, *, slug: str = "demo") -> bool:
    """Publishes and activates the ontology file for the demo workspace as its seeded admin. Returns False when that version already exists
    (so `make seed` stays idempotent). Goes through the same OntologyService as the API: validation, compile check, immutability, audit."""
    spec, issues = parse_text(content)
    if spec is None:
        raise SystemExit(f"not a valid ontology: {[f'{i.path}: {i.code}' for i in issues]}")
    wanted_hash = content_hash(spec)
    ctx = WorkspaceContext.for_system_task(workspace_id_for(slug), reason="seed demo ontology")
    principal = Principal(
        principal_id=principal_id_for(slug, f"admin@{slug}"),
        workspace_id=ctx.workspace_id,
        kind=PrincipalKind.USER,
        handle=f"admin@{slug}",
        roles=frozenset({Role.WORKSPACE_ADMIN}),
    )
    service = OntologyService(GraphCompiler(), SpecCache())
    async with tenant_session(engine, ctx, principal.principal_id) as conn:
        # A long-lived dev workspace may already use this version number for something else (a draft, an older flow): the same CONTENT is "already
        # published" (idempotent), different content goes to the next free minor version, so a re-run never collides or silently skips.
        for _ in range(100):
            taken = (
                await conn.execute(
                    text(
                        "SELECT id, content_hash, state FROM ontology_versions WHERE workspace_id = :ws AND ontology_key = :k AND version = :v"
                    ),
                    {"ws": ctx.workspace_id, "k": spec.key, "v": spec.version},
                )
            ).first()
            if taken is None:
                break
            if taken.state != "draft" and taken.content_hash == wanted_hash:
                # already published: `--ontology X` still means "X is the active ontology" (an earlier demo may have activated another version)
                active = (
                    await conn.execute(
                        text("SELECT active_version_id FROM workspace_ontology WHERE workspace_id = :ws"), {"ws": ctx.workspace_id}
                    )
                ).scalar_one_or_none()
                if taken.state == "published" and active != taken.id:
                    audit = AuditWriter(conn, principal, request_id=None, correlation_id=None)
                    await service.activate(conn, ctx, principal, audit, taken.id, "seed_demo_ontology")
                return False
            major, minor, patch = spec.version.split(".")
            content = re.sub(r"(?m)^(\s*version:\s*)\S+", rf"\g<1>{major}.{int(minor) + 1}.{patch}", content, count=1)
            spec, issues = parse_text(content)
            if spec is None:
                raise SystemExit(f"not a valid ontology after bumping the version: {[f'{i.path}: {i.code}' for i in issues]}")
            wanted_hash = content_hash(spec)
        audit = AuditWriter(conn, principal, request_id=None, correlation_id=None)
        draft = await service.create_draft(conn, ctx, principal, audit, content=content, fmt="yaml", copy_of=None)
        await service.publish(conn, ctx, principal, audit, draft.id, notes="seeded from examples/ontologies", activate=True)
    return True
