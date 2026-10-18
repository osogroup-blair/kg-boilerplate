# Runbook: Neo4j is down or unhealthy

**Detect:** alert `GraphDependencyDown` (`dependency_up{dependency="neo4j"} == 0`), `CanaryFailing`/`CanaryStale`, console Operations → Health shows Neo4j failing, `/ready` reports it.

## What the platform does by itself
| Surface | Behaviour while Neo4j is unavailable |
|---|---|
| Deterministic commands (`POST /v1/entities`, relationships, corrections) | The ledger write (Postgres) succeeds; the graph projection cannot complete, so the answer is `202` with `projection_status: pending`. The outbox retries with backoff. Nothing is lost. |
| Event ingestion | Accepted into the ledger; `normalize` and the memory stages run; `graph_ingest` retries and stays `partial`/`accepted`, never `indexed`, until the graph is back. |
| Context builds | The graph source is reported in `unavailable_sources`; the package is built from Postgres (memories, facts) and `status` is `partial` (or `insufficient_evidence`). The budget still holds. Agents that set `allow_partial=false` get `503 context_incomplete`. |
| Reads that need the graph (explorer, graph search) | Degrade with a clear "graph unavailable" state; Postgres-backed pages keep working. |
| Deletions | The Postgres half proceeds; the graph step retries; the request stays `purging` and **reads of the subject stay blocked** until verification passes. |

## What to do
1. `docker compose ps neo4j` and `docker logs <neo4j> --tail 200`. Typical causes: out of memory (heap/pagecache above the VM; raise `NEO4J_HEAP`/`NEO4J_PAGECACHE` or the VM), disk full, a corrupt store after a hard kill.
2. Restart the container. Wait for the healthcheck (`cypher-shell RETURN 1`). The worker and API reconnect on their own; no restart of either is needed.
3. Watch the backlog drain on Operations → Backlog (`graph_ingest`, `graph_projection`). Dead letters are not expected from an outage; if any appear, replay them with a reason ([stuck-job-replay.md](stuck-job-replay.md)).
4. **Always run reconciliation afterwards:** `kg reconcile --workspace <each> --scope graph` (console: Operations → Reconciliation → Scan). Drift classes name ids only; `--repair` fixes the safe ones from Postgres.
5. If the store is lost or corrupt: restore Neo4j from the newest dump ([backup-restore.md](backup-restore.md)), then `kg deletions reapply` (so the restore forgets again), then reconcile. If there is no usable dump, rebuild from Postgres: [reindex-ontology.md](reindex-ontology.md) (`mode project` needs no model calls).

## After
Write down the duration and cause; if `graph_ingest` p95 or the freshness lag exceeded the proposed SLOs, note it in the incident log (the decision register tracks the SLOs).
