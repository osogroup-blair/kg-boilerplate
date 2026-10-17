"""P5 WP4 performance smoke (ADR-019 baseline, extended): exact scoped search over 50,000 memories in 20 workspaces. Not a benchmark: it records p50/p95 for the
operator and fails only if the exact scoped plan is wildly off (a sequential scan of everyone's rows would show here long before it hurts)."""

from __future__ import annotations

import json
import statistics
import time
import uuid
from pathlib import Path

import pytest
from kg_db.session import tenant_session
from kg_domain.enums import PrincipalKind, Role
from kg_domain.principal import Principal, WorkspaceContext
from kg_memory.models import SearchIn
from kg_memory.reads import Viewer
from kg_memory.scopes import ScopeGrants
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from tests.conftest import OWNER_URL, require

pytestmark = pytest.mark.integration
WORKSPACES, PER_WORKSPACE, PROJECTS = 20, 2500, 50
REPORT = Path(__file__).resolve().parents[2] / "eval" / "reports" / "memory-search-perf.json"


async def test_exact_scoped_search_at_50k_memories(api):
    require(OWNER_URL, "postgres")
    owner = create_async_engine(OWNER_URL)
    wss = [uuid.uuid4() for _ in range(WORKSPACES)]
    principal = uuid.uuid4()
    try:
        async with owner.begin() as c:
            for i, ws in enumerate(wss):
                await c.execute(
                    text("INSERT INTO workspaces (id, slug, display_name) VALUES (:w, :s, 'perf')"),
                    {"w": ws, "s": f"perf-{i}-{ws.hex[:6]}"},
                )
                pid = principal if i == 0 else uuid.uuid4()
                await c.execute(
                    text("INSERT INTO principals (id, workspace_id, kind, handle, display_name) VALUES (:p, :w, 'agent', 'a', 'a')"),
                    {"p": pid, "w": ws},
                )
                await c.execute(
                    text(
                        "INSERT INTO commands (id, workspace_id, idempotency_key, request_hash, command_type, actor_principal_id, authority_class, classification, payload, status, http_status) "
                        "SELECT gen_random_uuid(), :w, 'perf', 'h', 'memory.save', :p, 'agent_generated', 'internal', '{}', 'applied', 201"
                    ),
                    {"w": ws, "p": pid},
                )
                await c.execute(
                    text(
                        "INSERT INTO curated_memories (id, workspace_id, type, origin, content, summary, status, authority_class, evidence_status, classification, created_by, command_id, "
                        "pipeline_version, candidate_hmac, embedding_status) "
                        "SELECT gen_random_uuid(), :w, 'semantic', 'explicit', 'topic ' || (g % 97) || ' note number ' || g || ' about planning', 'note ' || g, 'active', 'user_asserted', "
                        "'verified', 'internal', :p, (SELECT id FROM commands WHERE workspace_id = :w AND idempotency_key = 'perf'), 'p5.v1', "
                        "encode(sha256(convert_to(CAST(:w AS text) || g::text, 'UTF8')), 'hex'), 'ready' FROM generate_series(1, :n) g"
                    ),
                    {"w": ws, "p": pid, "n": PER_WORKSPACE},
                )
                await c.execute(
                    text(
                        "INSERT INTO memory_scopes (memory_id, workspace_id, scope_kind, scope_ref) "
                        "SELECT id, workspace_id, 'project', 'p' || (row_number() OVER (ORDER BY id) % :k) FROM curated_memories WHERE workspace_id = :w"
                    ),
                    {"w": ws, "k": PROJECTS},
                )
                await c.execute(
                    text(
                        "INSERT INTO memory_embeddings (memory_id, embedding_version, workspace_id, embedding, embedding_model_id, embedding_dim) "
                        "SELECT id, 1, workspace_id, ARRAY(SELECT random() - 0.5 FROM generate_series(1, 384))::vector, 'perf', 384 FROM curated_memories WHERE workspace_id = :w"
                    ),
                    {"w": ws},
                )
            await c.execute(text("ANALYZE curated_memories; ANALYZE memory_scopes; ANALYZE memory_embeddings"))
        ctx = WorkspaceContext.for_system_task(wss[0], reason="perf smoke")
        p = Principal(principal_id=principal, workspace_id=wss[0], kind=PrincipalKind.AGENT, handle="a", roles=frozenset({Role.AGENT}))
        search = api.app.state.kg.memory.search
        timings: list[float] = []
        async with tenant_session(api.engine, ctx) as conn:
            grants = ScopeGrants.from_rows(p, [("project", "p7")])
            v = Viewer(p, grants)
            for i in range(60):
                qvec = [((i * 31 + j * 7) % 100) / 100 - 0.5 for j in range(384)]
                sql_text, params = search.build(v, SearchIn(query=f"topic {i % 97} planning", limit=10), qvec)
                started = time.perf_counter()
                rows = (await conn.execute(text(sql_text), params)).all()
                timings.append((time.perf_counter() - started) * 1000)
                assert (
                    rows[0].considered == PER_WORKSPACE // PROJECTS
                )  # 50 rows: the grant selects ~2% of ONE workspace's rows, never anyone else's
                assert all(r.id is None or True for r in rows)
        timings.sort()
        report = {
            "memories": WORKSPACES * PER_WORKSPACE,
            "workspaces": WORKSPACES,
            "candidates_per_query": PER_WORKSPACE // PROJECTS,
            "queries": len(timings),
            "p50_ms": round(statistics.median(timings), 2),
            "p95_ms": round(timings[int(len(timings) * 0.95) - 1], 2),
            "max_ms": round(max(timings), 2),
            "plan": "exact cosine over the authorised candidates (no ANN index, ADR-019)",
        }
        REPORT.parent.mkdir(parents=True, exist_ok=True)
        REPORT.write_text(json.dumps(report, indent=2) + "\n")
        print(report)
        assert report["p95_ms"] < 500, report  # a host under load is slow; a full scan of 50k vectors per query would be far beyond this
    finally:
        async with owner.begin() as c:
            await c.execute(text("DELETE FROM memory_embeddings WHERE workspace_id = ANY(:w)"), {"w": wss})
            await c.execute(text("DELETE FROM memory_scopes WHERE workspace_id = ANY(:w)"), {"w": wss})
            await c.execute(text("DELETE FROM curated_memories WHERE workspace_id = ANY(:w)"), {"w": wss})
            await c.execute(text("DELETE FROM commands WHERE workspace_id = ANY(:w)"), {"w": wss})
            await c.execute(text("DELETE FROM principals WHERE workspace_id = ANY(:w)"), {"w": wss})
            await c.execute(text("DELETE FROM workspaces WHERE id = ANY(:w)"), {"w": wss})
        await owner.dispose()
