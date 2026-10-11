# Ontology, Type Registry and Rules

**Parent:** [Blueprint index](../README.md)  
**Capability owners:** CAP-01 / CAP-02  
**Planning question:** How can an admin define the legal vocabulary of a graph without writing code per domain?

## Core concepts

An **ontology** is a versioned schema containing `entity_types`, `relationship_types`, `attribute_definitions`, and `policies`. A **rule** constrains writes, inference, validity, or review. A published version is immutable. A draft can be changed and previewed.

## Example concepts

- `Person`: stable person ID, name, aliases, birth date (sensitive); age should normally be derived from birth date instead of stored as mutable truth.
- `Organization`: name, type, identifiers.
- `Asset`: model/serial and ownership lifecycle.
- `Topic`: e.g. entrepreneurship, cycling.
- Relationships: `WORKS_AT`, `OWNS`, `KNOWS`, `CONSIDERING`, `PARTICIPATES_IN`.
- A significant meeting can be both an event and a graph entity if it has participants and an ongoing query need; don't require every message to be a node.

## Canonical config model

| Concept | Required fields |
|---|---|
| Ontology version | `ontology_id`, `workspace_id`, `version`, `state=draft/published/retired`, `created_by`, `published_at`, content hash |
| Entity type | `key`, description, attributes, identity strategy, allowed classifications |
| Attribute | `key`, primitive/reference/array type, required/default, editable, sensitive, source priority, temporal policy |
| Relationship type | `key`, source type, target type, cardinality, direction, properties, temporal policy |
| Rule | `rule_id`, stage, type, severity, condition, action, revision, test cases |

## Declarative rule families

1. **Structural:** `WORKS_AT` is `Person -> Organization`; refuse unknown node types if strict mode.
2. **Validation:** prohibit invalid dates or wrong scalar types; distinguish validation from factual truth.
3. **Cardinality:** one active `PRIMARY_EMPLOYER` per person if domain policy says so; don't impose globally on `WORKS_AT` (multiple jobs exist).
4. **Temporal:** new validated employment may expire a prior conflicting role effective at a stated time; keep original provenance.
5. **Authority:** user-confirmed or authoritative-system fact outranks an unsupported agent guess.
6. **Inference:** proposed AI facts may require reviewer approval based on sensitivity, confidence calibration, or conflict.
7. **Retention:** source and memory lifecycles, including holds and deletion propagation.
8. **Retrieval:** permissible memory types/scopes per agent, maximum context tokens, freshness preference.

## Rule evaluation order

`authenticate -> resolve workspace -> load published schema -> schema validation -> source classification -> identity resolution -> factual conflicts/temporal rules -> authorization for mutation -> accept/propose/reject -> persist and audit`.

Rules are **declarative, deterministic** initially. Avoid allowing arbitrary administrator-provided Python/JavaScript in the UI; this creates execution/security issues. Implement a fixed catalog of predicates/operators and explicit versioned rule evaluators.

## Graphiti integration

Graphiti's custom entity/edge types can be defined with Pydantic models and supplied during episode processing. The platform should compile a controlled subset of the published ontology to compatible Pydantic types or runtime schema mappings. Enforcement of business-specific constraints and reviewer workflows remains platform responsibility. Generated identifiers and reserved Graphiti fields must not conflict with ontology attribute names. Current reference: https://help.getzep.com/graphiti/core-concepts/custom-entity-and-edge-types

## Schema lifecycle

`draft -> validate -> simulate on sample episodes -> diff -> publish -> pinned in new jobs -> optional migration/reindex/reingest -> retire`. Record source schema version for every event and memory derivation. For a breaking change, build a new graph namespace and compare evaluations before cutover.

## Requirements / acceptance

- `ONT-01` Admin can create/edit a draft `Person`, `Organization` and `WORKS_AT` definition and see schema validation errors.
- `ONT-02` Publish creates immutable version, audit entry and content hash; rollback points to an older published version without mutation of history.
- `ONT-03` Worker logs ontology version used for each processing job.
- `ONT-04` Disallowed typed links are rejected or queued based on configured rule; don't silently accept them.
- `ONT-05` A schema migration can be previewed for affected types/relationships before publication.
- `ONT-06` Every rule has deterministic fixture tests and a documented conflict policy.

See [example configuration](../06-reference/02-example-config-and-payloads.md).
