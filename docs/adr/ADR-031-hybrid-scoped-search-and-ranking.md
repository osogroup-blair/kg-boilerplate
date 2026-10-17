# ADR-031: Hybrid scoped memory search and the `mem-rrf.v1` ranker

**Status:** Accepted  **Decision refs:** ADR-013 (E5 prefixes, versions), ADR-019 (exact scoped search), RET-02/RET-04, SEC-02  **Evidence:** `tests/integration/test_p5_memory.py`, `tests/integration/test_db_p5.py` (EXPLAIN), `tests/integration/test_p5_perf.py`, `tests/security/test_memory_leaks.py`, `make eval-memory`

## Context
Retrieval for agents must be relevant, explainable and stable, and it must never leak. Filtering after an unscoped candidate list exists leaks through candidates, scores, counts and timings.

## Decision
1. **One SQL statement** authorises, filters and ranks (`MemorySearch`): `candidates` (workspace + classification ceiling + **scope visibility** + status + validity window + type/subject/project filters) → `emb` (exact cosine at the pinned embedding version) → `vec` (over the cosine floor, ranked) and `lex` (`ts_rank_cd` over the generated `simple` tsvector of summary + content, an OR query of the topic words) → `ranked` (reciprocal-rank fusion) → `LIMIT`. Nothing reads `curated_memories` except through `candidates`.
2. **Exact cosine, no ANN index** (ADR-019): a btree on `(workspace_id, status, type)` plus the scope join. The size trigger for revisiting is ≥ 200,000 memories per workspace or p95 > 100 ms, then only with `hnsw.iterative_scan` and per-workspace recall. The 50,000-memory smoke test records p50/p95 in `eval/reports/memory-search-perf.json`.
3. **`mem-rrf.v1`:** `score = 1/(60+rank_lex) + 1/(60+rank_vec) + 0.002·importance + 0.001·ln(1+evidence)`, plus `recency_weight·0.01·exp(-age/90d)` **only when requested** (recency is not validity). Ties break by id, so the same request on the same data returns the same order and scores (CTX-06 groundwork). Importance, confidence and evidence count are tie-breakers, never truth.
4. **Validity and status.** Default: `active`, `effective_from <= now < effective_until`, `expires_at` not passed. `as_of` additionally admits `superseded` and `expired` memory whose validity covered that instant. `include=proposed|disputed` needs `memory.curate`.
5. **E5 handling.** Memories embed as `passage:` (summary and content when the summary adds words), queries as `query:`, both through `ModelGateway.embed` (chunk/pool beyond 512 tokens). A query is labelled with the caller's classification ceiling (capped at `restricted`, since `local_only` is never embedded).
6. **Degradation is visible.** If the query cannot be embedded (router outage, egress denied) the same statement runs lexical-only and the response carries `degraded: ["vector_unavailable"]` and `embedding_coverage`; it never falls back to an unscoped path. `local_only` memories are never embedded (`skipped_by_policy`) and are found lexically only.
7. **The vector half has a cosine floor** (`MEMORY_VEC_MIN_COSINE`, default 0.2) so unrelated memories do not enter the ranking by noise. E5 similarities cluster high, so the live value is set from the data `make eval-memory-live` records (gold / distractor / filler cosine distribution), not from the mock.
8. **Versioned embeddings (PV-04 path A).** `memory_embeddings(memory_id, embedding_version)`; reads pin `MEMORY_EMBEDDING_VERSION`; `kg memories reembed --to-version N` writes the new version while reads stay pinned; switch when coverage is 100%.

## Consequences
- `candidates_considered` is a count of what the caller may see, so it also tells a caller nothing about what it may not.
- The ranker is intentionally simple and explainable (`components` per hit); P6's context composer consumes the same method and adds budgeting.
