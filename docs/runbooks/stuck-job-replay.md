# Runbook: stuck, failed and dead jobs; audited replay

Events and commands are processed by a Postgres outbox. A job is never lost (it commits with the state change that needs it), and a crashed worker's jobs resume when their lease expires. This runbook is for what is left: jobs that retried and stopped, and events that ended `failed` or `partial`.

## Where to look
| Where | What it shows |
|---|---|
| Admin console → **Operations** | health, workers' last heartbeat, backlog by stage and status, oldest pending age, lease reclaims (1 h, 24 h), **dead letters** |
| Admin console → **Interactions & Episodes** → an event | the stage timeline: state, attempts, generation, error category and code, timings; pinned pipeline and ontology versions |
| `GET /v1/operations/jobs?status=dead`, `/v1/operations/backlog`, `/v1/operations/jobs/{id}` | the same, as JSON (attempts with worker ids, outcomes and durations) |
| `uv run kg jobs list|show --workspace <slug>` | the same from a terminal |
| Prometheus | `outbox_backlog{stage,status}`, `outbox_oldest_pending_age_seconds{stage}`, `outbox_dead_letters{stage}`, `outbox_lease_reclaims_total{stage}`, `outbox_stale_completions_total{stage}`, `outbox_jobs_total{stage,outcome}`, `interactions_status_total{status}` |

## Reading a job
| Status | Meaning | What to do |
|---|---|---|
| `pending`, `available_at` in the future | waiting out a backoff after a retryable error | nothing; check `last_error_code` (`graph_unavailable`, `router_unavailable`) and the dependency |
| `pending`, `attempts` unchanged, `budget_exceeded` | paused for ~15 minutes without consuming an attempt | restore budget in the router |
| `leased` | a worker holds it; if its lease passes, another worker reclaims it (a "lease reclaim") | many reclaims in an hour means workers are dying or stalling: check the worker log and `docker compose ps` |
| `skipped` | a policy-terminal error: the stage is `skipped_by_policy` (for example data that may not leave the platform) | nothing; this is a decision, not a fault |
| `dead` | config-terminal error, or attempts exhausted | fix the cause, then replay (below) |
| `cancelled` | an operator cancelled it | none |

`last_error_code` values are tokens, never content: `fault_injected` (test only), `unsupported_content_type`, `raw_content_missing`, `stage_missing`, `generation_mismatch`, `attempts_exhausted` (a job that crashed workers until it ran out of attempts), `graph_unavailable`, `router_unavailable`, `router_rejected`, `router_policy`, `graph_rejected`, `ontology_unreadable`, `command_missing`.

## Procedures
```bash
# replay a dead/failed stage of an event (a new generation; results are written once)
uv run kg interactions replay <event_id> --workspace demo --actor operator@demo --reason fault_cleared [--stage normalize]

# retry a dead job (becomes a new job one generation higher; the dead one stays as history)
uv run kg jobs retry <job_id> --workspace demo --actor operator@demo --reason config_fixed

# cancel a pending or dead job (the stage becomes dead: cancelled_by_operator; the event is finalised partial/failed)
uv run kg jobs cancel <job_id> --workspace demo --actor operator@demo --reason not_needed

# find work the outbox lost track of (exit 1 while findings remain), and re-enqueue it
uv run kg interactions reconcile --workspace demo
uv run kg interactions reconcile --workspace demo --repair
```
The same actions are in the console (Replay… on an event, Retry… / Cancel… on a job) and in the API (`POST /v1/interactions/{id}/replay`, `/v1/operations/jobs/{id}/retry|cancel`, all with an `Idempotency-Key` and a `reason_code`). **Every replay, retry and cancel needs a reason code** (`^[a-z][a-z0-9_]*$`) and writes an audit entry with the actor, the stage, the generation and the reason. CLI actions are attributed to `--actor`, which must be a principal of the workspace.

A replay of a stage that is still `pending` or `running` is `409 stage_running`: wait, or cancel its job. A replay with nothing dead or failed is `409 nothing_to_replay`. A foreign or unknown id is `404`.

## Reconcile
`kg interactions reconcile` reports, per workspace: `missing_stage` (an event lacks a stage its pipeline defines), `missing_job` (a stage or an applied command is waiting but no live job exists), `terminal_job_open_stage` (a job finished but its stage did not; repair bumps the generation), `orphan_job`. It should be clean; a finding means something removed or lost a job row. `--repair` re-enqueues with the right dedupe key, so running it twice is safe.

## If the whole queue is stuck
1. `make ps` / `docker compose ps`: is the worker running and healthy? The Operations page shows the last heartbeat.
2. Worker log (`docker compose logs worker`): `outbox_claim_failed` means the database is unreachable (the dispatcher keeps retrying), `job_completion_failed` means a lost acknowledgement (the lease expires and the job re-runs), `job_unexpected_error` carries only an exception type.
3. A job whose `attempts` equals `max_attempts` and whose workers keep dying is a poison event: it is dead-lettered without running again. Cancel it or fix the data, then replay.
4. The P2 sweeper no longer exists (deleted in Phase 4); the outbox is the only driver of projection. For `graph_ingest` jobs see [extraction-quarantine](extraction-quarantine.md).

## Failure injection (development only)
`KG_FAULTS` / `KG_FAULTS_DIR` are refused when `ENV=production`. In the dev stack the worker re-reads `.local/faults/spec` (one rule per line, `stage:point:count`, plus `stage:stall:count:seconds`); counters live in `.local/faults/counters.json`. Delete both files to disarm. `make demo-p3` and the Playwright ADM-06 spec use this.
