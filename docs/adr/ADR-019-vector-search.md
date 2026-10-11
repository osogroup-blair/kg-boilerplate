# ADR-019: Vector search: exact scoped search plus RLS at MVP

**Status:** Accepted  **Decision refs:** DEC-08, risk "tenant vector recall", SEC-01  **Evidence:** PV-01, PV-02, PV-03, GR-10

## Findings (50,000 synthetic memories, 20 workspaces, 5 scope kinds, 384-dim)
- **Exact scoped search** with workspace/scope/status predicates in SQL (btree on `(workspace_id, scope_kind, status)` then exact sort) returned the brute-force top-10 for 200/200 random scoped queries and never left scope; p50 ≈ 2.5 ms, p95 ≈ 9.7 ms.
- **HNSW under a selective filter (~1% of rows)** collapses: mean recall@10 0.03 at `ef_search=40` and every query returned fewer than 10 results; `ef_search=200` 0.17; `iterative_scan` (pgvector 0.8.x) recovers 0.67–0.74 but is still approximate and ~20× slower than the exact scoped query on the same trials (p50 28–31 ms vs 1.4 ms).
- **RLS** isolates workspaces for unfiltered vector queries, known foreign ids, updates and cross-workspace inserts, and is fail-closed when no workspace is set.

## Decision
1. **MVP uses exact scoped search.** No ANN index until a per-workspace evaluation shows a latency/size need; then only with `hnsw.iterative_scan` on and recall re-measured per workspace (never trust global recall).
2. **Scope is enforced in SQL predicates** (workspace + scope ACL + status); never filter after retrieval.
3. **RLS is defence in depth** on every memory table: policy on `workspace_id = current_setting('app.workspace_id', true)`, `ENABLE` + `FORCE`. P1 requirements: the app role is NOT superuser and NOT BYPASSRLS (superusers ignore RLS: the spike's owner saw all 50,000 rows); every request sets `app.workspace_id` with `set_config(..., true)` inside an explicit transaction. **Pitfall found:** on a non-autocommit connection `conn.transaction()` is only a savepoint, so the setting leaked into the next block; use autocommit + explicit transactions (or SQLAlchemy `begin()`) and test a reused pooled connection.
4. **Graph search recipes without a cross-encoder** (GR-10): `EDGE_HYBRID_SEARCH_RRF` (default), `EDGE_HYBRID_SEARCH_NODE_DISTANCE` with `center_node_uuid` for subject-centred retrieval, `NODE_HYBRID_SEARCH_RRF` for entities; MMR variants work. Always pass an explicit config (Graphiti's `search_` defaults to the cross-encoder recipe) and copy the recipe per call (`Graphiti.search` mutates `recipe.limit`). A `NoCrossEncoder` that raises replaces the default reranker so any accidental use fails loudly.

## Consequences
Latency/DEC-11 baseline: exact scoped search is a few ms at 50k rows on a laptop; revisit at ≥10× that size.

> **Note (Phase 1, L18):** the PV-01 latency above (p50 ≈ 2.5 ms, p95 ≈ 9.7 ms) and `docs/compatibility-report.md` (p50 9.0 ms, p95 63.2 ms) come from different runs on a shared laptop: this is run-to-run variance, not a contradiction. P1 makes no latency claims; P9 measures baselines on a fixed seed.
