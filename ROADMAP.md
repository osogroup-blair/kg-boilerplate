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

**Detailed plan:** [docs/plans/phase-5-curated-memory.md](docs/plans/phase-5-curated-memory.md). It covers 11 work packages and applies 14 lessons from P0–P4 (R1–R14):
- scope-enforced visibility, pulling `principal_scopes` enforcement forward from P7;
- a typed memory policy in the ontology;
- the `memory_propose` stage after `graph_ingest`;
- exact scoped hybrid search in one SQL statement;
- a sentinel leak suite;
- the Memory Explorer.

It has two exits: offline (mock) and **live** (`make eval-memory-live`, run together with the P4 live exit before P6).

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

**Status (2026-10-12): offline exit reached; live exit pending (user action).**
- `make reset up seed demo-p1 demo-p2 demo-p3 demo-p4 demo-p5` passes, and again without a reset (demo-p5 12/12, demo-p4 13/13). `make check` is green: ruff, import-linter (17 contracts), mypy `--strict` (169 files), 1178 unit/offline tests, 294 integration, 62 security, 57 contract, 125 Vitest. `make test-resilience` 12/12 (FI-01..12) and `make test-e2e` 19/19 (Memory Explorer added). `make eval-memory` scores 100% on the 9 scenario fixtures (strict mock router). Exact scoped search over 50,000 memories in 20 workspaces: p95 ≈ 18 ms.
- Built: `kg_memory` (typed policy, scope grants and visibility predicate, four creation paths, lifecycle, review decisions, hybrid scoped search `mem-rrf.v1`, re-embed), the `memory_propose` stage (pipeline `p5.v1`), migration `0005`, `/v1/memories` and `/v1/scopes`, `kg memories|scopes` CLI, the Memory Explorer, memory review cards and scope grants in the console, the leak/isolation suites and `eval/memory`.
- Details: [docs/architecture/p5-memory.md](docs/architecture/p5-memory.md) (sequences, data, **14 deviations from the plan**), ADR-029 (model, scopes, visibility), ADR-030 (extraction, creation policy, prompt safety), ADR-031 (hybrid scoped search), runbooks [memory-reembed](docs/runbooks/memory-reembed.md) and [memory-quarantine](docs/runbooks/memory-quarantine.md).
- **Live exit (required before P6, user action):** `make verify-router`, then `OSO_ROUTER_API_KEY=... make eval-memory-live` (together with the P4 live exit). Use it to confirm the proposed thresholds and to set `MEMORY_VEC_MIN_COSINE` (currently a mock-sized 0.2).
- Surprises worth knowing in P6+:
  - **Visibility is a predicate inside the ranking statement**, never a post-filter; `candidates_considered` counts authorised rows only. P6 must reuse `MemorySearch`, not query `curated_memories` itself.
  - **Memory commands are hidden from `/v1/commands`** (their visibility depends on scopes), so any audit or timeline view must go through `kg_memory.reads`.
  - **Workspace-scoped memory is visible to every principal that may read memory**; "granted nothing" means workspace-wide memory only.
  - **Suites and demos written before P5 run with `extraction: disabled`** (`NO_MEMORY_YAML`, `kg seed demo --no-memory-extraction`); P5 suites use the v1.2.0 example. A stage that dies behind a dead dependency leaves the event `partial`.
  - `kg seed demo --ontology X` now re-activates X when it is already published but another version is active.
  - `kg db downgrade` past `0005` needs `--force-drop-memories`.

## Phase 6 — Task-Aware Context Engine (Slice 5) — **MVP Milestone**

**Detailed plan:** [docs/plans/phase-6-context-engine.md](docs/plans/phase-6-context-engine.md). It covers 12 work packages and applies 12 lessons from P0–P5 (S1–S12):
- critical current facts come from Postgres; graph and memory searches are reused, never bypassed;
- deterministic ranking (`ctx-rank.v1`) and tiered packing under a hard budget, enforced on the rendered output;
- deterministic unknowns from `expected_signals` in the profile;
- metadata-only traces with re-materialization;
- the Context Lab through an audited act-as;
- a retrieval evaluation harness.

**It can be built offline. MVP sign-off needs the single live session** (P4, P5 and P6 live evaluations plus TK-02).

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

**Status (2026-10-12): offline exit reached; MVP sign-off needs the single live session (user action).**
- Verified: `make reset up seed demo-p1 … demo-p6` passes and demo-p6 re-runs (11/11, twice); `make check` is green (ruff, import-linter 19 contracts, mypy `--strict` 194 files, 1255+ unit/offline, 347 integration, 70 security, 58 contract, 138 Vitest); `make test-resilience` 12/12; `make test-e2e` 25/25 (Context Lab added); `make eval-context` PASS on the 14 frozen cases (leakage 0, budget 100%, attribution 100%, recall 1.0 in mock). Baseline in `eval/reports/context-perf.json`. The reset run also found three stale assertions left by P5 in `demo_p1`, `demo_p3` and `demo_p5` (a seventh principal, `skipped_by_policy` instead of `skipped_disabled`, `p5.v1`), now fixed.
- Built: `kg_context` (planner, candidates, merge, temporal reconciliation, deterministic unknowns, `ctx-rank.v1`, exact-budget tiered packer, renderer with the neutraliser, validated `summarized` mode, parallel sources with per-source timeouts, traces, `ContextService`), migration `0006`, `/v1/context/*` and `/v1/memories/{id}/usage`, the Context Lab (act-as, ADR-035), the trace inspector and "Used in contexts", `kg context`, worker retention, the typed `ContextProfile` and ontology v1.3.0, the `GraphSearch` egress fix, `eval/context` (14 frozen cases) and `make demo-p6`.
- Details: [docs/architecture/p6-context.md](docs/architecture/p6-context.md) (sequences, data, **13 deviations from the plan**), ADR-032 (contract, statuses, one query embedding), ADR-033 (ranking, packing, compression), ADR-034 (traces, re-materialisation, debug snapshots), ADR-035 (Lab act-as), runbook [context-debugging](docs/runbooks/context-debugging.md), [agent-integration](docs/agent-integration.md).
- **Live exit (user action, once, P4 + P5 + P6 together):** `make verify-router` (LV-01..10, G-05, GR-11..13, **TK-02**), then `OSO_ROUTER_API_KEY=... make eval-extraction-live eval-memory-live eval-context-live`; confirm the proposed baselines, set `IDENTITY_COS_MIN` and `MEMORY_VEC_MIN_COSINE`, close TK-02 (the token margin) and re-run `make eval-context`.
- Surprises worth knowing in P7+:
  - **A context is never stored, only explained.** `GET /v1/context/{id}` re-materialises from item ids under the caller's *current* access; anything that changed is `withdrawn`. P8's deletion must purge `context_items` rows that name a deleted record (a keyed delete).
  - **Facts are workspace-readable until P7 adds subject ACLs**, so an agent with no grants gets a subject's facts and none of its memories (S7); `context.build` already takes the visible-subject set, so P7 narrows one place.
  - **The act-as header counts on `POST /v1/context/build` only** and only for admins acting as an active agent/app of the same workspace; P7's allowed-profiles per agent must be honoured by it.
  - **Command facts carry no `fact_text`**; the engine renders them with the ontology's own template (what the graph holds). An ontology edit changes how current facts read.
  - **Rate limit and the concurrency cap are in-process** (P9 moves them to shared state); a build holds up to three DB connections.
  - **Suites that need Sarah's whole world use `tests/support/p6.sarah_world`** (strict mock + the real worker, ~5 s); `eval.context.run_eval` reuses it.

## Phase 7 — Shared Multi-Agent Memory, SDK and MCP (Slice 6) — runs alongside P8

**Detailed plan:** [docs/plans/phase-7-multi-agent.md](docs/plans/phase-7-multi-agent.md). It covers 10 work packages and applies 12 lessons from P0–P6 (T1–T12):
- agent and app registration with grants and privileges;
- ontology-driven subject ACLs on entities and facts, where a hidden endpoint hides the whole fact;
- the publish model with `agent_generated`/`verified` labels;
- final revocation, with a `compromised` quarantine path;
- quotas;
- the Python SDK and the MCP server, both of which forward the caller's own key;
- the two-agent success scenario, run offline.

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

**Status (2026-10-12): offline exit reached; adds no live dependency (the P4 + P5 + P6 live session stays the user's action).**
- Verified: `make reset up seed demo-p1 … demo-p7` passes from a clean reset and demo-p7 re-runs (10/10); `make lint typecheck` clean (import-linter 22 contracts, mypy `--strict` 213 files); 1328 unit/offline, 407 integration, 87 security, 70 contract (incl. SDK and MCP parity), 150 Vitest; `make test-resilience` 12/12; `make test-e2e` 29/29 (Agents & Access added); `make eval-context` PASS on 16 cases (C15/C16 are the subject-ACL cases, leakage 0).
- Built: migration `0007` (access, privileges, quotas, evidence guard, revocation state), ontology `access: workspace|subject_scoped` with a required `SubjectFilter` on every knowledge/ingestion read, grants with `read|propose|publish`, the publish model and `agent_generated`/`verified` labels, final revocation (retired vs compromised with the `source_compromised` review), quotas, `sdk/python` (models generated from OpenAPI), `apps/mcp` (stateless Streamable HTTP, forwards the caller's key), the Agents & Access admin, `make demo-p7`.
- Details: [docs/architecture/p7-agents.md](docs/architecture/p7-agents.md) (**14 deviations from the plan**), ADR-036..040, runbooks [agent-onboarding](docs/runbooks/agent-onboarding.md) and [agent-revocation](docs/runbooks/agent-revocation.md), [agent-integration](docs/agent-integration.md).
- Nothing in P5–P7 is committed yet; the working tree holds all three phases.
- Surprises worth knowing in P8+:
  - **A hidden id answers the same 404 as a missing one**, and a fact is visible only when both endpoints are: P8's deletion and correction paths must go through the same `SubjectFilter`, never around it.
  - **Compromised-revoke quarantines only what came from submissions and published findings**, not facts an app created by command.
  - **`make mcp-up` recreates api and mock-router**: never run it during a test run.
  - **Quota counters are in-process (requests) and durable (daily tokens/interactions)**; P9 moves the request bucket to shared state.

## Phase 8 — Governance: Corrections, Consolidation, Deletion (Slice 7)

**Detailed plan:** [docs/plans/phase-8-governance.md](docs/plans/phase-8-governance.md). It covers 13 work packages and applies 14 lessons from P0–P7 (U1–U14):
- corrections and identity split as audited ledger commands, with impact preview;
- consolidation: deterministic reinforce runs automatically, LLM synthesis always goes to review;
- deletion as freeze → plan → purge → verify → receipt, with legal holds, suppression and an out-of-band journal for P9 restores;
- a trigger-enforced purge gate on immutable tables;
- ontology simulation in staging namespaces;
- workspace settings and retention;
- the SEC-04/AT-15 and SEC-06 containment suites;
- Review Queue v2, Deletions, Settings and the privacy audit view.

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

**Status (2026-10-15): offline exit reached; the live session (P4 + P5 + P6 + P8) is still the user's action.**
- Verified: `make reset up seed demo-p1 … demo-p8` passes from a clean reset (demo-p8 13/13, and it re-runs: it makes its own people and leaves the ontology as it found it); `make lint typecheck` clean (import-linter 22 contracts, mypy `--strict` 242 files); 1392 unit/offline, 604 integration + security + contract, 172 Vitest; `make test-e2e` 35/35 (the new `governance.spec.ts`: ADM-03, deletion with receipt, the legal-hold path, Settings, the privacy audit, tenant isolation, role gating); `make eval-consolidation` PASS (every strict counter 0). Not run here: the live evals (`eval-consolidation-live`, `eval-simulation-live`, plus the P4-P6 ones) and `make test-resilience`.
- Built: migration `0008` (the purge gate, redaction guards, deletion / hold / correction / lineage / settings tables), corrections (fact, attribute, rename, memory) and the identity split with impact previews and authoritative-override notices, consolidation (reinforce, and synthesis that always goes to review), deletion orchestration (freeze, plan, purge, verify, receipt; holds, suppression, tombstones, the out-of-band journal, workspace cooling-off, retention), ontology simulation in staging namespaces, versioned workspace settings with narrow-only egress, the privacy audit, the SEC-04/AT-15 corpus (45 payloads) and the SEC-06 matrix, the admin console (Review Queue v2, Deletions, Settings, Privacy audit, correction / split / simulate dialogs), `kg deletions`, `make demo-p8`.
- Details: [docs/architecture/p8-governance.md](docs/architecture/p8-governance.md) (deviations listed there), ADR-041..047, runbooks [subject-deletion](docs/runbooks/subject-deletion.md), [legal-hold](docs/runbooks/legal-hold.md), [identity-correction](docs/runbooks/identity-correction.md) (split added), [ontology-simulation](docs/runbooks/ontology-simulation.md), [retention](docs/runbooks/retention.md).
- Nothing in P5-P8 is committed yet (WP0 needs the owner's approval); the working tree holds all four phases.
- Surprises worth knowing in P9+:
  - **The restore contract is the journal:** a backup restored from before a deletion must replay `DELETION_JOURNAL_DIR` (`kg deletions journal --since <backup time>` lists the lines; `kg deletions reapply` is P9's).
  - **Egress narrowing is cached per process** (`WORKSPACE_EGRESS_TTL_SECONDS`); the dev compose uses 3 s. A shared limiter / cache is P9.
  - **The purge gate trusts a role shortcut** (owner / BYPASSRLS) except for the ontology registry, which stays immutable even for the owner (`purge_request_allows`).
  - Demos share one workspace: a mock-router rule matched by `contains: [marker]` applies to every later prompt with that marker, and real extraction in a workspace that already has nodes also needs `NodeResolutions` and `SummarizedEntities` rules.

## Phase 9 — Evaluation, Recoverability and Release Hardening (Slice 8)

**Detailed plan:** [docs/plans/phase-9-release-hardening.md](docs/plans/phase-9-release-hardening.md). It covers 13 work packages and applies 13 lessons from P0–P8 (V1–V13). Two owner actions gate it: committing P5–P8 (WP0) and the single live session (WP1, `make live-session`). It then delivers:
- frozen, hash-locked evaluation suites and `eval-gate`;
- failure matrix FI-01..23 and a unified `kg reconcile`;
- encrypted backups and a restore rehearsal with `kg deletions reapply` (AT-18);
- load baselines for owner-set SLOs (DEC-11);
- a threat review, release sweep, scanners and hardening;
- OIDC, a shared Postgres limiter and NOTIFY cache invalidation;
- Flow F reindex with atomic cutover;
- Operations v2 and alerting;
- a production reference deployment, runbooks, a decision register and the `v1.0.0-rc1` release candidate run.

- **Evaluation suite:** the full regression and evaluation suite, with separate labelled sets for ranking, extraction, conflicts, authorization and injection. Model or router changes are tested against the frozen data set.
- **Failure injection:** Neo4j outage, router/LLM outage, worker crash, and replay. Also the reconciliation command that compares the ledger against the graph and memory store.
- **Backup and restore:** a rehearsal restoring into a separate environment (AT-18).
- **Baselines:** cost, latency (p50/p95) and freshness, which settle DEC-11 with measured numbers.
- **Security:** threat review and the full SEC-01 to SEC-06 sweep.
- **Release readiness:** all runbooks from `03-runtime/05` and a release checklist.
- **Admin:** full **Operations** (cost, latency, error queues, replay with audit reason).
- **Gate:** every strict release blocker passes (AT-01, AT-08, AT-10, AT-11, AT-15, and the AT-16 read block). Open production decisions are documented.

**Status (2026-10-18): implemented; the final `make release-candidate` run, the live session and the owner's sign-offs are still to do.**
- Verified: on the previous machine (per [docs/release/handoff.md](docs/release/handoff.md)) lint, mypy `--strict`, import-linter (22 contracts), 1465 unit, 528 integration, 105 security, 70 contract, 183 Vitest, `make test-release` (every SEC/AT id on two replicas), `make prod-smoke`, `make obs-test`, and a `make release-candidate` that passed every step through `restore-rehearsal` (RTO about 95 s) before the disk filled. On the new machine: lint, mypy (261 files), 1467 unit, 183 Vitest, 135 memory and context integration tests, `make eval-memory` PASS, and a complete `make perf-baseline`. The full integration, security, contract and release sweeps are not re-run yet (the release-candidate run does that).
- Built: hash-locked evaluation suites and `eval-gate`, failure matrix FI-01..23, `kg reconcile`, encrypted backups with `kg deletions reapply` and the restore rehearsal, load baselines with SLO proposals, secret rings and rotation, scanners, the two-replica release sweep, OIDC, the shared Postgres limiter and NOTIFY invalidation, Flow F reindex with atomic cutover, Operations v2 with alerting, the production reference deployment and runbooks, and the decision register.
- Details: [docs/architecture/p9-release.md](docs/architecture/p9-release.md) (ten deviations listed there), ADR-048..053, [docs/release/checklist.md](docs/release/checklist.md), [decisions.md](docs/release/decisions.md) (DEC-11 now carries the measured numbers and proposals), [docs/runbooks/deploy-production.md](docs/runbooks/deploy-production.md) (sizing), [eval/reports/baseline-2026-10-18.md](eval/reports/baseline-2026-10-18.md).
- Surprises worth knowing in P10+:
  - **The first complete baseline exposed a search bug and four load-tool defects, none of them visible before.** (1) A search whose term matched most of a workspace (`licensing` in every seeded memory) was planned as a nested loop of the whole workspace squared: 2.5 billion comparisons at 50,000 memories, minutes per query. The `lex` and `vec` LEFT JOINs in `kg_memory/search.py` are now one FULL JOIN (`hits`), which Postgres can only hash or merge; ranking is unchanged (`make eval-memory` PASS). No synthetic regression test reproduced it, so the baseline's size curve is the only guard. (2) The load agent had no subject grant, so after P7 every context build was a fast `denied` that the report counted as a success. (3) The perf stack never received its raised rate limits, because the dev compose passes through only the variables it lists (`eval/load/compose.perf.yml` now names them). (4) Proposals now need evidence. (5) The scenarios queued behind each other's backlog; the baseline now drains between them.
  - **Search is the capacity limit, not the worker.** Exact cosine costs about 2.5 Postgres cores per search per second at 50,000 memories (p95 2.6 to 3.6 s); the ANN trigger in the baseline is already true. The worker settles about 0.25 jobs per second per stage at its defaults and is bound by router latency and the model-call semaphore, not CPU.
  - **A workspace builds at most about 5 contexts per second** (6 in flight, `CONTEXT_WORKSPACE_CONCURRENCY`); the rest get 429 by design, and per-principal limits (60 builds/min) apply to a single agent.
  - A client timeout does not cancel the Postgres query behind it, so a slow search keeps burning CPU for minutes after the caller gave up.
  - A fresh clone has no `uv`, `pip` or `python3-venv` on some hosts; `make tools` then fails and `.tools/uvenv/bin/uv` can be dropped in by hand.

## Phase 10 — Fully Working Examples

**Detailed plan:** [docs/plans/phase-10-closeout-and-examples.md](docs/plans/phase-10-closeout-and-examples.md). Part A closes out Phase 9 (baseline, sizing, SLO proposals, loose ends); Part B builds the examples below, and the single release-candidate run comes last.

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

**Status (2026-10-18): implemented and passing locally; the CI `examples` job and the final release-candidate run have not run yet.**
- Verified: `make examples` (Sarah 11/11 steps, the CRM benchmark with leakage 0, budget and attribution 100%, the agents example 5/5) and `make test-examples` (26 passed, including the seven notebooks run top to bottom) on the dev stack with the mock router; `make lint typecheck` clean (import-linter 22 contracts); the mock router suite passes (50). The scorer of the CRM benchmark was checked to fail on deliberately wrong cases.
- Built: the example kit and shared runner (`examples/_kit`), `make demo-sarah`, `make demo-crm`, `make eval-crm`, `make examples-agents`, seven notebooks generated from text sources, Claude Code and Claude Desktop MCP configs, the quickstart README, `examples/README.md`, `docs/architecture/overview.md`, `docs/guides/extend.md` and `add-a-domain.md`, the `examples` CI job and a release-candidate step. Details and decisions D1-D5: [docs/plans/phase-10-closeout-and-examples.md](docs/plans/phase-10-closeout-and-examples.md).
- Deviations: the CRM benchmark has its own scorer (the `eval/context` runner builds Sarah's world in Python); the agents talk to the router with plain `httpx`, and offline the mock router *scripts* the model (`_message` tool calls), so the offline transcripts prove the platform side, not a model; notebook 1 does not show ciphertext (the examples never read the database); legal hold and identity split are linked, not shown.
- Surprises worth knowing next:
  - **`known_at` is record time set by the database and ending a fact edits it in place**, so `known_at` cannot "un-know" an end date; the examples show `known_at` on a fact that was recorded later. `source_recorded_at` is informational.
  - **A missing mock fixture does not fail an example, it makes a job retry until a wait times out** (details in the handoff gotchas).
  - **The first `settle` helper used a wrong path and silently did nothing**; the examples hid two retrying jobs until it was fixed. Prefer waits that name what they wait for.
  - **CI `check` is red on `main`** with four integration failures that have not been investigated (see the handoff).


---

## How We'll Use This Roadmap

1. After approval, keep this roadmap in the repository as `ROADMAP.md` (done).
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
