# Knowledge & Memory Platform — High-Level Implementation Roadmap

## Context

`solution_inputs/knowledge-memory-platform-blueprint/` (v0.1.0) specifies a reusable multi-agent knowledge and memory boilerplate:
- **Neo4j + Graphiti** for episodes and the temporal graph.
- **PostgreSQL + pgvector** for the ledger, curated memory, policies, traces and audit.
- A **FastAPI modular monolith with workers**.
- A **React admin console**.
- A Python SDK and an MCP server.

The product is `POST /v1/context/build`, a bounded, attributable, permission-filtered context package. The repository is empty apart from the blueprint, so this is greenfield work.

This roadmap orders the work into phases. Each phase gets its own detailed implementation plan before a coding agent executes it. The roadmap ends with fully working examples.

**Decisions captured from the user**
- **LLM and embeddings:** all model calls go through the user's **model router**, which exposes **OpenAI-compatible** endpoints (chat/completions and embeddings) and chooses the model itself. The platform has one `ModelGateway` adapter (base_url + key). Graphiti is wired to the router through its OpenAI-compatible LLM client and embedder. This settles DEC-02 at the platform level.
- **Admin UI:** built **incrementally**. Each phase ships the screens it needs, so every demo is visible in the UI.
- **Final examples:** the Sarah two-agent scenario, a second-domain template, real agent integrations (SDK + MCP), and Jupyter notebook walkthroughs.

**Blueprint slices mapped to phases:**

| Blueprint slice | Roadmap phase |
|---|---|
| Spike (new) | P0 |
| S0 | P1 |
| S1 | P2 |
| S2 | P3 |
| S3 | P4 |
| S4 | P5 |
| S5 | P6 |
| S6 | P7 |
| S7 | P8 |
| S8 | P9 |
| Examples (new) | P10 |

## Dependency Graph

```
P0 Spike ─► P1 Foundation ─┬─► P2 Ontology ──────┐
                            └─► P3 Ledger/Outbox ─┼─► P4 Graph+Time ─┐
                                        P3 ──────► P5 Memory ─────────┼─► P6 Context (MVP) ─► P7 Multi-Agent ─┐
                                                   P4 + P5 ──────────────► P8 Governance ─────────────────────┼─► P9 Harden ─► P10 Examples
```

Phases that can run in parallel: **P2 ∥ P3** (both need only P1), **P4 ∥ P5** (P5 needs P3 only), and **P7 ∥ P8**.

---

## Cross-Cutting Rules (apply to every phase plan)

- **Security first:**
  - Workspace and scope come from the authenticated principal.
  - Prefilter at the SQL and Cypher boundary, never filter afterwards.
  - Every phase adds negative and leakage tests for what it introduces.
- **Traceability:** every phase plan maps its work to the blueprint IDs (`CAP-*`, `ONT/KGR/EVD/MEM/CTX/ING/RET/AGT/ADM/SEC-*`, `AT-*`).
- **Model calls:**
  - Only `ModelGateway` calls the router.
  - Calls carry `sensitivity` and `purpose` metadata so the egress policy (SEC-06, ING-06) can block a call before it leaves.
  - Calls are logged with tokens and cost (metadata only).
- **Deterministic paths never call the LLM.** This covers structured commands, authorization and the rule engine (ADR-007).
- **Each phase ends with:**
  - a runnable demo script (`make demo-pN`);
  - green tests;
  - admin screens for that phase;
  - the OpenAPI spec regenerated and the TypeScript client regenerated from it;
  - a runbook stub where relevant.
- **Inputs to each detailed plan:** the blueprint README, `05-delivery/01-implementation-slices.md`, `05-delivery/02-acceptance-and-evaluations.md`, and the domain/runtime docs listed in that phase. Each detailed plan uses the prompt template in the slices doc.

---

## Phase 0 — Compatibility Spike and Version Pinning

**Goal:** remove the riskiest unknowns before any production code exists. The output is throwaway spike code plus ADRs.

**Detailed plan:** [docs/plans/phase-0-compatibility-spike.md](docs/plans/phase-0-compatibility-spike.md). It splits the checks into OFFLINE (run now against a mock router) and LIVE-DEFERRED (run later with `make verify-router` against the local Oso router).

- **Pin versions:** Graphiti, Neo4j, Pydantic, pgvector, Python and Postgres (DEC-01).
- **Router compatibility:**
  - Does Graphiti extraction (structured output / JSON schema) work through the router?
  - What embedding dimensions does it return, and are they stable per model?
  - Does the router report the model used and token usage?
  - Can it accept routing hints, such as forcing a private or local model for restricted data?
- **Graphiti behaviour:**
  - runtime-generated Pydantic custom types (DEC-05);
  - `group_id` namespacing (DEC-06);
  - idempotency on re-submitting the same episode;
  - editing and removing edges, needed for corrections (DEC-04);
  - deleting episodes and their derivatives, needed for deletion and rebuilds;
  - mapping `valid_at` / `invalid_at` / `expired_at`.
- **Tokenizer strategy for CTX-01:** the router picks the model, so use a conservative configured tokenizer (for example tiktoken plus a safety margin) and record which one was used.
- **Auth for development:** local signed API keys or JWT now, with an OIDC path for later (DEC-03).
- **Exit:** an ADR document with the pinned versions and a pass/fail for each spike. Every fallback is chosen, for example allowlisted typed classes if dynamic Pydantic types fail.
- **Blueprint inputs:** `05-delivery/03`, `02-domain/01`, `02-domain/02`, `03-runtime/02`.

**Status (2026-10-11): offline exit reached; live finalisation pending.**
- 96 offline tests pass from a clean `make spike-up && make spike-offline`; results in [docs/compatibility-report.md](docs/compatibility-report.md), ADRs in `docs/adr/ADR-011..020`, pins in `compat/versions.lock.yaml`, router contract in [docs/router-contract.md](docs/router-contract.md).
- **P1 can start now.** Only P4 (Graphiti extraction) and P5 (memory extraction) gate on `make verify-router`.
- Surprises that changed later phases: the blueprint's example ontology collides with Graphiti's reserved `name` field (ADR-014); `search()` without `group_ids` and by-uuid fetches are not tenant-scoped (ADR-015); `add_triplet` calls the LLM, so commands use direct saves (ADR-016); Graphiti stamps `created_at` with wall-clock time (ADR-017); `remove_episode` leaves derived summaries and dangling references (ADR-018); plain HNSW recall collapses under selective filters (ADR-019); RLS needs a non-superuser role and real transactions (ADR-019).
- **Router change requests** (for the router owner, section 11 of the contract): an `oso/structured` alias; non-admin `kg-api`/`kg-worker` keys with a higher `maxClassification`; optionally a second 384-dim embedding deployment.

## Phase 1 — Foundation: Runtime, Contracts, Auth (Slice 0)

**Detailed plan:** [docs/plans/phase-1-foundation.md](docs/plans/phase-1-foundation.md). It covers 12 work packages, applies 18 Phase 0 lessons (L1–L18), and runs entirely against the mock router.

- **Monorepo layout** as in `03-runtime/01`:
  - `apps/api`, `apps/worker`, `apps/admin`;
  - `packages/{domain, ontology, ingestion, graph, memory, context, governance, observability}`;
  - plus `sdk/python` and `examples/`.
- **Docker Compose:** api, worker, postgres+pgvector, neo4j and the admin dev server, with health checks. Also `.env.example` (router URL and key) and Makefile targets.
- **Migrations and models:** Alembic migrations for workspaces, principals (users, apps and agents), roles, scopes, API keys and the audit log.
- **Request handling:** workspace-resolver middleware, `request_id` / `correlation_id`, structured error envelope, versioned `/v1` routes and generated OpenAPI.
- **Adapters:** `ModelGateway` (router) and the Neo4j/Graphiti connection adapter, with health checks only for now.
- **Telemetry:** structured logs and an OpenTelemetry/metrics skeleton with no raw content.
- **Seed:** a `demo` workspace and an `other_demo` workspace, each with agents.
- **Admin:**
  - Vite + React + TypeScript shell with the OpenAPI-generated client and dev login;
  - **Dashboard** (health) and **Agents & Access** (read-only list).
- **Demo:** after `make up && make seed`, an agent sees its own workspace and an agent from `other_demo` is denied.
- **Gate:** repeatable clean start; zero cross-workspace reads (start of SEC-01).

**Status (2026-10-11): implemented; exit criteria met except `git init` (waiting for the user's approval).**
- `make reset up seed demo-p1` passes twice with identical output (8/8 assertions). `make check` is green: ruff, import-linter (11 contracts), mypy `--strict`, 158 unit/offline tests, 35 integration, 35 security, 56 contract, 12 Vitest; `make test-e2e` (Playwright, 2 tests) passes against the running stack.
- Details: [docs/architecture/p1-foundation.md](docs/architecture/p1-foundation.md), [docs/runbooks/startup.md](docs/runbooks/startup.md). Ports used here: API 8000, Postgres 5544, Neo4j 7475/7688, mock router 8782, admin **5273** (5173/5174 are taken locally).
- Surprises worth knowing in P2+: `FORCE ROW LEVEL SECURITY` hides rows from a `SECURITY DEFINER` function's owner, so the migration owner has `BYPASSRLS` (migrations only); FastAPI 0.143 wraps included routers lazily, so `app.routes` no longer lists API routes (`kg_api.routers.ALL_ROUTERS` is the registry; a new router must be added there); never keep per-request state in `request.state` (the principal lives in the ASGI scope); judge expiry and heartbeat age with the database clock, not the host's.

## Phase 2 — Ontology Registry and Deterministic Entity Commands (Slice 1)

**Detailed plan:** [docs/plans/phase-2-ontology-and-commands.md](docs/plans/phase-2-ontology-and-commands.md). It covers 10 work packages and applies 16 lessons from P0 and P1 (M1–M16). Postgres is authoritative, and the graph is updated by a state-based projection with zero LLM calls.

- **Ontology tables:**
  - ontology drafts and immutable published versions with content hash;
  - a YAML loader matching `06-reference/02` section A.
- **Validation:** a validator and a fixed **declarative rule catalog** covering structural, validation, cardinality, temporal and authority rules. No user-supplied code.
- **Graphiti compilation:** an ontology compiler that turns the ontology into Graphiti entity and edge types, using the approach chosen in P0.
- **Typed commands:** `POST /v1/entities` and relationship commands, validated against the published ontology and projected deterministically into Neo4j. Every write is recorded in a command ledger.
- **Read and versioning:** `GET /v1/entities/{id}`; rollback re-points to an older version without rewriting history.
- **Admin:** **Ontology Types** and **Relationships & Rules** (draft, validate, diff, publish) and a basic **Entity Directory**.
- **Demo:** the admin publishes Person → WORKS_AT → Organization; a trusted app creates Sarah and Acme (`evt_001` / `evt_002` as commands).
- **Gate:**
  - ONT-01, ONT-02, ONT-04 and ONT-06;
  - a disallowed edge is rejected or queued;
  - an assertion confirms these writes made no LLM call.

**Status (2026-10-11): implemented; exit criteria met.**
- `make reset up seed demo-p2` passes all 13 steps from a clean start, and `make demo-p2` passes again without a reset. `make demo-p1` still passes (8/8). `make check` is green: ruff, import-linter (13 contracts), mypy `--strict`, 362 unit/offline tests (143 of them the pure ontology package), 117 integration, 39 security, 57 contract, 56 Vitest; `make test-e2e` (Playwright: P1 smoke, **ADM-01**, entity console) passes.
- Details: [docs/architecture/p2-ontology.md](docs/architecture/p2-ontology.md) (pipeline, projection, deviations, open decisions), ADR-021 (command ledger, state-based projection, WP0 findings), ADR-022 (spec and rule catalog), ADR-023 (succession vs supersession, record time), [docs/runbooks/projection-repair.md](docs/runbooks/projection-repair.md).
- Surprises worth knowing in P3+:
  - **Graphiti cannot save a node or edge without an embedding** (`None` makes the vector-property procedure throw), so `local_only` data is Postgres-only (`skipped_by_policy`).
  - Graphiti MERGEs on uuid alone, so every ScopedGraph write first proves the uuid is free or belongs to the group.
  - **`LOG_LEVEL=DEBUG` used to print Cypher parameters** (the Neo4j driver logs them): content-bearing library loggers are now pinned to WARNING.
  - Commands that are rejected or queued are recorded under their `Idempotency-Key`, so a corrected retry needs a **new** key (the console generates one per click).
  - The integration tests share Neo4j with `make up` (the demo workspace has the same deterministic id in both), so a test must not assume the demo group is empty.
  - P3 hooks: the projector is a `JobHandler` (`stage="graph_projection"`) and the worker's sweeper is a temporary driver behind `PROJECTION_SWEEPER_ENABLED`; migration `0002` has `down_revision="0001"`, so a parallel P3 migration needs an Alembic merge revision.

## Phase 3 — Interaction Ledger and Outbox (Slice 2) — runs alongside P2

**Detailed plan:** [docs/plans/phase-3-ledger-and-outbox.md](docs/plans/phase-3-ledger-and-outbox.md). It covers 10 work packages and applies 14 lessons from P0–P2 (N1–N14):
- an encrypted interaction ledger;
- a fenced, leased Postgres outbox (`kg_jobs`);
- P2's projection moved off the sweeper and onto the outbox;
- a failure-injection gate (FI-01..08).

- **Ingestion endpoint:** `POST /v1/interactions` with the full event envelope from `02-domain/03` and an `Idempotency-Key`. There is a unique key plus a checksum, and reusing a key with a different body is an error.
- **Storage and status:** encrypted raw payload or object pointer; the event row and the outbox row are written in one transaction; `GET /v1/interactions/{id}` returns status.
- **Worker:** an outbox dispatcher with leases and heartbeat, exponential backoff, a dead-letter queue, per-stage state, and an audited replay command.
- **Admin:** **Interactions & Episodes** (ledger list and detail, stage timeline, retry) and a basic **Operations** page (backlog and failures).
- **Demo:** ingest the `evt_003` conversation twice with the same key and get one logical event. Kill the worker mid-job and the event still completes.
- **Gate:** EVD-01, EVD-02 and EVD-05; ING-01 and ING-05; AT-01; no lost events under failure injection.

**Status (2026-10-11): implemented; exit criteria met except `git init` (still waiting for the user's approval).**
- `make reset up seed demo-p1 demo-p2 demo-p3` passes from a clean start (8/8, 13/13, 10/10) and each demo passes again without a reset. `make check` is green: ruff, import-linter (15 contracts), mypy `--strict` (133 files), 985 unit/offline tests, 190 integration, 45 security, 57 contract, 78 Vitest. `make test-resilience` passes **FI-01..FI-08**; `make test-e2e` (Playwright, 8 tests, including **ADM-06** and **EVD-06**) passes.
- Built: the encrypted, idempotent interaction ledger (`POST /v1/interactions`, `kg_ingestion`), the fenced Postgres outbox (`kg_jobs`, migration `0003`, `ops.*` functions), the `normalize` stage with the pipeline skeleton (`graph_ingest`/`memory_propose` declared and disabled), P2's projection moved onto the outbox (sweeper off), audited replay/retry/cancel (API, CLI, console), reconcile, payload key rotation, and the **Interactions & Episodes** and **Operations** console pages.
- Details: [docs/architecture/p3-ledger-outbox.md](docs/architecture/p3-ledger-outbox.md) (acceptance, outbox sequence, failure matrix, deviations), ADR-024 (ledger, idempotency, encryption), ADR-025 (outbox, fencing, effectively-once), runbooks [stuck-job-replay](docs/runbooks/stuck-job-replay.md) and [payload-key-rotation](docs/runbooks/payload-key-rotation.md).
- Surprises worth knowing in P4+:
  - **Tests share `kg_test`**, so a test that leaves outbox jobs behind lets a later test's worker claim them (and spend its injected faults); a conftest fixture now cancels leftovers.
  - **Graphiti's MERGE on `uuid` is not atomic without a uniqueness constraint.** The outbox job and the request's own fast path now project the same row at once, which created duplicate nodes on the first clean-start `demo-p2` run (the P2 sweeper's leases had hidden it). `GraphService.ensure_indices` now drops Graphiti's plain range index on `Entity.uuid` / `RELATES_TO.uuid` and creates uniqueness constraints; a rare constraint violation under contention is retryable (`graph_conflict`). A test hammers one row with four writers.
  - **A 429 from the router can be `rate_limited` or `budget_exceeded`**: the projector reads the router's error code (the OpenAI SDK's `.code`), not just the HTTP status.
  - **The worker must not wait for Neo4j to start dispatching**: normalisation does not need the graph, and projection jobs simply back off.
  - **The request hash must not depend on the payload encryption key**, or key rotation would turn every idempotent replay into a 409 (hence `CONTENT_HMAC_SECRET`).
  - `docker compose` bind mounts work for `.local/faults` (development-only failure injection through a spec file the worker re-reads).
  - `GET /v1/audit` gained an `action` filter (the demos used to scan the newest 100 entries, which a busy demo pushed past).
  - P4 hooks: `concurrency_key` exists and is tested (set `graph:{workspace}` for Graphiti episodes); the stage and job tables already have `graph_ingest`/`memory_propose`; delete the P2 sweeper first; widen the `kg_ingestion` import contract explicitly.

## Phase 4 — Graphiti Processing, Identity and Temporal Facts (Slice 3)

**Detailed plan:** [docs/plans/phase-4-graph-identity-time.md](docs/plans/phase-4-graph-identity-time.md). It covers 11 work packages and applies 14 lessons from P0–P3 (Q1–Q14):
- a `graph_ingest` stage and reconcile-then-restore (model output never overrides higher-authority truth);
- identity review and merge;
- the Graph Explorer and Review Queue v1;
- an extraction evaluation harness.

It has two exits: offline (mock scenario fixtures) and **live** (`make verify-router` + `make eval-extraction-live`, a user action required before P6).

- **Graphiti stage:** a worker stage that adds the episode with the authorized `group_id` mapping and the pinned ontology version, then links episode references back to the ledger.
- **Fact policy in Postgres:** a `proposed → active → superseded/disputed/expired/deleted` state machine and a valid-time vs record-time mapping layer.
- **Identity resolution:** candidate generation and a review queue for ambiguous matches. Merges keep aliases and lineage.
- **Conflict policy:** historical succession vs disputed facts. An inferred fact never displaces a verified one (ING-04).
- **Queries:** `as_of` and `known_at` graph queries, plus evidence lookups ("why does the system believe X?").
- **Admin:**
  - **Graph Explorer**: three-pane, with capped hops and nodes, temporal view and source links;
  - fact detail showing provenance;
  - **Review Queue** v1 (identity matches).
- **Demo:**
  - Sarah's role change from `evt_001` to `evt_002` is backdated and keeps both times;
  - the `evt_003` episode yields CONSIDERING as a tentative fact;
  - two different Sarahs are not merged.
- **Gate:** KGR-01 to KGR-06; AT-02 to AT-05; ING-02 (Neo4j outage leads to retry or a partial status).

**Status (2026-10-11): offline exit reached; live exit pending (user action).**
- `make reset up seed demo-p1 demo-p2 demo-p3 demo-p4` passes from a clean start (8/8, 13/13, 10/10, 13/13) and again without a reset. `make check` is green: ruff, import-linter (16 contracts), mypy `--strict` (149 files), 1038 unit/offline tests, 231 integration, 52 security, 57 contract, 103 Vitest. `make test-resilience` 10/10 (FI-01..10) and `make test-e2e` 13/13 (ADM-02 added). `make eval-extraction` scores 100% on the 7 scenario gold sets (strict mock router).
- Built: the `graph_ingest` stage (`kg_extraction`), reconcile-then-restore, the pure conflict/authority policy (`kg_ontology.conflicts`), identity resolution with forked candidates, reviews and merge as ledger commands, evidence/neighbourhood/search/summary APIs, `kg reviews|extraction|graph` CLI, the Graph Explorer (Cytoscape.js 3.33.1, pinned) and Review Queue v1, migration `0004`, the strict scenario mock router and the extraction evaluation harness. The P2 sweeper is deleted.
- Details: [docs/architecture/p4-graph-identity.md](docs/architecture/p4-graph-identity.md) (handler sequence, conflict table, lock model, deviations), ADR-026 (Graphiti behaviour EX-01..07), ADR-027 (reconcile-then-restore), ADR-028 (identity), runbooks [extraction-quarantine](docs/runbooks/extraction-quarantine.md) and [identity-correction](docs/runbooks/identity-correction.md).
- **Live exit (required before P6, user action):** `make verify-router`, then `OSO_ROUTER_API_KEY=... make eval-extraction-live`. It writes `eval/reports/extraction-live-<date>.*`; use it to confirm the proposed thresholds and set `IDENTITY_COS_MIN` (ADR-013).
- Surprises worth knowing in P5+:
  - **Graphiti dedupes nodes by exact name**, so unvouched mentions are forked to candidate entities and reviewed; and it can invalidate a fact on a node the interaction does not mention, so the reverted rows are recorded in the stage result and added to the restore scope.
  - **Graphiti's own retry waits minutes on bad JSON**: the platform client makes 2 attempts and the stage has a hard timeout.
  - **A Neo4j outage must be raised inside the error-mapping block** or it shows as `unexpected_error`; job payloads accept scalars only (no id lists).
  - **`CommandSummary`'s command-type literal must list every ledger command type** (review decisions and merges are commands); a test now lists them.
  - Entity type ids are positional (compile sorts types alphabetically); scenario fixtures key on the new fact text; Neo4j can be OOM-killed on a small Docker Desktop.
  - The Playwright specs share one workspace and the worker's fault file, so they run with one worker; `make test-e2e` runs demo-p2 and demo-p4 first.
  - `kg db downgrade` past `0004` needs `--force-drop-extracted` (or `kg extraction purge`).

## Phase 5 — First-Class Curated Memory (Slice 4) — can start after P3

- **Schema:**
  - the `curated_memories` table with normalized scope ACLs, provenance join tables, revisions/etag and status/TTL;
  - a pgvector column sized to the router embedding model pinned in P0.
- **Memory types:** working, episodic, semantic and procedural, each with its own scope rules.
- **Creation:**
  - explicit save;
  - a worker stage that extracts memories through the router, deduplicated by candidate hash;
  - `POST /v1/memories/proposals`.
- **Search:** `POST /v1/memories/search` using **exact scoped search** first (lexical + vector, prefiltered); ANN indexing comes later after evaluation.
- **Corrections:** `supersede`; `GET /v1/memories/{id}` returns content, history and evidence.
- **Admin:** **Memory Explorer** (filters, evidence, revisions, approve/expire/supersede).
- **Demo:**
  - `evt_003` produces the episodic memory `mem_003` with its source;
  - `evt_004` produces a separate explicit preference.
- **Gate:** MEM-01, MEM-02, MEM-04 and MEM-05; scope isolation tests that include the vector candidate set.

## Phase 6 — Task-Aware Context Engine (Slice 5) — **MVP Milestone**

- **Context build pipeline:** `POST /v1/context/build` runs:
  1. plan and select a profile;
  2. authorize;
  3. retrieve graph and memory in parallel (prefiltered);
  4. merge and deduplicate;
  5. reconcile time;
  6. rank deterministically with recorded weights;
  7. pack in **structured** mode (extractive mode optional);
  8. enforce a hard token budget, returning `insufficient_budget` if required items don't fit;
  9. attach citations, unknowns and conflicts;
  10. record the trace.
- **Profiles:** versioned context profiles (`quick_answer`, `planning`, `research`, `review`, `personal_assistant`). Agent overrides can only narrow them.
- **Degradation:** `partial` / `allow_partial` handling with source watermarks. Traces are stored as metadata only; `GET /v1/context/{id}/trace`.
- **Evaluation harness v1:** the frozen Sarah benchmark from `03-runtime/03`, scored for recall, precision, temporal correctness, attribution, leakage and budget.
- **Admin:** **Context Lab**, which uses the *same* authorization and composer path as the public API, plus the trace inspector.
- **Demo:** the planning task for `person_sarah` with a 600-token budget returns:
  - Acme / Engineering Manager as current facts;
  - the `mem_003` memory and the preference;
  - "runway unknown";
  - the citations.

  The cycling distractor is excluded.
- **Gate:** CTX-01 to CTX-08; RET-01 to RET-06; AT-06 to AT-09 and AT-12.

## Phase 7 — Shared Multi-Agent Memory, SDK and MCP (Slice 6) — runs alongside P8

- **Agent registration:** agents get scopes, quotas, allowed profiles and a model-disclosure policy. Run and session IDs are kept separate from agent identity.
- **Publishing:** propose → validate (evidence exists and is authorized) → policy auto-approve or review → publish. Findings carry `agent_generated` / `verified` labels, and `evidence_required` is supported.
- **Revocation:** immediate, and it also invalidates context and evidence links.
- **Python SDK** (`sdk/python`), typed from OpenAPI, with `build_context`, `ingest_interaction`, `propose_memory`, `search_memory` and `get_evidence`.
- **MCP server:** a thin transport over the same authorized API, with no privileged path.
- **Admin:** **Agents & Access** gains full functions (register, revoke, scopes); the Review Queue gains memory proposals.
- **Demo:**
  - the Research Agent proposes `evt_005` and it is approved;
  - the Planning Agent retrieves it;
  - the Research Agent's private working notes stay hidden;
  - a revoked agent is blocked.
- **Gate:** AGT-01 to AGT-05; MEM-03; AT-10, AT-11 and AT-14; SEC-03.

## Phase 8 — Governance: Corrections, Consolidation, Deletion (Slice 7)

- **Corrections:** correction commands for facts and entities, merge and split, and relationship expiry. Corrections propagate to the Neo4j projection, linked memories, embeddings and cached context, using the DEC-04 approach chosen in P0.
- **Memory consolidation worker:** idempotent, policy-gated, and preserves distinct claims and sources.
- **Deletion orchestration:** `POST /v1/deletion-requests` blocks reads immediately, then:
  - enumerates derivatives across PG, pgvector, Graphiti, traces and objects;
  - purges or redacts them;
  - verifies the result;
  - issues a receipt.
- **Ontology changes:** simulation and migration preview (ONT-05); a version-2 publish pins new jobs while old provenance stays on v1.
- **Prompt-injection containment** tests (SEC-04, AT-15).
- **Admin:**
  - Review Queue v2 (contradictions, merge/split, deletion exceptions);
  - correction flows from the Graph and Memory Explorers;
  - **Settings** (retention, quotas, model and egress policy);
  - privacy audit view.
- **Demo:**
  - a reviewer rejects the wrong extracted fact "Michael manages Sarah" and the next context build reflects it;
  - a subject deletion removes the subject from context immediately and the purge completes.
- **Gate:** MEM-06, MEM-07; ADM-03; AT-13, AT-15, AT-16 and AT-17; SEC-04, SEC-05 and SEC-06.

## Phase 9 — Evaluation, Recoverability and Release Hardening (Slice 8)

- **Evaluation suite:** the full regression and evaluation suite, with separate labelled sets for ranking, extraction, conflicts, authorization and injection. Model or router changes are tested against the frozen data set.
- **Failure injection:** Neo4j outage, router/LLM outage, worker crash, and replay. Also the reconciliation command that compares the ledger against the graph and memory store.
- **Backup and restore:** a rehearsal restoring into a separate environment (AT-18).
- **Baselines:** cost, latency (p50/p95) and freshness, which settle DEC-11 with measured numbers.
- **Security:** threat review and the full SEC-01 to SEC-06 sweep.
- **Release readiness:** all runbooks from `03-runtime/05` and a release checklist.
- **Admin:** full **Operations** (cost, latency, error queues, replay with audit reason).
- **Gate:** every strict release blocker passes (AT-01, AT-08, AT-10, AT-11, AT-15, and the AT-16 read block). Open production decisions are documented.

## Phase 10 — Fully Working Examples

All examples run against `docker compose up` plus one seed command and are covered by CI smoke tests.

1. **Sarah two-agent scenario:** `make demo-sarah` runs all 10 reference steps from `05-delivery/02`, including the 2027 job change, the correction, the deletion, and a model-outage variant. It produces a pass/fail report and prints the context packages.
2. **Second domain template:** for example a *Project / Customer CRM* ontology with its own seed events, context profiles and benchmark. It proves P-01 reusability with **no platform code changes**, only config. It also documents "how to add a domain".
3. **Real agent integrations:**
   - a Research agent and a Planning agent built with the Claude Agent SDK (or any OpenAI-compatible agent framework through the router), using the Python SDK;
   - the same agents wired through the MCP server;
   - an "unauthorized agent" example that shows the denials.
4. **Jupyter notebooks:**
   - `01_ingest_and_ledger`;
   - `02_temporal_graph_as_of`;
   - `03_curated_memory`;
   - `04_context_build_and_budget`;
   - `05_multi_agent_sharing`;
   - `06_corrections_and_deletion`;
   - `07_second_domain`.

   Each notebook runs from top to bottom against the local stack.

- **Docs:** a quickstart README, an architecture overview linked to the blueprint, and an "extend this boilerplate" guide.
- **Gate:** a fresh clone, `make up seed examples` and `make test-examples` all pass.

---

## How We'll Use This Roadmap

1. After approval, save this roadmap to the repository as `/Users/blairboylan/Documents/kg_boilerplate/ROADMAP.md`.
2. For each phase in order, write a **detailed phase plan** before handing it to a coding agent. Each plan covers:
   - file-level work items, schemas and migrations, OpenAPI contracts;
   - authorization points, worker states, UI routes;
   - fixtures, negative tests, demo script and rollback.
3. Unresolved decisions stay explicit in each phase plan and are not assumed.

## Verification of the Roadmap Itself

- Every blueprint slice (S0–S8) and every `AT-01` to `AT-18` maps to exactly one gating phase. AT-01 is in P3; AT-02 to AT-05 in P4; AT-06 to AT-09 and AT-12 in P6; AT-10, AT-11 and AT-14 in P7; AT-13 and AT-15 to AT-17 in P8; AT-18 in P9.
- Every phase ends with a runnable demo and a measurable gate.
- **Open items to confirm in P0** (router-specific). Answers come from the router source and the mock; each is marked pending until `make verify-router` has confirmed it live:
  - **Structured outputs / JSON schema:** supported, but only by models with the `structured_output` capability (glm-5.2, gemini-3.8-flash, gemini-3.5-flash, gemini-3.1-pro-preview, gemini-2.5-pro). `oso/fast` has none and fails `unsupported_capability`; `oso/general` has exactly one, so extraction has no fallback there. Use `oso/general` with `oso/code` as fallback until the router adds `oso/structured`. *Pending: LV-04, GR-12.*
  - **Embedding model stable / pinnable:** `oso/embedding` is a single deployment of `intfloat/multilingual-e5-small`, 384 dimensions, 512-token model limit, no fallback. Pinned as `vector(384)` with version columns so a change is a managed re-embed (ADR-013). *Pending: LV-05.*
  - **Routing hints for sensitive data:** supported via `routing.data_classification` / `privacy`; `local_only` is rejected because no local deployment exists, which the platform treats as `skipped_by_policy` and never sends (ADR-012). The seeded key is capped at `internal`, so platform keys need a higher cap. *Pending: LV-06.*
  - **Usage and cost in responses:** usage (prompt/completion/total tokens) yes; cost no. The model is reported as the alias only, and provider/model headers are admin-only. The platform estimates cost from a local price table and records the router request id (ADR-012). *Pending: LV-02, LV-03.*
