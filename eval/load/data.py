"""Seeded synthetic workspaces for the load baselines (phase-9 WP4): S, M and L sizes, bulk-loaded with SQL so a 200,000-memory workspace takes minutes, not hours.

    SIZES  S = 1k entities / 5k facts / 10k memories,  M = 5k / 25k / 50k,  L = 20k / 100k / 200k   (per workspace)

The ledger is honest where it matters: every row hangs off a real command, memories carry scopes and a vector, and facts are marked projected without nodes in Neo4j (the context
builds then measure the Postgres side and report the graph as having nothing, exactly as the P6 performance baseline did). Everything is synthetic; the seed makes it repeatable."""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kg_cli.seed import SeedPrincipal, SeedResult, publish_ontology, seed_workspace, workspace_id_for
from kg_common.settings import Settings
from kg_db.engine import create_app_engine
from kg_domain.enums import PrincipalKind, Role
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

ROOT = Path(__file__).resolve().parents[2]
SIZES: dict[str, tuple[int, int, int]] = {"S": (1_000, 5_000, 10_000), "M": (5_000, 25_000, 50_000), "L": (20_000, 100_000, 200_000)}
ABOUT_SARAH = 120


@dataclass
class Seeded:
    slug: str
    size: str
    workspace_id: uuid.UUID
    keys: dict[str, str]
    sarah: str
    entities: int
    facts: int
    memories: int


def _chunks(total: int, step: int) -> list[int]:
    return [step] * (total // step) + ([total % step] if total % step else [])


async def seed(settings: Settings, owner_url: str, slug: str, size: str, *, ontology_yaml: str | None = None) -> Seeded:
    entities, facts, memories = SIZES[size]
    engine = create_app_engine(settings, pool_size=3, max_overflow=2)
    result = SeedResult(new_keys={})
    principals = [
        SeedPrincipal(f"admin@{slug}", PrincipalKind.USER, Role.WORKSPACE_ADMIN, "Admin"),
        SeedPrincipal(f"agent@{slug}", PrincipalKind.AGENT, Role.AGENT, "Agent"),
        SeedPrincipal(f"app@{slug}", PrincipalKind.APP, Role.APP_WRITER, "App", trusted_source=True),
    ]
    await seed_workspace(
        engine,
        slug,
        f"Load {slug}",
        principals,
        pepper=settings.auth_key_pepper.get_secret_value(),
        rotate_keys=False,
        result=result,
        grants={
            f"agent@{slug}": [
                ("subject", "*"),
                ("project", "load-plan"),
            ],  # without the subject grant every context build is `denied` (P7 subject ACLs)
            f"admin@{slug}": [("subject", "*"), ("project", "*")],
            f"app@{slug}": [("subject", "*")],
        },
    )
    yaml_text = ontology_yaml or (ROOT / "examples" / "ontologies" / "personal-knowledge-agents.yaml").read_text()
    await publish_ontology(engine, yaml_text, slug=slug)
    await engine.dispose()
    ws = workspace_id_for(slug)
    owner = create_async_engine(owner_url)
    try:
        async with owner.begin() as c:
            await c.execute(text("SELECT setseed(0.7)"))
            row = (
                await c.execute(
                    text(
                        "SELECT (SELECT active_version_id FROM workspace_ontology WHERE workspace_id = :ws) AS ov, "
                        "(SELECT id FROM principals WHERE workspace_id = :ws AND handle = :h) AS admin"
                    ),
                    {"ws": ws, "h": f"admin@{slug}"},
                )
            ).one()
            cmd = uuid.uuid4()
            await c.execute(
                text(
                    "INSERT INTO commands (id, workspace_id, idempotency_key, request_hash, command_type, actor_principal_id, authority_class, classification, payload, status, http_status, ontology_version_id, projection_status) "
                    "VALUES (:id, :ws, 'load-seed', 'h', 'entity.create', :admin, 'admin_asserted', 'internal', '{}', 'applied', 201, :ov, 'not_applicable')"
                ),
                {"id": cmd, "ws": ws, "admin": row.admin, "ov": row.ov},
            )
            p = {"ws": ws, "cmd": cmd, "ov": row.ov, "admin": row.admin}
            sarah = uuid.uuid4()
            await c.execute(
                text(
                    "INSERT INTO entities (id, workspace_id, type_key, display_name, ontology_version_id, created_by_command_id, last_command_id, projection_status, embedding_status) "
                    "VALUES (:id, :ws, 'Person', 'Sarah Johnson', :ov, :cmd, :cmd, 'projected', 'skipped_by_policy')"
                ),
                {**p, "id": sarah},
            )
            await c.execute(
                text(
                    "INSERT INTO entities (id, workspace_id, type_key, display_name, ontology_version_id, created_by_command_id, last_command_id, projection_status, embedding_status) "
                    "SELECT gen_random_uuid(), :ws, 'Person', 'Load Person ' || g, :ov, :cmd, :cmd, 'projected', 'skipped_by_policy' FROM generate_series(1, :n) g"
                ),
                {**p, "n": entities},
            )
            await c.execute(
                text(
                    "CREATE TEMP TABLE _e ON COMMIT DROP AS SELECT id, row_number() OVER (ORDER BY display_name) rn FROM entities WHERE workspace_id = :ws AND display_name LIKE 'Load Person %'"
                ),
                p,
            )
            await c.execute(text("CREATE UNIQUE INDEX ON _e (rn)"))
            await c.execute(text("ANALYZE _e"))
            await c.execute(
                text(
                    "INSERT INTO facts (id, workspace_id, rel_type, source_entity_id, target_entity_id, valid_from, status, authority_class, source_type, source_command_id, last_command_id, "
                    "source_system, source_ref, ontology_version_id, projection_status, embedding_status) "
                    "SELECT gen_random_uuid(), :ws, 'KNOWS', a.id, b.id, now() - interval '1 day', 'active', 'admin_asserted', 'command', :cmd, :cmd, 'load', 'load', :ov, 'projected', 'skipped_by_policy' "
                    "FROM generate_series(1, :n) g JOIN _e a ON a.rn = (g % :m) + 1 JOIN _e b ON b.rn = ((g * 7) % :m) + 1 WHERE a.id <> b.id"
                ),
                {**p, "n": facts, "m": entities},
            )
            for lo, step in ((0, 20_000),):
                del lo, step
            done = 0
            for n in _chunks(memories, 25_000):
                await c.execute(
                    text(
                        "INSERT INTO curated_memories (id, workspace_id, type, origin, content, summary, status, authority_class, evidence_status, classification, created_by, command_id, "
                        "pipeline_version, candidate_hmac, embedding_status, purpose) "
                        "SELECT gen_random_uuid(), :ws, CASE WHEN g % 3 = 0 THEN 'episodic' ELSE 'semantic' END, 'explicit', "
                        "'topic ' || (g % 97) || ' note number ' || (g + :o) || ' about planning and licensing', 'note ' || (g + :o), 'active', 'user_asserted', 'verified', 'internal', :admin, :cmd, 'p5.v1', "
                        "encode(sha256(convert_to(CAST(:ws AS text) || (g + :o)::text, 'UTF8')), 'hex'), 'ready', 'load' FROM generate_series(1, :n) g"
                    ),
                    {**p, "n": n, "o": done},
                )
                done += n
            await c.execute(
                text(
                    "WITH m AS (SELECT id, row_number() OVER (ORDER BY id) rn FROM curated_memories WHERE workspace_id = :ws AND purpose = 'load') "
                    "INSERT INTO memory_scopes (memory_id, workspace_id, scope_kind, scope_ref) "
                    "SELECT m.id, :ws, CASE WHEN m.rn <= :s THEN 'subject' WHEN m.rn <= :s + 5000 THEN 'workspace' ELSE 'project' END, "
                    "CASE WHEN m.rn <= :s THEN CAST(:sarah AS text) WHEN m.rn <= :s + 5000 THEN '*' ELSE 'load-plan' END FROM m"
                ),
                {**p, "s": ABOUT_SARAH, "sarah": str(sarah)},
            )
            await c.execute(
                text(
                    "INSERT INTO memory_subjects (memory_id, workspace_id, entity_id) SELECT m.id, :ws, CAST(:sarah AS uuid) FROM curated_memories m "
                    "JOIN memory_scopes s ON s.memory_id = m.id AND s.workspace_id = m.workspace_id WHERE m.workspace_id = :ws AND s.scope_kind = 'subject'"
                ),
                {**p, "sarah": str(sarah)},
            )
            await c.execute(
                text(
                    "INSERT INTO memory_embeddings (memory_id, embedding_version, workspace_id, embedding, embedding_model_id, embedding_dim) "
                    "SELECT m.id, 1, :ws, (SELECT array_agg(random() - 0.5) FROM generate_series(1, 384) WHERE m.id IS NOT NULL)::vector, 'load-synthetic', 384 "
                    "FROM curated_memories m WHERE m.workspace_id = :ws AND m.purpose = 'load'"
                ),
                p,
            )
            await c.execute(text("ANALYZE"))
    finally:
        await owner.dispose()
    return Seeded(slug, size, ws, {k: v for k, v in result.new_keys.items()}, str(sarah), entities, facts, memories)


async def rekey(settings: Settings, owner_url: str, slug: str, size: str) -> Seeded:
    """New API keys for an already-seeded workspace (the plaintext of the first ones is gone once printed); no data is touched."""
    engine = create_app_engine(settings, pool_size=2, max_overflow=1)
    result = SeedResult(new_keys={})
    principals = [
        SeedPrincipal(f"admin@{slug}", PrincipalKind.USER, Role.WORKSPACE_ADMIN, "Admin"),
        SeedPrincipal(f"agent@{slug}", PrincipalKind.AGENT, Role.AGENT, "Agent"),
        SeedPrincipal(f"app@{slug}", PrincipalKind.APP, Role.APP_WRITER, "App", trusted_source=True),
    ]
    await seed_workspace(
        engine, slug, f"Load {slug}", principals, pepper=settings.auth_key_pepper.get_secret_value(), rotate_keys=True, result=result
    )
    await engine.dispose()
    ws = workspace_id_for(slug)
    owner = create_async_engine(owner_url)
    try:
        async with owner.connect() as c:
            sarah = (
                await c.execute(text("SELECT id FROM entities WHERE workspace_id = :ws AND display_name = 'Sarah Johnson'"), {"ws": ws})
            ).scalar_one()
            n = (await c.execute(text("SELECT count(*) FROM curated_memories WHERE workspace_id = :ws"), {"ws": ws})).scalar_one()
    finally:
        await owner.dispose()
    entities, facts, memories = SIZES[size]
    return Seeded(slug, size, ws, dict(result.new_keys), str(sarah), entities, facts, int(n) or memories)


def save_credentials(path: Path, seeded: list[Seeded]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    have = json.loads(path.read_text()) if path.exists() else {}
    have.update({s.slug: {"size": s.size, "sarah": s.sarah, "keys": s.keys, "memories": s.memories} for s in seeded})
    path.write_text(json.dumps(have, indent=1))
    path.chmod(0o600)


_ = (asyncio, Any)
