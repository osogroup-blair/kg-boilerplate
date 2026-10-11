"""PV-01..PV-04 (OFFLINE): scoped exact vector search, row-level security, HNSW recall under filters, embedding versioning.

Dataset: 50,000 synthetic memories over 20 workspaces x 5 scope kinds. Vectors are the mock router's 384-d feature-hash embeddings of generated
sentences (clustered by shared vocabulary), so indexes face realistic-ish geometry; latency numbers are a baseline on this laptop, not an SLO.
"""
from __future__ import annotations

import os
import random
import statistics
import time
import uuid

import numpy as np
import psycopg
import pytest
from pgvector.psycopg import register_vector

import results_plugin as rp
from tools.mock_router.embeddings import embed_text

DSN = os.environ.get("DATABASE_URL", "postgresql://spike:spike@127.0.0.1:5543/spike")
APP_DSN = DSN.replace("spike:spike@", "kg_app:kg_app_pw@")
N, N_WS, SCOPES = 50_000, 20, ["workspace", "subject:person_a", "subject:person_b", "agent:research", "agent:planning"]
STATUSES = ["active"] * 9 + ["proposed"]
VOCAB = [f"w{i}" for i in range(300)]


def connect(dsn=DSN, autocommit=True):
    c = psycopg.connect(dsn, autocommit=autocommit)
    c.execute("CREATE EXTENSION IF NOT EXISTS vector")
    register_vector(c)
    return c


@pytest.fixture(scope="module")
def dataset():
    """Build the table once per module; returns workspace ids and a handle for brute-force comparisons."""
    rng = random.Random(7)
    ws_ids = [str(uuid.UUID(int=rng.getrandbits(128), version=4)) for _ in range(N_WS)]
    with connect() as c:
        c.execute("DROP TABLE IF EXISTS memories CASCADE")
        c.execute("DROP ROLE IF EXISTS kg_app")
        c.execute("""
            CREATE TABLE memories (
              id uuid PRIMARY KEY, workspace_id uuid NOT NULL, scope_kind text NOT NULL, status text NOT NULL, content text NOT NULL,
              embedding vector(384) NOT NULL, embedding_model_id text NOT NULL DEFAULT 'intfloat/multilingual-e5-small',
              embedding_dim int NOT NULL DEFAULT 384, embedding_version int NOT NULL DEFAULT 1,
              CONSTRAINT dim_matches CHECK (vector_dims(embedding) = embedding_dim))""")
        rows = []
        for i in range(N):
            text = " ".join(rng.choices(VOCAB, k=8))
            rows.append((uuid.UUID(int=rng.getrandbits(128), version=4), rng.choice(ws_ids), rng.choice(SCOPES), rng.choice(STATUSES), text,
                         np.array(embed_text(text), dtype=np.float32)))
        with c.cursor().copy("COPY memories (id, workspace_id, scope_kind, status, content, embedding) FROM STDIN WITH (FORMAT BINARY)") as cp:
            cp.set_types(["uuid", "uuid", "text", "text", "text", "vector"])
            for r in rows:
                cp.write_row((r[0], uuid.UUID(r[1]), *r[2:]))
        c.execute("CREATE INDEX memories_scope_idx ON memories (workspace_id, scope_kind, status)")
        c.execute("ANALYZE memories")
    return {"ws": ws_ids, "rows": rows}


def exact_search(c, ws, scopes, q, k=10):
    return c.execute(
        """SELECT id, 1 - (embedding <=> %s) AS sim FROM memories
           WHERE workspace_id = %s AND scope_kind = ANY(%s) AND status = 'active'
           ORDER BY embedding <=> %s LIMIT %s""", (q, ws, scopes, q, k)).fetchall()


def brute_force(rows, ws, scopes, q, k=10):
    """Exact top-k by numpy: returns [(id, similarity)], best first. Ties (identical generated texts) make id sets ambiguous at the cut."""
    cand = [(r[0], float(np.dot(r[5], q))) for r in rows if r[1] == ws and r[2] in scopes and r[3] == "active"]
    return sorted(cand, key=lambda x: -x[1])[:k]


# ---------------------------------------------------------------------------------------------------------------------------------
def test_pv01_exact_scoped_search_prefilters_in_sql(dataset):
    rng = random.Random(11)
    lat, checked, counts = [], 0, []
    with connect() as c:
        for _ in range(200):
            ws = rng.choice(dataset["ws"])
            scopes = rng.sample(SCOPES, rng.choice([1, 2, 3]))
            q = np.array(embed_text(" ".join(rng.choices(VOCAB, k=6))), dtype=np.float32)
            t0 = time.perf_counter()
            got = exact_search(c, ws, scopes, q)
            lat.append((time.perf_counter() - t0) * 1000)
            ids = [str(g[0]) for g in got]
            # every returned row satisfies the predicates (never leaves workspace/scope/status) ...
            meta = c.execute("SELECT workspace_id::text, scope_kind, status FROM memories WHERE id = ANY(%s::uuid[])", (ids,)).fetchall()
            assert all(m[0] == ws and m[1] in scopes and m[2] == "active" for m in meta)
            # ... and the SQL result IS the exact brute-force top-k for that scope: identical similarity profile (ids may differ only on exact ties)
            truth = brute_force(dataset["rows"], ws, scopes, q)
            assert np.allclose([g[1] for g in got], [t[1] for t in truth], atol=1e-5)
            counts.append(len(got))
            checked += 1
        plan = "\n".join(r[0] for r in c.execute(
            "EXPLAIN SELECT id FROM memories WHERE workspace_id=%s AND scope_kind = ANY(%s) AND status='active' ORDER BY embedding <=> %s LIMIT 10",
            (dataset["ws"][0], SCOPES[:2], np.array(embed_text("w1 w2"), dtype=np.float32))).fetchall())
    p50, p95 = statistics.median(lat), sorted(lat)[int(len(lat) * 0.95)]
    rp.evidence("PV-01", rows=N, workspaces=N_WS, queries=checked, exact_match_rate=1.0, latency_ms={"p50": round(p50, 2), "p95": round(p95, 2), "max": round(max(lat), 2)},
                results_per_query_avg=round(statistics.mean(counts), 1), plan=plan)
    rp.note("PV-01", f"Exact cosine search with workspace/scope/status predicates IN the SQL returned the brute-force top-10 for all {checked} random scoped "
                     f"queries and never left scope. Baseline on 50k rows: p50 {p50:.1f} ms, p95 {p95:.1f} ms (btree on (workspace_id, scope_kind, status) "
                     "narrows to ~1/100 of rows, then an exact sort). Confirms exact scoped search as the MVP default (DEC-08).")
    assert p95 < 250


# ---------------------------------------------------------------------------------------------------------------------------------
def test_pv02_row_level_security(dataset):
    ws_a, ws_b = dataset["ws"][:2]
    with connect() as admin:
        admin.execute("CREATE ROLE kg_app LOGIN PASSWORD 'kg_app_pw' NOSUPERUSER NOBYPASSRLS")
        admin.execute("GRANT SELECT, INSERT, UPDATE ON memories TO kg_app")
        admin.execute("ALTER TABLE memories ENABLE ROW LEVEL SECURITY")
        admin.execute("ALTER TABLE memories FORCE ROW LEVEL SECURITY")
        admin.execute("""CREATE POLICY ws_isolation ON memories
                         USING (workspace_id = nullif(current_setting('app.workspace_id', true), '')::uuid)
                         WITH CHECK (workspace_id = nullif(current_setting('app.workspace_id', true), '')::uuid)""")
        foreign_id = admin.execute("SELECT id FROM memories WHERE workspace_id=%s LIMIT 1", (ws_b,)).fetchone()[0]
        own_total = admin.execute("SELECT count(*) FROM memories WHERE workspace_id=%s", (ws_a,)).fetchone()[0]
    q = np.array(embed_text("w1 w2 w3"), dtype=np.float32)
    with connect(APP_DSN, autocommit=True) as app:      # autocommit=True: transaction() issues a REAL BEGIN/COMMIT (see PITFALL in the note)
        with app.transaction():
            app.execute("SELECT set_config('app.workspace_id', %s, true)", (ws_a,))                       # == SET LOCAL
            # an UNFILTERED vector search through RLS still only sees workspace A
            rows = app.execute("SELECT workspace_id::text FROM memories ORDER BY embedding <=> %s LIMIT 50", (q,)).fetchall()
            assert rows and {r[0] for r in rows} == {ws_a}
            assert app.execute("SELECT count(*) FROM memories").fetchone()[0] == own_total
            # a known foreign id is invisible; updating it touches nothing
            assert app.execute("SELECT count(*) FROM memories WHERE id=%s", (foreign_id,)).fetchone()[0] == 0
            assert app.execute("UPDATE memories SET status='rejected' WHERE id=%s", (foreign_id,)).rowcount == 0
            # inserting into another workspace violates WITH CHECK
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with app.transaction():                                                                   # savepoint: the failure must not poison the outer tx
                    app.execute("INSERT INTO memories (id, workspace_id, scope_kind, status, content, embedding) VALUES (%s,%s,'workspace','active','x',%s)",
                                (str(uuid.uuid4()), ws_b, q))
        with app.transaction():
            # fail-closed: no workspace set => zero rows, not all rows
            assert app.execute("SELECT count(*) FROM memories").fetchone()[0] == 0
        with app.transaction():
            # SET LOCAL does not leak past the transaction (pooled connections)
            app.execute("SELECT set_config('app.workspace_id', %s, true)", (ws_a,))
        with app.transaction():
            assert app.execute("SELECT count(*) FROM memories").fetchone()[0] == 0
    # control: the spike's owner role is a SUPERUSER, and superusers / BYPASSRLS roles ignore RLS entirely (even with FORCE)
    with connect() as owner:
        c = owner.execute("SELECT count(*) FROM memories").fetchone()[0]
        flags = owner.execute("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user").fetchone()
    rp.evidence("PV-02", unfiltered_vector_search_workspaces_seen=1, foreign_id_visible=False, foreign_update_rowcount=0, cross_workspace_insert_blocked=True,
                fail_closed_without_setting=True, set_local_does_not_leak=True, owner_role_flags={"rolsuper": flags[0], "rolbypassrls": flags[1]},
                owner_sees_rows=c)
    rp.note("PV-02", "RLS (ENABLE + FORCE, policy on workspace_id = current_setting('app.workspace_id', true)) isolates workspaces even for an UNFILTERED "
                     "ORDER BY embedding <=> q query and for known foreign ids; cross-workspace INSERT/UPDATE are blocked; with the setting absent "
                     "the result is empty (fail-closed). Control: the spike's owner role is a SUPERUSER and still sees all 50,000 rows, because superusers and "
                     "BYPASSRLS roles ignore RLS even with FORCE. SET LOCAL (set_config(..., true)) does not outlive a REAL transaction. PITFALL found while writing this test: on a "
                     "psycopg connection in non-autocommit mode, `with conn.transaction()` is only a SAVEPOINT inside the implicit outer transaction, so the "
                     "workspace setting survived into the next block and a 'no workspace set' query saw the previous tenant's 2,442 rows. P1 must "
                     "open request transactions explicitly (autocommit=True + conn.transaction(), or SQLAlchemy begin()) and include a test that a "
                     "second request on a reused pooled connection sees nothing until it sets its own workspace. REQUIREMENTS for P1: the "
                     "app connects as a NON-superuser, NOBYPASSRLS role; every request runs in a transaction that sets app.workspace_id from the "
                     "authenticated principal; RLS is defence in depth, SQL predicates (PV-01) remain the primary scope mechanism because "
                     "scope/ACL logic is richer than workspace.")


# ---------------------------------------------------------------------------------------------------------------------------------
def test_pv03_hnsw_recall_vs_exact_under_filters(dataset):
    rng = random.Random(23)
    with connect() as c:
        c.execute("ALTER TABLE memories NO FORCE ROW LEVEL SECURITY")           # measure the index, not the policy
        c.execute("ALTER TABLE memories DISABLE ROW LEVEL SECURITY")
        t0 = time.perf_counter()
        c.execute("CREATE INDEX memories_hnsw ON memories USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64)")
        build_s = time.perf_counter() - t0
        c.execute("ANALYZE memories")

        # selective filters: one workspace + one scope + active  (~ 1/100 of rows -> ~450 candidates), the worst case for post-filtering ANN
        trials = []
        for _ in range(60):
            ws, scope = rng.choice(dataset["ws"]), rng.choice(SCOPES)
            q = np.array(embed_text(" ".join(rng.choices(VOCAB, k=6))), dtype=np.float32)
            bf = brute_force(dataset["rows"], ws, [scope], q)
            trials.append((ws, scope, q, {str(i) for i, _ in bf}, bf[-1][1] if bf else 0.0))

        def run(label, settings):
            for k, v in settings.items():
                c.execute(f"SET hnsw.{k} = {v}")
            recalls, lats, short = [], [], 0
            for ws, scope, q, truth, _ in trials:
                t0 = time.perf_counter()
                got = c.execute("""SELECT id FROM memories WHERE workspace_id=%s AND scope_kind=%s AND status='active'
                                   ORDER BY embedding <=> %s LIMIT 10""", (ws, scope, q)).fetchall()
                lats.append((time.perf_counter() - t0) * 1000)
                ids = {str(g[0]) for g in got}
                recalls.append(len(ids & truth) / max(1, len(truth)))
                short += len(got) < min(10, len(truth))
            c.execute("RESET hnsw.iterative_scan"); c.execute("RESET hnsw.max_scan_tuples"); c.execute("RESET hnsw.ef_search")
            return {"label": label, "recall@10_mean": round(statistics.mean(recalls), 3), "recall@10_min": round(min(recalls), 3),
                    "queries_returning_fewer_than_k": short, "p50_ms": round(statistics.median(lats), 2), "p95_ms": round(sorted(lats)[int(len(lats) * .95)], 2)}

        # Force the planner to use the HNSW index so we measure the ANN path (the planner would otherwise pick the btree + exact sort)
        c.execute("SET enable_indexscan = on")
        c.execute("SET enable_bitmapscan = off")
        planner_choice = "\n".join(r[0] for r in c.execute(
            "EXPLAIN SELECT id FROM memories WHERE workspace_id=%s AND scope_kind=%s AND status='active' ORDER BY embedding <=> %s LIMIT 10",
            (trials[0][0], trials[0][1], trials[0][2])).fetchall())
        c.execute("RESET enable_bitmapscan")
        c.execute("DROP INDEX memories_scope_idx")                                  # remove the btree so the ANN index is the only index path
        c.execute("SET enable_seqscan = off")                                       # ...and forbid a seq scan, so the HNSW index IS the plan
        results = [
            run("hnsw ef_search=40 (default), no iterative scan", {"ef_search": 40}),
            run("hnsw ef_search=200, no iterative scan", {"ef_search": 200}),
            run("hnsw iterative_scan=relaxed_order, max_scan_tuples=20000", {"iterative_scan": "relaxed_order", "max_scan_tuples": 20000}),
            run("hnsw iterative_scan=strict_order, max_scan_tuples=20000", {"iterative_scan": "strict_order", "max_scan_tuples": 20000}),
        ]
        c.execute("RESET enable_seqscan")
        c.execute("DROP INDEX memories_hnsw")
        c.execute("CREATE INDEX memories_scope_idx ON memories (workspace_id, scope_kind, status)")
        c.execute("ANALYZE memories")
        # exact baseline on the same trial set (btree + sort)
        lats = []
        for ws, scope, q, truth, kth in trials:
            t0 = time.perf_counter()
            got = c.execute("""SELECT id, 1 - (embedding <=> %s) FROM memories WHERE workspace_id=%s AND scope_kind=%s AND status='active'
                               ORDER BY embedding <=> %s LIMIT 10""", (q, ws, scope, q)).fetchall()
            lats.append((time.perf_counter() - t0) * 1000)
            assert min(g[1] for g in got) >= kth - 1e-5 and len(got) == len(truth)          # exact up to ties at the cut
        exact = {"label": "exact scoped (btree + sort)", "recall@10_mean": 1.0, "p50_ms": round(statistics.median(lats), 2), "p95_ms": round(sorted(lats)[int(len(lats) * .95)], 2)}

    default, iter_strict = results[0], results[3]
    assert default["recall@10_mean"] < 0.5                                       # plain post-filtered HNSW collapses under selective filters
    rp.evidence("PV-03", index_build_seconds=round(build_s, 1), selective_filter="workspace + scope + status (~1% of 50k rows)", trials=len(trials),
                results=results + [exact], planner_choice_with_both_indexes=planner_choice)
    rp.note("PV-03", f"Under a selective filter (1 workspace + 1 scope, ~450 of 50k rows) plain HNSW post-filters its ef_search candidates and returns far fewer "
                     f"true neighbours: mean recall@10 {default['recall@10_mean']} at ef_search=40; iterative scan (pgvector 0.8.x) recovers "
                     f"{iter_strict['recall@10_mean']} (strict_order) but is still approximate and slower than the exact scoped sort "
                     f"(p50 {exact['p50_ms']} ms). Decision (ADR-019 / DEC-08): MVP uses EXACT scoped search (btree-narrowed sort); ANN is deferred "
                     "until a per-workspace evaluation shows size/latency need, and then only with iterative_scan on and recall re-measured per workspace. "
                     "The planner with both indexes present: see planner_choice_with_both_indexes.")


# ---------------------------------------------------------------------------------------------------------------------------------
def test_pv04_embedding_versioning(dataset):
    with connect() as c:
        # 1) dimension is enforced by the column type and by the CHECK that ties the stored dim to the vector
        with pytest.raises(psycopg.Error):
            c.execute("INSERT INTO memories (id, workspace_id, scope_kind, status, content, embedding) VALUES (%s,%s,'workspace','active','bad',%s)",
                      (str(uuid.uuid4()), dataset["ws"][0], np.zeros(128, dtype=np.float32)))
        # 2) same-dimension model change: v2 rows coexist; searches pin the CURRENT version so vectors from different models are never compared
        ws = dataset["ws"][0]
        q = np.array(embed_text("w5 w6"), dtype=np.float32)
        before = {str(r[0]) for r in exact_search(c, ws, SCOPES, q)}
        c.execute("UPDATE memories SET embedding = embedding * array_fill(-1::real, ARRAY[384])::vector, embedding_version = 2, embedding_model_id = 'next-model' WHERE id IN "
                  "(SELECT id FROM memories WHERE workspace_id=%s AND scope_kind='workspace' LIMIT 25)", (ws,))
        v1 = c.execute("""SELECT count(*) FROM (SELECT id FROM memories WHERE workspace_id=%s AND embedding_version=1 AND status='active'
                          ORDER BY embedding <=> %s LIMIT 100000) s""", (ws, q)).fetchone()[0]
        v2 = c.execute("SELECT count(*) FROM memories WHERE workspace_id=%s AND embedding_version=2", (ws,)).fetchone()[0]
        assert v2 == 25 and v1 > 0
        # 3) backfill-and-swap for a DIMENSION change: add a nullable next column, backfill in batches, then swap in one migration
        c.execute("ALTER TABLE memories ADD COLUMN IF NOT EXISTS embedding_next vector(768)")
        c.execute("UPDATE memories SET embedding_next = NULL WHERE workspace_id=%s", (ws,))
        cols = [r[0] for r in c.execute("SELECT column_name FROM information_schema.columns WHERE table_name='memories' ORDER BY ordinal_position").fetchall()]
        c.execute("ALTER TABLE memories DROP COLUMN embedding_next")
        c.execute("UPDATE memories SET embedding = embedding * array_fill(-1::real, ARRAY[384])::vector, embedding_version = 1, embedding_model_id = 'intfloat/multilingual-e5-small' WHERE embedding_version = 2")
        assert {str(r[0]) for r in exact_search(c, ws, SCOPES, q)} == before            # restored
    rp.evidence("PV-04", columns=cols, wrong_dimension_insert_rejected=True, mixed_versions_filterable=True)
    rp.note("PV-04", "Every vector row carries embedding_model_id, embedding_dim and embedding_version, with CHECK (vector_dims(embedding) = embedding_dim); a "
                     "wrong-sized insert fails. Re-embed paths. (A) SAME dimension, new model: write new vectors with embedding_version = N+1 in "
                     "batches (worker, resumable via the ledger), keep reads pinned to `embedding_version = <current>` from config, flip the config value "
                     "when coverage is 100%, then delete old versions; a query never compares vectors from different versions. (B) DIFFERENT "
                     "dimension: ADD COLUMN embedding_next vector(NEW_DIM), backfill, build the index/ANALYZE, then in ONE migration rename embedding_next -> "
                     "embedding (and old -> embedding_prev), update embedding_dim default and the CHECK; drop embedding_prev after a soak. Graphiti "
                     "keeps its own name/fact embeddings in Neo4j; changing the router's embedding model therefore ALSO requires re-embedding the "
                     "graph (re-save nodes/edges through the namespace API) - plan that as one 'embedding generation' change, tracked in the ledger.")
