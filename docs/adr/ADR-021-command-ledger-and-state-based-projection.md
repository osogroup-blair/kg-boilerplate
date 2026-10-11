# ADR-021: Command ledger and state-based projection

**Status:** Accepted  **Decision refs:** ADR-007, ADR-015, ADR-016, ADR-017, ADR-018  **Evidence:** WP0 PJ-01..05 (`tests/integration/test_projection_compat.py`), `tests/integration/test_p2_projection.py`

## Context
Trusted apps and admins write structured facts (`entity.create`, `entity.update`, `relationship.assert`, `relationship.end`). They must be validated, auditable, idempotent, and projected into Neo4j without an LLM. Postgres is the system of record for entities, facts, status and record time (ADR-017); the graph is for retrieval.

## Decision
1. **Every command goes through one ledger** (`commands`): idempotency key, request hash, authority class, classification, ontology version, decision trace (stage, rule, code, path: never values), result ids, HTTP status. The ledger is immutable for the application role (column grants allow only `projection_*`, `last_error_code`, `projected_at` to change). Rejected and queued commands are recorded too.
2. **Entities and facts are written in the same transaction as the command and its audit entry.** Postgres stays authoritative: every read comes from Postgres, so a read is consistent with its write whatever the graph is doing.
3. **Projection is state-based.** The projector makes the node or edge equal the current Postgres row (uuid = row id). It does not replay deltas, so it is idempotent and independent of order. It runs after the commit, within a request budget (`PROJECTION_REQUEST_BUDGET_SECONDS`, 5 s): success answers 201, timeout or a retryable error answers 202 with the command's status URL, and a sweeper in the worker finishes the job (lease + `FOR UPDATE SKIP LOCKED`, exponential backoff, 8 attempts, then `failed` with a code; `kg commands retry-projection`). P3's outbox dispatcher replaces the sweeper through the `graph_projection` `JobHandler`.
4. **Direct saves only** (`ScopedGraph.upsert_entity_node` / `upsert_fact_edge`), never `add_triplet`. Only embeddings go to the router (purpose `graph_projection`, alias `oso/embedding`), under an egress context.
5. **Sensitive attributes are Postgres-only** and never reach the node, the fact text, an embedding or a log.
6. **`ScopedGraph` has no `group_id` parameter.** Graphiti MERGEs on uuid alone, so every write first proves the uuid is free or belongs to this group (another group's uuid is `NotFound`), and an edge may only join two nodes of the group.
7. **Drift check:** `kg graph reconcile --workspace <slug> [--repair]` compares every row with its node/edge (stripping `source_uuid`/`target_uuid` that Graphiti stores in `attributes`) and reports missing, mismatched, unexpected and orphaned items.

## WP0 findings (graphiti-core 0.30.2, Neo4j 5.26)
| Check | Result | Consequence |
|---|---|---|
| PJ-01 `date` / `datetime` attributes | Stored as native Neo4j `Date` / `DateTime`; an ISO string stays a string | The projector writes ISO strings (`YYYY-MM-DD`, UTC `…Z`); the drift comparison is then exact |
| PJ-02 save with `name_embedding` / `fact_embedding` = `None` | **Fails** (`db.create.*VectorProperty` throws a null-pointer error) | `local_only` data (egress denied) is **not projected**: `projection_status=skipped_by_policy`, Postgres-only. A fact is skipped when either endpoint is |
| PJ-03 edge with `episodes=[]`, `invalid_at`, `expired_at` | Round-trips exactly; read-back `attributes` contains `source_uuid`/`target_uuid` | `read_edge` strips them (M7) |
| PJ-04 re-save of the same uuid | Updates in place; no duplicate node or edge; attributes are **replaced**, not merged | Removing an attribute in Postgres removes it from the graph on the next projection |
| PJ-05 spy over the router transport | N embedding calls per save, zero `/chat/completions` | The CI gate (`test_the_zero_chat_gate_and_graph_equals_postgres`) |
| (found later) `LOG_LEVEL=DEBUG` | The Neo4j driver logs every Cypher statement **with parameters** | `configure_logging` pins `neo4j`, `graphiti_core`, `httpx`, `httpcore`, `openai`, `sqlalchemy.engine`, `psycopg` to WARNING; a test proves no content appears at DEBUG |

## Consequences
- The API never blocks on Neo4j or the router beyond the budget, and an outage leaves a consistent, readable system that heals itself.
- `local_only` entities are served from Postgres but invisible to graph retrieval until the egress policy changes. Relaxing the policy and re-queueing is `kg graph reconcile --repair`.
- Request-time projection and the sweeper can run on the same row at once. Both are idempotent and the final update is guarded by the row revision, so the loser is a no-op.
