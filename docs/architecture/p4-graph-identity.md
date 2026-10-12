# Phase 4: Graphiti processing, identity and temporal facts

Decisions: [ADR-026](../adr/ADR-026-graphiti-extraction-behaviour.md) (what Graphiti does), [ADR-027](../adr/ADR-027-reconcile-then-restore-and-authority.md) (model output is a proposal), [ADR-028](../adr/ADR-028-identity-resolution-and-merge.md) (identity). Plan: [phase-4-graph-identity-time.md](../plans/phase-4-graph-identity-time.md). Run it: `make reset up seed demo-p1 demo-p2 demo-p3 demo-p4`.

## Packages
| Package | Role |
|---|---|
| `kg_extraction` (new) | `GraphIngestJob` (the `graph_ingest` stage), `Reconciler`, identity helpers, error classification |
| `kg_ontology.conflicts` | the pure conflict/authority policy (`resolve`), tested from `tests/fixtures/conflicts/policy.yaml` |
| `kg_graph` | `ScopedGraph` extensions (episodes, similar nodes, merge, `search_facts`), `PlatformLLMClient`, ontology compile (sensitive attributes excluded) |
| `kg_knowledge` | `ExtractedKnowledge` (writes extracted facts, evidence, mentions, claims), `ReviewService`, `merge_entity`, `GraphSearch`, `GraphLock`, projector restore |
| `kg_ingestion` | per-stage pins (`graph_ingest` enabled), `repin` replay, `graph_episode` derivative |
| `apps/api` | `/v1/reviews`, `/v1/graph/search`, `/v1/entities/{id}/graph|mentions|attribute-claims|summary`, `/v1/relationships/{id}/evidence` |
| `apps/admin` | Graph Explorer (Cytoscape.js 3.33.1, pinned), Review Queue v1, status/evidence badges |

## The `graph_ingest` handler
```
normalize succeeded ─► graph_ingest job (concurrency_key graph:{ws})
  tx 1  load head, pinned ontology, subjects, normalized content; consent/classification gate (zero model calls when denied); stage -> running
  lock  GraphLock (Postgres advisory, per workspace)
  graph ensure_episode_node(uuid5 "{interaction}:g{gen}")  ──►  episode_has_results?
          yes (re-run): read stored result, NO model call (EX-04)
          no          : add_episode_scoped under egress(purpose=graph_extraction) + asyncio.timeout
  tx 2  Reconciler: identity (link / fork), conflict policy per fact, evidence rows, mentions, claims, reviews, reverts
  graph restore the episode's nodes/edges from Postgres (state-based projector), then enqueue/complete
  stage result {episode_uuid, entities, facts, reverts, name_variants, reviews}; pipeline advances (indexed / partial)
```
A crash anywhere is healed by the retry: before Graphiti saved → the model runs again; after → the stored result is reconciled, no model call (FI-09, FI-10).

## Conflict policy (summary of `resolve`)
| Incoming (extracted) vs existing | Outcome |
|---|---|
| nothing on the same relationship | `active`, `unverified` |
| relationship type / endpoint outside the ontology | `rejected` (`out_of_ontology`), kept for audit |
| same fact, same authority | evidence added to the existing fact; no new fact |
| cardinality conflict with a verified (higher authority) fact | `proposed` + `contradiction` review; Graphiti's invalidation reverted |
| cardinality conflict with an equal-authority fact | `proposed` + review |
| succession in time (cardinality 1, new starts after old ends) from a verified source | old valid time ended, no review |
| endpoint is an unresolved candidate | `proposed` (`identity_pending`) |
| dated by the text | `valid_from` from the text (unknown stays `valid_from_unknown`), record time = platform time |
The exact table is the YAML fixture; a meta-test requires every branch to have a case.

## Identity flow
mention → (subject_ref / actor, or name variant anchored to a unique same-type subject) → **link** (alias added) | else **fork** to a candidate entity + `identity_match` review → reviewer: `same_entity` (merge command) | `different_entity` (candidate activated, `identity_non_matches`) | `unsure`.

## Lock model
One advisory lock per workspace serialises `graph_ingest`, `graph_projection`, merges and review decisions on the graph. Jobs wait up to `GRAPH_INGEST_LOCK_WAIT_SECONDS` then retry as `graph_busy`; the command API waits 0.25 s, then returns 202 and lets the outbox finish the projection.

## Reviews
Types: `contradiction`, `identity_match`, `policy_violation` (an out-of-ontology or over-authority proposal), plus the P2 types. A decision is a ledger command (`review.decide`) with `If-Match` + `Idempotency-Key`; `rationale_code` is a closed enum, `rationale_text` is stored only on the review row. Evidence for reviewers is a narrow, audited read.

## Operations
| Setting | Default |
|---|---|
| `IDENTITY_TRGM_MIN` / `IDENTITY_COS_MIN` | 0.45 / 0.92 (cosine to be finalised by the live eval) |
| `GRAPH_INGEST_TIMEOUT_SECONDS` | 600 |
| `GRAPH_INGEST_LOCK_WAIT_SECONDS` | see `.env.example` |
| `GRAPH_PREVIOUS_EPISODES` | 0 |
| `GRAPH_STORE_EPISODE_CONTENT` | true |
Metrics: `graph_ingest_*`, `extraction_*`, `reviews_created_total`, `review_decisions_total`, `review_items_open`, `review_age_seconds`, `graph_lock_wait_seconds`, `graph_search_*` (no identifiers in labels). The P2 projection sweeper and `ops.workspaces_with_pending_projection` are gone; the outbox is the only driver.

## Verification
- Offline: unit + integration + security + resilience (`FI-09`, `FI-10`), `make eval-extraction` (strict mock scenarios, must be 100%), `make demo-p4` (13 steps).
- **Live (user action, required before P6):** `make verify-router` then `make eval-extraction-live` (needs `OSO_ROUTER_API_KEY`, costs model calls). It writes `eval/reports/extraction-live-<date>.{json,md}`: fact precision/recall, identity decisions, reverts, cost and latency per episode, and the related/unrelated E5 cosine distributions. Thresholds in `eval/extraction/run_eval.py` are proposed baselines (precision and recall ≥ 0.8, identity decisions 1.0, missed reverts / cross-workspace links / un-quarantined unparseable outputs 0); set `IDENTITY_COS_MIN` to the suggested midpoint and record the result in ADR-012/013.

## Deviations from the plan
1. The pure conflict policy lives in `kg_ontology` (not `kg_extraction`), because `kg_knowledge` (which applies review decisions) may not import `kg_extraction`; `kg_extraction.conflicts` re-exports it.
2. Review items reuse the existing columns plus a `detail jsonb`; the new review types are enum additions in migration 0004.
3. `previous_episode_uuids` defaults to empty (`GRAPH_PREVIOUS_EPISODES=0`).
4. Sensitive attributes are excluded from the types Graphiti sees (they never reach the model).
5. A derived-summary endpoint (`GET /v1/entities/{id}/summary`) was added so Graphiti's summaries are never confused with facts.
6. The Graph Explorer is not offered to agent roles in the UI (the API already returns 403 for search).
7. `tools/mock_router/fixtures/sarah.json` is kept (loaded after the scenario files) for the P0 spike tests.
8. Strict fixture mode is toggled at runtime through `POST /__mock/strict` (the compose default is off; demo-p4 and the strict test fixture switch it on and restore it).
9. Merge commands created from deterministic links record the submitter as actor and `projection_status=not_applicable`.
10. `model_calls` rows are buffered; at most ~2 s of call log can be lost on a hard crash (the stage flushes before it records its result).
11. Deferred: restoring the Graphiti summary snapshot for a forked node (the summary is regenerated by the next episode).
