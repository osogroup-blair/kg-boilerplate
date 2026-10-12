# ADR-026: Graphiti extraction behaviour and how the platform drives it

**Status:** Accepted  **Decision refs:** ADR-012 (error categories), ADR-015 (graph tenancy), ADR-025 (outbox)  **Evidence:** `tests/integration/test_p4_extraction.py`, `tools/mock_router/fixtures/scenarios/`, `eval/extraction`, demo-p4

## Context
Phase 4 turns a ledger interaction into candidate entities and facts by running Graphiti's `add_episode` (0.30.2) with the workspace's compiled ontology. Graphiti was designed to own its graph. The platform needs Postgres to own it (ADR-021), so every behaviour below had to be known before the handler could be designed. The checks ran against Neo4j 5 and the strict mock router (any structured request without a matching fixture answers `500 fixture_missing` with its `schema_name`).

## Findings (EX-01..07)
| Check | Finding | Consequence |
|---|---|---|
| EX-01 | `add_episode(uuid=…)` continues an episode node that already exists (it does not create a second). The result lists the episode's nodes and edges **including edges it invalidated**. | The platform pre-creates the episode node with a deterministic uuid (`uuid5("{interaction_id}:g{generation}")`) and reads the result back. |
| EX-02 | When an extracted mention resolves to an existing node, Graphiti rewrites that node's `name`, attributes and `summary`; it also dedupes by **exact name**, so two different people called "Sarah Johnson" collapse into one node. | Graphiti output is a proposal (ADR-027): the projector restores canonical name and attributes from Postgres and preserves the Graphiti summary. Unvouched mentions are *forked* to a candidate entity (ADR-028). |
| EX-03 | A contradiction invalidates (`invalid_at`/`expired_at`) the older edge **only when both edges carry `valid_at` and the candidate is older**. Out-of-range `contradicted_facts` indexes discard the whole answer. | A conversation claim can end a command-sourced edge in the graph; the reconciler detects it and reverts (KGR-03). Fixtures need per-index contradiction rules. |
| EX-04 | A re-run of a completed episode is detectable from its `MENTIONS`/`entity_edges`. With the pre-created uuid, the handler reads the stored result and makes **zero chat calls**. | A crash after Graphiti saved (FI-09) costs no model calls on retry. |
| EX-05 | `update_communities=False` produces no community nodes. | Always off. |
| EX-06 | `store_raw_episode_content=False` blanks the stored episode body after extraction (the model still receives the text of the episode being processed). Previous-episode context is passed only when `previous_episode_uuids` is non-empty. | `GRAPH_STORE_EPISODE_CONTENT` (default true, inside the trusted boundary); `GRAPH_PREVIOUS_EPISODES` defaults to **0**: each episode is extracted alone, which keeps the fixture set and the cost predictable. |
| EX-07 | Per episode with compiled types Graphiti sends: `ExtractedEntities`, `NodeResolutions` (only when candidates exist), `ExtractedEdges`, `EdgeDuplicate` (one per new edge, keyed by the new fact text), `EdgeTimestamps`, `SummarizedEntities`. | These are the `schema_name`s the scenario fixtures match. A fixture keyed on loose text matches other facts' candidate lists, so keys are `"<new fact> …"`. |

Two operational findings: Graphiti's own retry waits 5–120 s on bad JSON (a graph lock would be held for minutes), and entity type ids are **positional** while jsonb reorders keys.

## Decision
1. `graph_ingest` runs one episode per interaction generation under the per-workspace lock (ADR-027), through `ScopedGraph.add_episode_scoped`, which refuses any option the platform does not set (no communities, no raw `group_id`, no extra kwargs).
2. The model client is `PlatformLLMClient`: **two attempts**, 1–8 s wait, then an error the handler classifies. An unparseable answer is retried once, then dead-lettered as `extraction_unparseable` (config-terminal, ING-03). `asyncio.timeout` (`GRAPH_INGEST_TIMEOUT_SECONDS`, 600) maps to `extraction_timeout`.
3. Every Neo4j or router call in the stage sits inside the error-mapping block, so an outage is `graph_unavailable` (retryable), never `unexpected_error`.
4. The ontology compiler (`kg_graph.compile`) sorts entity types by key (stable positional ids) and **excludes sensitive attributes** from the types Graphiti sees, so they are never sent to the model.
5. Consent or classification that forbids external processing makes the stage `skipped_by_policy` with zero model calls (ING-06).

## Consequences
- The strict mock router with scenario files is the offline gate; `make eval-extraction` must score 100% on it. Real-model quality is measured only by `make eval-extraction-live` (user action), which finalises the thresholds and `IDENTITY_COS_MIN`.
- A Graphiti upgrade re-runs EX-01..07 (the scenarios fail loudly on a changed `schema_name` or output shape).
- Summaries written by Graphiti are derived text, not facts; they are served only through `GET /v1/entities/{id}/summary`, labelled derived.
