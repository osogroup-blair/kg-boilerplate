# Runbook: graph projection status and repair

Postgres is authoritative for entities and facts. The graph is a projection, so a projection problem never loses data and never blocks reads; it only delays graph-based retrieval (P4+).

> **Since Phase 3 the outbox drives projection.** Every applied command enqueues a `graph_projection` job in its own transaction and the worker's dispatcher runs it (the P2 sweeper is off by default). Retries, backoff, dead letters and operator retry/cancel are described in [stuck-job-replay.md](stuck-job-replay.md); the status codes and `kg graph reconcile` below are unchanged.

## What you can see
| Where | What |
|---|---|
| `GET /v1/commands/{id}` | `projection_status` (`pending`, `projected`, `failed`, `skipped_by_policy`, `not_applicable`), `projection_attempts`, `last_error_code` |
| `GET /v1/entities`, `GET /v1/relationships/{id}` | the row's own `projection_status` |
| Admin console → Entities → Commands | the same, with the decision trace |
| Admin console → Operations | the command's `graph_projection` job: attempts, backoff, dead letter, Retry / Cancel |
| Prometheus | `projection_total{kind,outcome}`, `projection_pending`, `projection_lag_seconds`, `commands_total`, `rule_violations_total` |

## Error codes
| `last_error_code` | Meaning | Retried automatically |
|---|---|---|
| `graph_unavailable` | Neo4j unreachable or transient error | yes: job backoff (full jitter, 2 s doubling to 10 min), 8 attempts, then a dead letter |
| `router_unavailable` | embedding call failed (timeout, `rate_limited`, 5xx) | yes |
| `budget_exceeded` | the router refused for budget | the job pauses ~15 minutes **without consuming an attempt** |
| `router_policy`, `egress_denied` | the router/policy refused this data | no: the rows become `skipped_by_policy` and the job is `skipped` |
| `endpoint_not_projected` | an edge's node is not in the graph yet | yes |
| `router_rejected`, `graph_rejected`, `ontology_unreadable` | will not heal by waiting | no: the job is dead at once and the command `failed`; fix the cause and Retry (a new generation resets the failed rows) |
| `unexpected_error` | anything unclassified | yes, up to 8 attempts, then dead |

`skipped_by_policy` is not an error: `local_only` data is never sent to the router, and Graphiti cannot save a node or edge without an embedding, so it is not projected.

## Procedures
```bash
# 1. is the graph equal to Postgres?  (exit code 1 when drift remains)
uv run kg graph reconcile --workspace demo

# 2. repair drift: re-projects missing/mismatched rows and deletes orphans
uv run kg graph reconcile --workspace demo --repair

# 3. a command whose projection is `failed`: make its rows due again and project now
uv run kg commands retry-projection <command_id> --workspace demo
```
If the worker is not running, nothing finishes pending rows (the jobs wait in the outbox and no work is lost). Check `make ps` and the worker log (`job_done`, `projection_attempt_failed` carry codes, never content). Rolling back to the P2 driver is `kg db downgrade 0002` and `PROJECTION_SWEEPER_ENABLED=true`.

## Duplicate nodes
The graph has uniqueness constraints on `Entity.uuid` and `RELATES_TO.uuid` (created by the worker at start-up). If the worker log shows `graph_uniqueness_constraint_failed`, the graph already holds duplicates from before the constraints existed (Graphiti's `MERGE` is not atomic without them). Rebuild the affected workspace as below, then restart the worker. `graph_conflict` on a projection job is a lost race between two writers of the same row and is retried automatically.

## Rebuilding a workspace's graph from Postgres
```bash
# clear this workspace's group (ADR-015) in Neo4j, then re-project everything
#   MATCH (n {group_id: 'ws-<workspace uuid>'}) DETACH DELETE n
uv run kg graph reconcile --workspace demo --repair
```
Reconcile treats every row marked `projected` as expected in the graph, so after clearing the group it reports them missing and the repair re-projects them.
