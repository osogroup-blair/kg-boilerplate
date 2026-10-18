# ADR-051: Rebuilding the graph under parked uuids, cut over in one transaction

**Status:** Accepted (deviation from the plan's wording recorded)  **Decision refs:** Flow F, phase-9 plan WP8, ADR-015  **Evidence:** `tests/integration/test_p9_reindex.py`, `tests/resilience/test_p9_failure_injection.py` (FI-21..23)

## Context
The plan assumed a second Graphiti `group_id` could hold the same node uuids as the live one. It cannot: `Entity.uuid` and `RELATES_TO.uuid` have global uniqueness constraints and Graphiti merges by uuid.

## Decision
The rebuilt namespace is written under **parked uuids** (prefix `r<n>:`, the originals kept in `p_uuid`, `p_entity_edges`, `p_episodes`, `p_rev`). **Activation** is one Neo4j transaction that parks the live group and unparks the new one, plus the Postgres pointer flip (`workspace_graph_namespaces`), under the workspace's graph lock. **Rollback** is the reverse exchange, inside `REINDEX_RETENTION_DAYS`. A deletion clears every inactive namespace of the workspace. `mode reextract` replays `graph_ingest` after cutover with a cost preview first.

## Consequences
Exactly one namespace is live at any instant (proved at crash points FI-21..23). The cost is the park/unpark rewrite of node properties at cutover (proportional to graph size, measured in the baseline) and a rule that nothing else reads parked nodes (they are invisible to every query, which filters on the live group).
