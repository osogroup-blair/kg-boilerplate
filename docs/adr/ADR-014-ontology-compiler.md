# ADR-014: Ontology compiler approach

**Status:** Accepted  **Decision refs:** DEC-05, ADR-006  **Evidence:** GR-03 · `spikes/graphiti/ontology_compiler.py`

## Context
Can ontology YAML be turned into Graphiti entity/edge Pydantic types at runtime, safely?

## Decision
**Dynamic compilation works and is the approach.** `pydantic.create_model` builds types that Graphiti accepts in `add_episode(entity_types=, edge_types=, edge_type_map=)`; they appear as Neo4j labels and as JSON-Schema attribute-extraction calls. The allowlisted-static-class fallback is **not needed**. Rules the compiler enforces:
1. **Reserved fields.** `validate_entity_types` rejects any attribute that shares a name with an `EntityNode` field (`uuid, name, group_id, labels, created_at, name_embedding, summary, attributes`). **The blueprint's own example (Person.name) collides.** `name` is the node's own name, so the compiler does not emit it (it stays the platform identity alias); any other collision is a compile error shown to the ontology author. Edge types are checked against `EntityEdge` fields.
2. **All compiled fields are Optional/default None.** Extraction is best-effort; a required Graphiti field would fail a whole extraction for one missing attribute. `required` is enforced by the platform validator (P2 rule catalog), not by the Graphiti type.
3. **Safe identifiers.** Type and relationship names must match `^[A-Za-z][A-Za-z0-9_]{0,63}$` and avoid Graphiti's labels (`Entity, Episodic, Community, Saga`) and edge names (`MENTIONS, HAS_EPISODE, NEXT_EPISODE, RELATES_TO, HAS_MEMBER`), because labels are interpolated into Cypher.
4. Relationship endpoints must be declared entity types; `edge_type_map` is derived from source/target.
5. Supported attribute types: string, integer, number, boolean, date, datetime. Others are rejected explicitly.

## Consequences
The compiler is pure (YAML in, types out) and unit-testable; P2 ports it into `packages/ontology` with the full rule catalog. Because Graphiti sends the compiled models as response schemas, live runs should confirm `anyOf [T, null]` schemas are accepted by the structured-output models (GR-11).
