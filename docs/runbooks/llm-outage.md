# Runbook: the model router is down, over budget, or missing an alias

**Detect:** alert `RouterErrorRate`, console Operations → Health "Router", Cost panel errors, `POST /v1/operations/router-check` (operator) lists `missing_aliases`, worker log `egress_denied` / `router_*` events.

## What degrades, and what does not
Embeddings and chat go through one gateway with an egress policy in front of it. A failed call is classified (`provider_unavailable`, `rate_limited`, `budget_exceeded`, `cost_limit_exceeded`, `classification_not_permitted`, `model_not_found`, `timeout`, `invalid_structured_output`) and recorded in the metadata-only call log.
| Surface | Behaviour |
|---|---|
| Ingestion | Accepted; `normalize` always completes. `graph_ingest` and the memory stages retry with backoff (the error category is on the job). The event stays `accepted`/`partial`, never silently dropped. |
| Deterministic commands | Unaffected (no model call). |
| Memory search | Falls back to lexical-only when the query embedding fails (`memory_search_total{degraded="true"}`); results are labelled degraded. |
| Context builds | `structured` and `extractive` need no chat model; `summarized` falls back to `structured` (`context_compression_fallbacks_total`). Embedding failure makes the vector half unavailable → `partial`. |
| Consolidation, simulation, reindex `reextract` | Fail cleanly and can be re-run; nothing is applied half-way. |

## Triage by error code
- `provider_unavailable` / `timeout` / `routing_failed`: the router or its providers. Check the router's own status page/logs; nothing to change here. The outbox retries; the canary alerts if contexts fail.
- `rate_limited`: lower `WORKER_CONCURRENCY` / the stage limits (`WORKER_STAGE_LIMITS`) and Graphiti's `SEMAPHORE_LIMIT`; the live session calibrates the safe value (GR-13).
- `budget_exceeded` / `cost_limit_exceeded`: the router-side budget for the key is spent. Raise it at the router, or pause extraction (a workspace can set memory/graph extraction to `disabled` in its ontology) until the window resets. The Cost panel shows where the spend went (by purpose and alias).
- `model_not_found` / `router-check` missing aliases: an alias was renamed or removed at the router. Restore it, or change `compat/model-purposes.yaml` (a model-affecting change: the eval gate needs a live record, see [upgrade-pin-dependencies.md](upgrade-pin-dependencies.md)).
- `classification_not_permitted` / `data_policy_violation`: the key's `maxClassification` is below the data, or a workspace egress setting denies the class. This is the control working; change the key's ceiling only with the data owner's approval.
- `router_key_is_admin` in the log: the key returned provider headers. Replace it with a non-admin key now (it is an error under `ENV=production`).

## Recover
When the router is back the backlog drains by itself. Replay dead letters only after the cause is fixed ([stuck-job-replay.md](stuck-job-replay.md)). Run `kg reconcile --scope all` (check-only) once the queues are empty; episodes missing because jobs died are listed, and `--repair --allow-reingest` prints a cost preview before it spends anything.
