# Runbook: extraction failures and quarantine

`graph_ingest` is the only stage that calls a generative model. It fails in four ways, each with a stable code on the stage (`GET /v1/interactions/{id}`, Admin console → Interactions → stage timeline, Operations).

| `last_error_code` | Meaning | What the platform does | What you do |
|---|---|---|---|
| `extraction_unparseable` | the model answered something Graphiti cannot parse | one retry, then **dead letter**, event `partial` (the normalized content is still searchable); nothing is written to the ontology, policy or graph | fix the cause (model/router config, prompt-injection source), then **Replay** the stage (operator; audited) |
| `extraction_timeout` | the call exceeded `GRAPH_INGEST_TIMEOUT_SECONDS` | retried with backoff | check router latency; raise the limit only if the model is merely slow |
| `graph_unavailable` | Neo4j down or restarting | retried with backoff (the lock is released between attempts) | start Neo4j; the job heals itself |
| `graph_busy` | another job holds the workspace graph lock | retried shortly | none, unless it never ends: look for a stuck `graph_projection` job in Operations |
| `budget_exceeded` / `router_*` | router refused | budget pauses the queue ~15 minutes without consuming attempts | see [stuck-job-replay](stuck-job-replay.md) |

`skipped_by_policy` with zero model calls is correct for events whose consent or classification forbids external processing.

## Replaying a quarantined event
```bash
uv run kg interactions replay <event_id> --stage graph_ingest --reason source_fixed --actor <you> --workspace <ws>
# re-extract against the CURRENT ontology and pipeline version instead of the pinned ones:
uv run kg interactions replay <event_id> --stage graph_ingest --reason ontology_changed --repin --actor <you> --workspace <ws>
```
The replay creates the next generation (a new deterministic episode uuid), so earlier facts and their evidence stay; facts the new run also finds gain evidence rather than duplicates.

## Inspecting cost and volume
`uv run kg extraction stats --workspace <ws>` (model calls, tokens and estimated cost per episode). `uv run kg extraction purge --workspace <ws>` is the guarded last resort: it deletes **everything** extraction produced in the workspace (entities, facts, mentions, claims, episodes, extraction reviews), clears its graph and re-projects the command-sourced rows; interactions stay and can be re-extracted with `--repin`. It is also required before `kg db downgrade 0003`.

## After an incident
`uv run kg graph reconcile --workspace <ws>` must report no drift; `--repair` re-projects from Postgres ([projection-repair](projection-repair.md)). Content never appears in logs: use `episode_uuid` and `correlation_id` (`gi-{interaction}-g{generation}`) to find the model calls.
