# Phase 5: curated memory

Decisions: [ADR-029](../adr/ADR-029-curated-memory-model-scopes-and-visibility.md) (model, scopes, where visibility is enforced), [ADR-030](../adr/ADR-030-memory-extraction-policy-and-prompt-safety.md) (extraction, creation policy, prompt safety), [ADR-031](../adr/ADR-031-hybrid-scoped-search-and-ranking.md) (hybrid scoped search, `mem-rrf.v1`). Plan: [phase-5-curated-memory.md](../plans/phase-5-curated-memory.md). Run it: `make reset up seed demo-p1 demo-p2 demo-p3 demo-p4 demo-p5`.

## Packages
| Package | Role |
|---|---|
| `kg_memory` (new) | models, policy (`resolve_policy`, `decide`), scope grants and the visibility predicate, store, reads, `MemoryWriter`, `MemorySearch`, `MemoryProposeJob`, `MemoryEmbedJob`, `MemoryExpireJob`, review decisions, re-embed |
| `kg_ontology` | the typed `MemoryPolicy` (every field optional; the P2 keys still parse) |
| `kg_knowledge` | review decisions for memory through a **handler port** (`ReviewService.register_handler`), so it never imports `kg_memory`; `memory.*` command types |
| `kg_ingestion` | pipeline `p5.v1` (`memory_propose` after `graph_ingest`), `replay --upgrade-pipeline`, a stage blocked behind a dead dependency no longer holds the event in `processing` |
| `kg_governance` | six `memory.*` actions; scope grant helpers; closed audit metadata for memory |
| `apps/api` | `/v1/memories` (save, proposals, search, list, get, supersede, expire, dispute, publish), `/v1/scopes` |
| `apps/admin` | Memory Explorer, memory review views, scope grants (read-only) in Agents & Access, the memory stage on the interaction page |

## Data (migration `0005_curated_memory`)
`curated_memories` (immutable content; `UPDATE` only on `status, revision, last_confirmed_at, embedding_status, expires_at, effective_until`), `memory_scopes`, `memory_subjects`, `memory_evidence` (one row per cited thing, a generated `evidence_key` makes a repeat idempotent), `memory_lineage`, `memory_embeddings` (`vector(384)`, `(memory_id, embedding_version)`, no ANN index). `review_items` gains `memory_id` / `competing_memory_id` and the types `memory_proposal`, `memory_conflict`; `outbox_jobs.subject_type` gains `memory`; `principal_scopes` gains the `classification` kind and a unique grant index; `commands` takes the `memory.*` types, all five authority classes, and no ontology version for a memory command. `ops.memory_stats()` feeds the gauges (aggregates only). All six tables have `ENABLE` + `FORCE` RLS.

## Creation paths and what decides the status
```
explicit save   POST /v1/memories            app / admin   needs source + purpose for a shared scope     active, verified (shared procedural by an app: proposed)
proposal        POST /v1/memories/proposals  agent / app   evidence the proposer can SEE, else 422       policy: auto_activate types active; agent shared -> review; procedural -> review
extraction      memory_propose stage         platform      subjects only from the linked entities        active+unverified; procedural -> proposed + review
working         POST /v1/memories type=working  agent      session/agent scope only, TTL                 active at once; expires through a delayed outbox job
publish         POST /v1/memories/{id}/publish  owner      a NEW proposal with published_from lineage    proposal rules; the private original stays private
```
`policy.decide(origin, type, actor, shared, conflict, policy)` is pure and table-tested (`packages/memory/tests/fixtures/policy/decisions.yaml`, with a meta-test for every origin x type x actor). An agent never activates shared procedural memory; a conflict with a higher-authority memory the writer can see is `proposed` + `memory_conflict`.

## The `memory_propose` sequence
```
graph_ingest done (or skipped) ─► memory_propose job
  tx 1  stage lock, gate (consent / egress / classification / policy / extraction disabled -> skipped_by_policy, zero calls), load content, linked entities, episode facts (handles F1..), stage -> running
  model ModelGateway.complete(purpose="memory_extraction", response_schema=MemoryCandidates)  under one hard timeout; unparseable -> retry once -> dead (quarantine)
  pure  select_candidates(...)  every drop has a reason code
  tx 2  one memory.extract ledger command per kept candidate (keyed content hash dedupes), the stage result, Orchestrator.advance   -- ONE transaction
```
A crash before tx 2 asks the model again and the hash keeps one memory (FI-11); a crash after it only acks (FI-12).

## Scoped search (one statement)
`candidates` (workspace, classification ceiling, **visibility predicate over `memory_scopes`**, status, validity window, filters) → `emb` (exact cosine at the pinned version) → `vec` (over `MEMORY_VEC_MIN_COSINE`) and `lex` (`ts_rank_cd`, OR query without function words) → `ranked` (`mem-rrf.v1`) → `LIMIT`. The `EXPLAIN` test pins that `memory_scopes` is read inside the candidate scan. `candidates_considered` counts authorised rows only. A failed query embedding degrades to lexical-only and says so.

## Operations
| Setting | Default |
|---|---|
| `MEMORY_EMBEDDING_VERSION` | 1 (the version reads pin; see [memory-reembed](../runbooks/memory-reembed.md)) |
| `MEMORY_VEC_MIN_COSINE` | 0.2 (a mock-sized placeholder; set from `make eval-memory-live`) |
| `MEMORY_PROPOSE_TIMEOUT_SECONDS` | 120 |
Metrics (closed labels, no ids): `memories_total{type,status}`, `memory_writes_total{origin,outcome}`, `memory_extraction_candidates_total{outcome}`, `memory_search_total{degraded}`, `memory_search_duration_seconds`, `memory_search_candidates`, `memory_embed_total{outcome}`, `memory_embedding_coverage`, `memory_expired_total`. Retry policies: `memory_propose` 5 attempts, `memory_embed` 8 (cap 30 s), `memory_expire` 6. CLI: `kg memories list|show|search|supersede|expire|reembed`, `kg scopes grant|revoke|list`, `kg interactions replay --upgrade-pipeline`, `kg db downgrade --force-drop-memories`. Runbooks: [memory-quarantine](../runbooks/memory-quarantine.md), [memory-reembed](../runbooks/memory-reembed.md).

## Verification
- Offline: unit (policy table, scopes, candidate selection, ontology policy), integration (`test_p5_memory`, `test_p5_extraction`, `test_db_p5`, `test_p5_perf`), security (`test_memory_leaks`: sentinels in agent-private, session, subject-invisible and foreign-workspace places probed by every means; `test_p5_isolation`: route walker over foreign **and** in-workspace invisible ids), resilience FI-11/12, Vitest, Playwright, `make demo-p5` (12 steps) and `make eval-memory` (strict mock scenarios, must be 100%).
- 50,000 memories in 20 workspaces, exact scoped search: p50 ≈ 6 ms, p95 ≈ 18 ms on a loaded laptop (`eval/reports/memory-search-perf.json`).
- **Live (user action, required before P6):** `make verify-router`, then `OSO_ROUTER_API_KEY=... make eval-memory-live` (with the P4 live exit). It records precision and recall against the gold set, the three strict counters (escalation violations, unmapped-subject leaks, procedural auto-activations: 0), recall@3 and distractor-in-top-3 on the labelled queries, per-event cost and latency, and the gold / distractor / filler cosine distribution that sets `MEMORY_VEC_MIN_COSINE`. Proposed baselines: precision ≥ 0.8, recall@3 ≥ 0.9, no distractor in the top 3. Move `memory_extraction` in `compat/model-purposes.yaml` from provisional to accepted when GR-12 confirms the alias.

## Deviations from the plan
1. **Memory ledger commands are not listed by `/v1/commands`** (and `GET /v1/commands/{id}` answers 404 for them): their visibility depends on scopes that endpoint cannot know, and an `app_writer` or `operator` would otherwise see that an agent's private note exists. The literal still names the types (R10).
2. **The dedupe hash includes the scopes**, not only type, content and subjects (see ADR-029 §7).
3. **The example ontology is a second file** (`examples/ontologies/personal-knowledge-memory.yaml`, v1.2.0) rather than a rewrite of `personal-knowledge.yaml`: the P2–P4 demos and tests publish v1.0.0, and v1.0.0 doubles as the proof that the P2 policy keys still parse (R13). Suites written before P5 use v1.0.0 with `extraction: disabled`, so the memory stage skips itself with no model call.
4. **`MemoryPolicy` fields are all optional** and resolved field by field (type -> default -> platform defaults, `kg_memory.policy`), because a stored canonical spec dumps unset fields as null.
5. **Subject grants for Sarah are made by the demo** (`kg scopes grant`), not the seed: she does not exist when the seed runs. The seed grants the curation roles, the app and the project; `agent_support` has none.
6. **An agent cites only its own interactions as evidence** (the existing interaction-read rule), so the demo's research agent submits its own sourced analysis (blueprint evt_005) and cites that, not evt_003.
7. **The extraction prompt cites facts by handle (`F1…`)**, sorted by what they say, not by raw id: a model copies a short handle reliably and a scenario fixture can name it.
8. **`commands.ontology_version_id` is nullable for `memory.*`** and `commands.authority_class` takes all five classes, so an extraction can be recorded as a conversation report.
9. **Downgrade relabels memory jobs** (`cancelled`, `subject_type=command`) instead of deleting them: `job_attempts` is append-only.
10. **Extra review decisions**: `approve_with_edit` (needs `edited_content`; the ledger stores only its hash) and `supersede_existing` join the closed decision set; a supersession needs authority at least equal to the existing memory (`insufficient_authority`).
11. **A pipeline stage blocked behind a dead dependency** (`memory_propose` behind a dead `graph_ingest`) leaves the event `partial`, not `processing` forever.
12. **The lexical query drops English function words** (and one-letter tokens): the `simple` text-search configuration has no stop list, and `a` matched half the workspace.
13. **Roles other than admin/reviewer have no implicit grants**: even the app needs `subject:*` (seeded). Admin and reviewer read every classification by role; everyone else is capped at `confidential` unless granted.
14. Not built (out of scope, as planned): consolidation and propagation of corrections/deletion (P8), grant API and agent registration (P7), the context build and the retrieval history panel (P6), ANN indexes and per-principal RLS (P9).
