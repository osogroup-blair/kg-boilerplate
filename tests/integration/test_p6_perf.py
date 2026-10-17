"""P6 WP9 performance baseline (DEC-11 input). Not a benchmark and not a gate on latency: it records p50/p95 of POST /v1/context/build, end to end through the API (every source, the
mock router), on (1) the demo-sized world and (2) the same workspace after a synthetic bulk load (5,000 entities, 20,000 facts, 50,000 memories), for quick_answer and planning in
structured mode. It fails only if a build is wildly slow (a sequential scan of everything per request would show here)."""

from __future__ import annotations

import json
import statistics
import time
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from tests.conftest import OWNER_URL, require
from tests.support.p4 import strict_mock  # noqa: F401
from tests.support.p6 import ok_build, sarah_world

pytestmark = pytest.mark.integration
REPORT = Path(__file__).resolve().parents[2] / "eval" / "reports" / "context-perf.json"
ENTITIES, FACTS, MEMORIES, ABOUT_SARAH = 5_000, 20_000, 50_000, 120
TASKS = [
    ("quick_answer", "Where does Sarah work"),
    ("quick_answer", "Which cycling club is Sarah in"),
    ("planning", "Help Sarah assess the implications of starting a business"),
    ("planning", "What should Sarah consider about licensing for NewCo"),
]


async def measure(t: Any, sarah: str, n: int) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for profile in ("quick_answer", "planning"):
        wall: list[float] = []
        server: list[float] = []
        tokens: list[int] = []
        for i in range(n):
            task = [x for p, x in TASKS if p == profile][i % 2]
            started = time.perf_counter()
            r = await ok_build(t, "agent", task, subject_ids=[sarah], profile=profile, project="newco-plan")
            wall.append((time.perf_counter() - started) * 1000)
            tokens.append(r["budgets"]["used_tokens"])
            tr = (await t.get(f"/v1/context/{r['context_id']}/trace", "agent")).json()
            server.append(float(tr["latencies"]["total"]) * 1000)
        wall.sort()
        server.sort()
        out[profile] = {
            "builds": n,
            "wall_p50_ms": round(statistics.median(wall), 1),
            "wall_p95_ms": round(wall[max(0, int(n * 0.95) - 1)], 1),
            "server_p50_ms": round(statistics.median(server), 1),
            "server_p95_ms": round(server[max(0, int(n * 0.95) - 1)], 1),
            "tokens_mean": round(statistics.mean(tokens), 1),
        }
    return out


async def seed_bulk(ws: Any, sarah: str, agent_pid: str) -> None:
    owner = create_async_engine(OWNER_URL)
    try:
        async with owner.begin() as c:
            row = (
                await c.execute(
                    text(
                        "SELECT (SELECT id FROM commands WHERE workspace_id = :ws ORDER BY created_at LIMIT 1) AS cmd, "
                        "(SELECT active_version_id FROM workspace_ontology WHERE workspace_id = :ws) AS ov, "
                        "(SELECT id FROM principals WHERE workspace_id = :ws AND handle LIKE 'admin@%') AS admin"
                    ),
                    {"ws": ws},
                )
            ).one()
            p = {"ws": ws, "cmd": row.cmd, "ov": row.ov, "admin": row.admin, "sarah": sarah, "agent": agent_pid}
            await c.execute(
                text(
                    "INSERT INTO entities (id, workspace_id, type_key, display_name, ontology_version_id, created_by_command_id, last_command_id, projection_status, embedding_status) "
                    "SELECT gen_random_uuid(), :ws, 'Person', 'Perf Person ' || g, :ov, :cmd, :cmd, 'projected', 'skipped_by_policy' FROM generate_series(1, :n) g"
                ),
                {**p, "n": ENTITIES},
            )
            await c.execute(
                text(
                    "WITH e AS (SELECT id, row_number() OVER (ORDER BY display_name) rn FROM entities WHERE workspace_id = :ws AND display_name LIKE 'Perf Person %') "
                    "INSERT INTO facts (id, workspace_id, rel_type, source_entity_id, target_entity_id, valid_from, status, authority_class, source_type, source_command_id, last_command_id, "
                    "source_system, source_ref, ontology_version_id, projection_status, embedding_status) "
                    "SELECT gen_random_uuid(), :ws, 'KNOWS', a.id, b.id, now() - interval '1 day', 'active', 'admin_asserted', 'command', :cmd, :cmd, 'perf', 'perf', :ov, 'projected', 'skipped_by_policy' "
                    "FROM generate_series(1, :n) g JOIN e a ON a.rn = (g % :m) + 1 JOIN e b ON b.rn = ((g * 7) % :m) + 1 WHERE a.id <> b.id"
                ),
                {**p, "n": FACTS, "m": ENTITIES},
            )
            await c.execute(
                text(
                    "INSERT INTO facts (id, workspace_id, rel_type, source_entity_id, target_entity_id, valid_from, status, authority_class, source_type, source_command_id, last_command_id, "
                    "source_system, source_ref, ontology_version_id, projection_status, embedding_status) "
                    "SELECT gen_random_uuid(), :ws, 'KNOWS', CAST(:sarah AS uuid), e.id, now() - interval '1 day', 'active', 'admin_asserted', 'command', :cmd, :cmd, 'perf', 'perf', :ov, 'projected', 'skipped_by_policy' "
                    "FROM (SELECT id FROM entities WHERE workspace_id = :ws AND display_name LIKE 'Perf Person %' LIMIT 40) e"
                ),
                p,
            )
            # memories: ~120 about Sarah (the agent may see them), 5,000 workspace-wide and 5,000 in the agent's project (both visible to it), the rest about other people (not)
            await c.execute(
                text(
                    "INSERT INTO curated_memories (id, workspace_id, type, origin, content, summary, status, authority_class, evidence_status, classification, created_by, command_id, "
                    "pipeline_version, candidate_hmac, embedding_status, purpose) "
                    "SELECT gen_random_uuid(), :ws, CASE WHEN g % 3 = 0 THEN 'episodic' ELSE 'semantic' END, 'explicit', "
                    "'topic ' || (g % 97) || ' note number ' || g || ' about planning and licensing', 'note ' || g, 'active', 'user_asserted', 'verified', 'internal', :admin, :cmd, 'p5.v1', "
                    "encode(sha256(convert_to(CAST(:ws AS text) || g::text, 'UTF8')), 'hex'), 'ready', 'perf' FROM generate_series(1, :n) g"
                ),
                {**p, "n": MEMORIES},
            )
            await c.execute(
                text(
                    "WITH m AS (SELECT id, row_number() OVER (ORDER BY id) rn FROM curated_memories WHERE workspace_id = :ws AND purpose = 'perf'), "
                    "e AS (SELECT id, row_number() OVER (ORDER BY display_name) rn FROM entities WHERE workspace_id = :ws AND display_name LIKE 'Perf Person %') "
                    "INSERT INTO memory_scopes (memory_id, workspace_id, scope_kind, scope_ref) "
                    "SELECT m.id, :ws, CASE WHEN m.rn <= :s THEN 'subject' WHEN m.rn <= :s + 5000 THEN 'workspace' WHEN m.rn <= :s + 10000 THEN 'project' ELSE 'subject' END, "
                    "CASE WHEN m.rn <= :s THEN :sarah WHEN m.rn <= :s + 5000 THEN '*' WHEN m.rn <= :s + 10000 THEN 'newco-plan' ELSE CAST(e.id AS text) END "
                    "FROM m LEFT JOIN e ON e.rn = (m.rn % :m) + 1"
                ),
                {**p, "s": ABOUT_SARAH, "m": ENTITIES},
            )
            await c.execute(
                text(
                    "INSERT INTO memory_subjects (memory_id, entity_id, workspace_id, role) "
                    "SELECT s.memory_id, CAST(s.scope_ref AS uuid), :ws, 'subject' FROM memory_scopes s WHERE s.workspace_id = :ws AND s.scope_kind = 'subject' "
                    "AND s.memory_id IN (SELECT id FROM curated_memories WHERE workspace_id = :ws AND purpose = 'perf')"
                ),
                p,
            )
            await c.execute(
                text(
                    "INSERT INTO memory_evidence (id, memory_id, workspace_id, source_kind, external_ref) "
                    "SELECT gen_random_uuid(), id, workspace_id, 'external_ref', 'perf' FROM curated_memories WHERE workspace_id = :ws AND purpose = 'perf'"
                ),
                p,
            )
            await c.execute(
                text(
                    "INSERT INTO memory_embeddings (memory_id, embedding_version, workspace_id, embedding, embedding_model_id, embedding_dim) "
                    "SELECT id, 1, workspace_id, ARRAY(SELECT random() - 0.5 FROM generate_series(1, 384))::vector, 'perf', 384 FROM curated_memories WHERE workspace_id = :ws AND purpose = 'perf'"
                ),
                p,
            )
            await c.execute(
                text(
                    "ANALYZE entities; ANALYZE facts; ANALYZE curated_memories; ANALYZE memory_scopes; ANALYZE memory_subjects; ANALYZE memory_embeddings; ANALYZE memory_evidence"
                )
            )
    finally:
        await owner.dispose()


async def clear_bulk(ws: Any) -> None:
    owner = create_async_engine(OWNER_URL)
    try:
        async with owner.begin() as c:
            p = {"ws": ws}
            await c.execute(text("DELETE FROM context_items WHERE workspace_id = :ws"), p)
            await c.execute(text("DELETE FROM context_debug_snapshots WHERE workspace_id = :ws"), p)
            await c.execute(text("DELETE FROM context_traces WHERE workspace_id = :ws"), p)
            await c.execute(text("DELETE FROM memory_embeddings WHERE workspace_id = :ws AND embedding_model_id = 'perf'"), p)
            await c.execute(text("DELETE FROM memory_evidence WHERE workspace_id = :ws AND external_ref = 'perf'"), p)
            await c.execute(
                text(
                    "DELETE FROM memory_subjects WHERE workspace_id = :ws AND memory_id IN (SELECT id FROM curated_memories WHERE workspace_id = :ws AND purpose = 'perf')"
                ),
                p,
            )
            await c.execute(
                text(
                    "DELETE FROM memory_scopes WHERE workspace_id = :ws AND memory_id IN (SELECT id FROM curated_memories WHERE workspace_id = :ws AND purpose = 'perf')"
                ),
                p,
            )
            await c.execute(text("DELETE FROM curated_memories WHERE workspace_id = :ws AND purpose = 'perf'"), p)
            await c.execute(text("DELETE FROM facts WHERE workspace_id = :ws AND source_system = 'perf'"), p)
            await c.execute(text("DELETE FROM entities WHERE workspace_id = :ws AND display_name LIKE 'Perf Person %'"), p)
    finally:
        await owner.dispose()


async def test_context_build_latency_baseline(api, tenant_ctx, strict_mock):  # noqa: F811
    require(OWNER_URL, "postgres")
    t = tenant_ctx
    ids = await sarah_world(api, t)
    await measure(t, ids["sarah"], 4)  # warm the connections and the driver
    demo = await measure(t, ids["sarah"], 24)
    try:
        await seed_bulk(t.ws, ids["sarah"], await t.principal_id("agent"))
        await measure(t, ids["sarah"], 4)
        bulk = await measure(t, ids["sarah"], 24)
    finally:
        await clear_bulk(t.ws)
    report = {
        "date": time.strftime("%Y-%m-%d"),
        "router": "mock",
        "mode": "structured",
        "demo_workspace": {"entities": 4, "memories": "≈10", "builds_per_profile": 24, **demo},
        "synthetic_workspace": {
            "entities": ENTITIES,
            "facts": FACTS,
            "memories": MEMORIES,
            "visible_to_the_agent": f"~{ABOUT_SARAH} about the subject + 5,000 workspace-wide + 5,000 in its project",
            "builds_per_profile": 24,
            **bulk,
        },
        "note": "end to end through the API in one process, mock router, Neo4j holds only the demo world; p50/p95 are recorded for DEC-11, never gated tighter than 'not pathological'",
    }
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    for dataset in (demo, bulk):
        for profile in ("quick_answer", "planning"):
            assert dataset[profile]["wall_p95_ms"] < 8000, report
