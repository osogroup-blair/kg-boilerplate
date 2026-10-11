# ADR-022: Ontology spec and rule catalog

**Status:** Accepted  **Decision refs:** ADR-014, ONT-01..06  **Evidence:** `packages/ontology/tests` (143 tests), `packages/graph/tests/test_compile.py`

## Decision
1. **One canonical spec.** The blueprint YAML and the designer's JSON both normalise to `OntologySpec` (Pydantic, `extra="forbid"`). Shorthands are expanded (`ontology.id` → `key`, `identity.aliases` → `aliases_from`, bare rule names), defaults are filled, lists are sorted, and `content_hash = sha256(canonical JSON)`: two documents with the same meaning hash the same.
2. **Identifier safety lives in the model.** Type keys match `^[A-Za-z][A-Za-z0-9_]{0,63}$`, relationship keys `^[A-Z][A-Z0-9_]{0,63}$`, and neither may be a name Graphiti reserves, because Graphiti interpolates them into Cypher. The check cannot be skipped by any caller.
3. **`name` is the node's own name.** It is a declared attribute (for `required`, `pattern`, `max_length`) but is never compiled as a Graphiti field and cannot be sent in `attributes`. Any other collision with a Graphiti field (`labels`, `summary`, `uuid`, …) is a validation error shown at `entity_types.<T>.attributes.<name>`, not a publish-time crash. Compiled fields are all Optional: `required` is enforced by the platform at command time.
4. **A fixed rule catalog**, no expressions and no user code. Each kind has typed params, a stage, a family, an evaluator that is pure (no clock, no I/O, enforced by a source scan), a default action, a documented conflict policy and fixture cases (a meta-test fails the build if any is missing). Six rules are implicit (strict types, endpoint types, identity, attribute types, valid interval, classification); eight are declarable: `preserve_valid_time`, `succession`, `unique_active_pair`, `max_active_per_source`, `require_source`, `min_authority`, `require_review_if_inferred`, `queue_if`.
5. **Evaluation order** follows the blueprint: schema validation → classification → identity (DB) → temporal/cardinality → authority → review → decision. A stage that rejects stops the evaluation (later stages assume a valid command); `queue_if` softens a named implicit rejection into `queue_review`.
6. **Rules carry scenario tests** (`tests: [{given, command, expect}]`) that run at validation and block publish on a mismatch.
7. **Published versions are immutable**, enforced in Postgres by a trigger that also binds the table owner. Publish and activate serialise on the `workspace_ontology` row. One ontology key per workspace (open decision 1: confirmed for v1); rollback appends an activation and moves the pointer.
8. **Sensitive attributes** (`sensitive: true`, or `sensitivity: restricted|local_only`) stay in Postgres. A fact template may not reference one.

## Consequences
New rule kinds need code, fixtures and a conflict policy. The designer reads the catalog (`GET /v1/ontologies/rule-catalog`) to render rule forms, so a new kind appears in the UI without UI code.
