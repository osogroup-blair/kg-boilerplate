# Phase 3 architecture: interaction ledger and transactional outbox

Plan: [phase-3-ledger-and-outbox.md](../plans/phase-3-ledger-and-outbox.md). Decisions: [ADR-024](../adr/ADR-024-interaction-ledger-idempotency-and-encryption.md) (ledger, idempotency, encryption), [ADR-025](../adr/ADR-025-outbox-and-effectively-once.md) (outbox, fencing, concurrency keys). Runbooks: [stuck-job-replay](../runbooks/stuck-job-replay.md), [payload-key-rotation](../runbooks/payload-key-rotation.md), [projection-repair](../runbooks/projection-repair.md).

## Packages

| Package | Role | May import |
|---|---|---|
| `kg_jobs` | The generic outbox: `enqueue` (in the caller's transaction), `OpsClient` (the `ops.*` SQL functions), `RetryPolicy` and the category→outcome mapping, `Dispatcher`, test-only `faults`, tenant-scoped job reads | `kg_common`, `kg_db`, `kg_obs` |
| `kg_ingestion` | Envelope validation, `PayloadCipher` / `ContentHasher`, `AcceptService`, `PIPELINE_V1` + `Orchestrator`, `NormalizeHandler`, reads, `OperationsService` (replay, retry, cancel), `Reconciler`, key re-encryption | `kg_jobs`, `kg_db`, `kg_domain`, `kg_common`, `kg_obs`, `kg_governance` |
| `kg_knowledge` (changed) | `CommandPipeline` enqueues a `graph_projection` job in the command's transaction; `GraphProjectionHandler` replaces the sweeper's driver role | + `kg_jobs` |
| `kg_worker` (changed) | Wires `Dispatcher` + the handler registry; the sweeper is off | |

import-linter keeps `kg_jobs` free of domain code, and keeps `kg_ingestion` free of `kg_graph`, `kg_modelgw`, `kg_knowledge` and `graphiti_core` (P4 widens that contract explicitly).

## Acceptance (one transaction)

```mermaid
sequenceDiagram
  participant C as Client
  participant API as POST /v1/interactions
  participant DB as Postgres (kg_app, RLS)
  C->>API: event + Idempotency-Key
  API->>API: validate envelope (field paths only), size, payload_ref, keyed request hash
  API->>DB: BEGIN; advisory lock (workspace, source_system, key)
  DB-->>API: existing row? same hash -> 200 replay (current status); different -> 409
  API->>DB: occurred_at skew vs DB clock; resolve subjects; pin active ontology
  API->>DB: INSERT interaction + ENCRYPTED raw content + subjects + stage plan + normalize outbox job + audit
  API-->>C: 202 {event_id, status:accepted, status_url} after COMMIT
```

- **Only accepted events consume a key.** A 4xx leaves no row, so the key stays free (ADR-024). This differs from commands, which store refusals.
- **Raw content is AES-256-GCM encrypted** with the workspace/interaction/form bound as associated data. The envelope row keeps only metadata (kind, classification, sizes, pinned versions, the keyed request hash); `interactions` is immutable except for its processing status (column grants **and** a trigger that also binds the table owner).
- **Subjects:** `{entity_id}` must exist in the workspace (`422 unknown_entity`, identical for a foreign and a random id); `{type, system, value}` resolves through `entity_external_ids` or stays *unresolved* for P4, stored as a keyed hash (the value lives only inside the encrypted raw content).
- **Classification** is `max(requested, workspace default)`; the default is `internal` until workspaces have settings.

## Pipeline v1

```
normalize ──► graph_ingest (P4, disabled) ──┐
          └─► memory_propose (P5, disabled) ─┴─► finalize (inline, never a job)
```

Stage states: `pending → running → succeeded | skipped_by_policy | skipped_disabled | invalid | failed (retrying) | dead`. Interaction status: `accepted → processing → indexed | partial | failed` (`deletion_pending` is reserved for P8). `Pipeline.finalize` is a pure function and has a test for all 512 combinations of the three stage states. Disabled stages are *listed* as `skipped_disabled`, so a source-only event is a valid, `indexed` event (EVD-04); an enabled model stage of an event whose consent denies external processing is planned as `skipped_by_policy` (ING-06, asserted through the stage plan).

`normalize` canonicalises per kind (message/observation → text; conversation → `[at] speaker: text` lines; json → canonical JSON; document → whitespace-normalised text, an unsupported `content_type` is `invalid`), stores the normalised form encrypted, and records counts only. **No model is called** (asserted with the router spy). Content is data: nothing in it can choose a stage, classification, policy or role (test with an injection payload).

## Outbox

```mermaid
sequenceDiagram
  participant W as Dispatcher (worker)
  participant DB as ops.* (SECURITY DEFINER, kg_owner)
  participant T as tenant tx (kg_app, RLS)
  W->>DB: claim_jobs(worker, stages, n, lease)   -- SKIP LOCKED, concurrency-key advisory lock, attempts+1, new lease_token
  DB-->>W: routing metadata only (ids, stage, attempt, token, pinned versions)
  W->>W: fault hooks (tests); handler.handle(job)
  W->>T: idempotent side effect; ONE transaction: write stage result + Orchestrator.advance
  loop every lease/3
    W->>DB: heartbeat_jobs(worker, tokens)  -> tokens LOST (handler cancelled cooperatively)
  end
  W->>DB: complete_job(job, token, outcome, ...)  -- FENCED: a stale token changes nothing
```

- **Guarantee: at-least-once delivery + idempotent handlers = effectively-once results.** A crash after the claim, after the side effect, or after the result transaction just lets the lease expire; the re-run finds the stage terminal at its generation and completes. Replay raises the stage `generation`, which is part of the dedupe key, so replays never collide with history.
- **Retry policy per stage** (`normalize` 5 attempts, `graph_projection` 8, full jitter, 2 s base): retryable → `retry` with backoff (a Retry-After wins), policy-terminal → `skipped`, config-terminal → `dead`, budget → `budget_pause` (15 min, attempt **not** consumed), exhausted → `dead`, unexpected exception → retryable with its message dropped.
- **Dead letters** keep the last error category and code; `ops.complete_job` closes a `job_attempts` row either way. A job claimed again after burning every attempt on crashes is dead-lettered without running the handler a further time (a poison event cannot crash workers forever).
- **Time is the database's.** Leases, backoff and expiry use `now()` inside SQL; the dispatcher passes durations.
- **Graceful shutdown** stops claiming, waits up to `WORKER_SHUTDOWN_GRACE_SECONDS`, cancels the rest and calls `release_jobs` (the attempt is returned: a shutdown is not a failure).

## Projection on the outbox

`CommandPipeline` enqueues `command:{id}:graph_projection:g0` in the command's transaction for every applied, non-matching command. The 5 s in-request fast path remains (201 when projected in time); the job is then a no-op because the projector is state-based. Error codes map to categories (`graph_unavailable`/`router_unavailable` retry; `budget_exceeded` pauses; `egress_denied`/`router_policy` → `skipped_by_policy`; `router_rejected`/`graph_rejected`/`ontology_unreadable` → dead). An operator retry is generation+1 and resets `failed` rows first. Migration 0003 backfills one job per applied command that still has pending or failed rows. `PROJECTION_SWEEPER_ENABLED` defaults to `false`; the sweeper code is deleted in P4.

## Data (migration 0003)

`interactions`, `interaction_contents`, `interaction_subjects`, `interaction_stages`, `outbox_jobs`, `job_attempts`, all with `workspace_id`, composite FKs, `ENABLE`+`FORCE` RLS. `kg_app` can INSERT/SELECT jobs but **never UPDATE** them: every state change goes through `ops.claim_jobs`, `heartbeat_jobs`, `complete_job`, `release_jobs`, `cancel_job` (scoped by the tenant setting) and `backlog_stats` (aggregates, no ids). `job_attempts` is append-only (trigger) and written only by those functions.

## Authorization additions

`interaction.write` (admin, app_writer, agent), `interaction.read` (admin, operator, reviewer, app_writer, and an agent for **its own submissions only**, an ownership predicate in the SQL), `interaction.read_content` (admin only), `operations.repair` (admin, operator). No existing cell changed (`test_policy` asserts the whole table). Every call to `/content` is audited in the same transaction as the read and answers `Cache-Control: no-store`.

## Failure matrix (gate)

| Fault | Where it is proved | Result |
|---|---|---|
| crash after claim / after side effect / before ack (×3, 50 events) | FI-01..03 | all `indexed`, ≤4 attempts per job, reclaims > 0, one normalised row each |
| SIGKILL ×3 during 50 events + 20 commands, 2 workers | FI-04 | all terminal, 20/20 projected, graph drift 0 |
| worker frozen past its lease (event loop blocked) | FI-05 | second worker reclaims; the frozen worker's late ack is `stale` or its handler is cancelled; one result |
| Postgres restart mid-burst | FI-06 | dispatcher reconnects; nothing lost |
| config-terminal poison | FI-07 | dead after 1 attempt, visible, replayed after the fault is cleared |
| Neo4j down | FI-08, demo step 6 | jobs back off visibly, then complete; drift 0 |

The invariant checker (`tests/support/invariants.py`) asserts after every scenario: no interaction without stages, no stage `running` without a live lease, no two `succeeded` jobs for one (subject, stage, generation), exactly one normalised row per indexed event, nothing pending, no plaintext sentinel in any table.

## Deviations from the plan (and why)

- **`ContentHasher` is keyed by `CONTENT_HMAC_SECRET`, not by the active payload key.** Rotating payload keys must not change request hashes, or every replayed request would become `409 idempotency_mismatch` (ADR-024).
- **Backoff is computed in Python and passed as a duration;** SQL adds it to `now()`. The retry policy stays unit-testable and the clock stays the database's.
- **An extra function `ops.cancel_job`.** Cancelling needs a state change `kg_app` may not make directly; the function is scoped by `current_workspace_id()` so no argument can widen it.
- **Subject resolution is a small SQL port inside `kg_ingestion`** (two read-only queries) rather than an import of `kg_knowledge`, which the import contract forbids.
- **Token counts use `kg_common.tokens.count_tokens`** (o200k, with an offline fallback), computed at acceptance and stored on the envelope (the row is immutable afterwards).
- **The worker starts the dispatcher before the graph index bootstrap** and retries the bootstrap on each heartbeat: a worker restarted during a Neo4j outage must still normalise events, and its projection jobs just back off.
- **`classify_error` reads the router's own error code** (the OpenAI SDK exposes it as `.code`): a 429 can be `rate_limited` (retry) or `budget_exceeded` (pause), which the HTTP status alone cannot tell apart.
- **`kg interactions reconcile` takes `--workspace` (required)**; repairs from the CLI need `--actor <handle>` so the audit trail says who ran them.
- **The Dashboard keeps its dependency health** (P1 tests and the e2e smoke depend on it) and gains an outbox summary with a link; the full health, backlog and dead letters live on Operations.
- **Production now also requires `CONTENT_HMAC_SECRET` and `PAYLOAD_KEYS`/`PAYLOAD_ACTIVE_KID`** (the dev keyring is refused, as are `KG_FAULTS` and `KG_FAULTS_DIR`). The P1 production-settings test was extended accordingly.
- **A `stall` fault point** (blocks the process for N seconds) reproduces a frozen worker deterministically for FI-05 and the demo; `KG_FAULTS_DIR/spec` arms the dev container without a restart.
- **Neo4j uniqueness constraints on `Entity.uuid` and `RELATES_TO.uuid`** (not in the plan): once the outbox projects a row at the same time as the request's fast path, Graphiti's `MERGE` on `uuid` alone creates duplicate nodes. `GraphService.ensure_indices` drops Graphiti's plain range index on those properties (Neo4j will not build a constraint over it) and creates the constraints; a constraint violation under contention is classified `graph_conflict` (retryable). Existing duplicates block the constraint: use the projection runbook's rebuild.
- **`GET /v1/audit?action=`** (not in the plan): the demos and tests look up one action instead of scanning the newest entries.
- **Cross-test hygiene:** a conftest fixture cancels the pending jobs a DB-backed test leaves behind, because tests share `kg_test` and a later test's worker would claim them (and spend its injected faults on them).

## Decisions taken in the plan (§13), as implemented

1. The outbox lives in `kg_jobs`, not `kg_ingestion`. 2. Only accepted interactions consume an idempotency key. 3. `interaction.read_content` is admin-only. 4. Agents submit and read only their own events until P7 scope ACLs. 5. Protection at rest is encryption plus an audited reveal (no separate DB role; a content-reader role is a P9 hardening option). 6. The P2 sweeper is off and is deleted at the start of P4.
