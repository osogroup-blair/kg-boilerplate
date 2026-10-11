# ADR-016: Deterministic write path into Neo4j

**Status:** Accepted  **Decision refs:** ADR-007, ONT-*  **Evidence:** GR-05

## Context
Trusted structured commands (`POST /v1/entities`, relationship commands) must not call an LLM.

## Decision
- Commands are projected with **direct saves**: `graphiti.nodes.entity.save(EntityNode)` and `graphiti.edges.entity.save(EntityEdge)`. Measured: **zero chat calls**; only embedding calls (needed so the node/fact is searchable) (GR-05).
- **Do not use `add_triplet`**: it calls the LLM whenever an edge already exists between the nodes (an `EdgeDuplicate` call was observed), and its node-resolution step falls back to LLM dedup for candidates the deterministic similarity pass cannot settle (read from `resolve_extracted_nodes` → `_resolve_with_llm`; not exercised in the spike).
- A platform **projection writer** (P2) owns uuids (deterministic from the command ledger id), `valid_at`, `invalid_at`, `created_at` (REQUIRED constructor field, so the platform always chooses record time) and supersession (it sets `invalid_at`/`expired_at` on the predecessor itself).
- Read-back quirk: Graphiti puts `source_uuid`/`target_uuid` into `EntityEdge.attributes`; the projection reader strips them before comparing.
- CI assertion for P2: a spy transport proves a command write sends no `/chat/completions` request.

## Consequences
Two coordinated write paths (commands = direct save; interactions = `add_episode`) share the same ontology and `group_id` rules; both write through the adapter.
