# Phase 2: Ontology Registry and Deterministic Entity Commands — Detailed Implementation Plan

**Roadmap:** [ROADMAP.md](../../ROADMAP.md), Phase 2 (blueprint Slice 1)
**Blueprint inputs:**
- `02-domain/01` (ontology, rule families, evaluation order, ONT-01..06)
- `02-domain/02` (identity, time semantics, fact state machine, KGR-*)
- `02-domain/03` (source-of-truth priority, idempotency)
- `01-architecture/03` Flow B (deterministic authoritative fact)
- `03-runtime/01` (endpoints, API shape rules)
- `04-administration/01` (Ontology designer, Entity Directory, ADM-01)
- `06-reference/01` (Sarah evt_001/evt_002)
- `06-reference/02` §A (ontology YAML)

**Phase 0 inputs (binding):**
- ADR-014 (ontology compiler)
- ADR-015 (tenancy, `group_id`)
- ADR-016 (deterministic write path: direct save, never `add_triplet`)
- ADR-017 (temporal mapping; Postgres is the system of record for record time and status)
- ADR-018 (the correction projection is authoritative in Postgres)
- `spikes/graphiti/ontology_compiler.py`, `spikes/graphiti/personal_knowledge.yaml`

**Phase 1 inputs (binding):**
- `docs/architecture/p1-foundation.md`, including its "Deviations" section;
- the code in `packages/*` and `apps/*`. The patterns to reuse are `tenant_session`, `@requires`, `ALL_ROUTERS`, `AuditWriter`/`AuditMetadata`, `ScopedGraph`, `RouterTransport` + `egress()`, `GraphService`, `JobRegistry`, the seed's uuid5 IDs, and `Page[T]` cursors.

**Status:** Ready for a coding agent. **Does not need the live router.** Projection embeddings go to the mock.

---

## 1. Objective

Make the platform's vocabulary configurable and its trusted writes deterministic:
1. **Ontology registry.** Workspace admins draft, validate, diff, publish and roll back versioned ontologies (entity types, attributes, relationship types, rules). Published versions are immutable and content-hashed. The admin console has a form-based designer, so a non-developer can do it (ADM-01).
2. **Declarative rule catalog.** A fixed, versioned set of evaluators (structural, validation, cardinality, temporal, authority, review). Each has fixture tests and a documented conflict policy. No user code (ONT-06).
3. **Deterministic commands.** Trusted apps and admins create entities and assert or end relationships through an idempotent **command ledger**. Commands are validated against the *active published* ontology by the blueprint's evaluation order, stored in Postgres (the system of record for entities, facts, status and record time), and **projected into Neo4j by direct save with zero LLM calls** (ADR-016).
4. **Reads.** Entity and relationship reads with current state, `as_of` (valid time) and `known_at` (record time) filters, history, and provenance to the source command.

**Demo:**
1. The admin publishes the `personal-knowledge` ontology (Person, Organization, Topic; WORKS_AT, KNOWS, CONSIDERING).
2. `conversation_app` creates Sarah, Acme, Michael and Entrepreneurship.
3. It records `evt_001` (Software Engineer at Acme from 2022-01-10) and `evt_002` (Engineering Manager from 2024-06-01, recorded by HR on 2024-06-10).
4. Sarah's current employer and role, her history and her `as_of` answers are correct in Postgres and in the graph.
5. A disallowed edge is rejected.
6. No `/chat/completions` call was made.

**Gate:**
- ONT-01, ONT-02, ONT-04, ONT-06;
- ONT-03 partial: every command and projection records its ontology version;
- ONT-05 partial: diff plus impact counts;
- a disallowed edge is rejected or queued, per the rule;
- zero chat calls for command writes;
- the KGR-01/03/04/05 deterministic subset for command-sourced facts;
- all P1 security gates stay green, with new cross-workspace tests for every new route.

---

## 2. What Phases 0–1 Taught Us (and how P2 applies it)

| # | Finding (source) | P2 decision |
|---|---|---|
| M1 | The blueprint's own YAML collides with Graphiti's reserved `name` field (ADR-014, GR-03) | `name` on an entity type becomes the node's own name (the platform's `display_name`) and is never compiled as an attribute. Any other reserved collision is a **validation error shown to the author on the field**, not a publish-time crash. |
| M2 | Compiled Graphiti fields must be Optional; `required` is the platform's job (ADR-014) | `required` and `default` are enforced by the platform validator at command time. The Graphiti types are compiled for P4 extraction with all fields Optional. |
| M3 | Labels and edge names are interpolated into Cypher (ADR-014) | Type and relationship keys must match `^[A-Za-z][A-Za-z0-9_]{0,63}$` and avoid Graphiti's labels and edge names. Relationship keys must be UPPER_SNAKE. These checks sit in the spec model, so they cannot be skipped. |
| M4 | `add_triplet` calls the LLM; direct `nodes.entity.save` / `edges.entity.save` make zero chat calls (ADR-016, GR-05) | The projection uses **direct saves only**, through new `ScopedGraph` write methods. A CI spy asserts that command writes send **0** `/chat/completions` requests (embeddings are allowed). |
| M5 | `EntityEdge.created_at` is required; `add_episode` stamps wall-clock time; Postgres must be the system of record for record time (ADR-017, GR-06) | The **Postgres `facts` table is authoritative** for status, `recorded_at`, `superseded_at`, `valid_from` and `valid_to`. The graph edge mirrors it: `valid_at`, `invalid_at`, `created_at = recorded_at`, `expired_at = superseded_at`. |
| M6 | Invalidated edges still appear in Graphiti search unless filtered; in-place edits lose history (ADR-018, GR-07) | Nothing is edited in place: a change is a new fact plus a status or validity change on the old one. Graph edges carry the mirrored timestamps so P4/P6 can filter on them. |
| M7 | Read-back edges carry `source_uuid`/`target_uuid` inside `.attributes` (ADR-016) | The projection reader strips them before the drift comparison. |
| M8 | Neo4j Community has no per-tenant DB; the adapter is the only boundary (ADR-015) | New graph writes exist only on `ScopedGraph`. The `group_id` is never a parameter, and node and edge UUIDs are checked against the group before any update. |
| M9 | `FORCE` RLS hides rows from a `SECURITY DEFINER` owner unless it has BYPASSRLS; `kg_owner` therefore has BYPASSRLS (P1 deviation) | Cross-workspace worker discovery (the projection sweeper) uses the same pattern: an `ops.*` `SECURITY DEFINER` function owned by `kg_owner` that returns **only workspace IDs**. All real work then runs in a per-workspace `tenant_session`. |
| M10 | FastAPI 0.143 wraps included routers lazily; `kg_api.routers.ALL_ROUTERS` is the policy registry (P1 deviation) | New routers (`ontology`, `entities`, `relationships`, `commands`) are added to `ALL_ROUTERS`. T-REG covers them automatically. |
| M11 | Per-request state must not live in `request.state` (P1 deviation) | The idempotency and ETag helpers keep their state in the ASGI scope or explicit dependencies. |
| M12 | Judge time with the database clock (P1 deviation) | `recorded_at`, `projected_at` and idempotency expiry use `now()` / `clock_timestamp()` from Postgres. Never compare them with host time. |
| M13 | `ROLE_ACTIONS` "never widen silently" is asserted by `test_policy` (P1) | New actions are added with an explicit matrix update and test change (§6.3). No existing cell changes. |
| M14 | Embeddings go through `RouterTransport`, which fails closed without an egress context (P1 L7/L8) | Projection wraps every save in `egress(classification=<fact/entity class>, purpose="graph_projection", …)`. `local_only` data is projected **without** embeddings (`embedding_status=skipped_by_policy`), never sent. This needs a new purpose `graph_projection` in `compat/model-purposes.yaml` (embedding-only, alias `oso/embedding`). |
| M15 | The mock catalog and fixtures are synced from the router seed (Phase 0) | Projection tests run against the in-process mock with deterministic 384-dim embeddings, exactly like P1's gateway tests. |
| M16 | P2 and P3 may run in parallel and both touch migrations, `ROLE_ACTIONS`, `ALL_ROUTERS`, admin navigation and the worker (roadmap) | Migration `0002_ontology_and_commands` has `down_revision="0001"`. If P3 is cut from the same base, it adds an Alembic **merge revision**. The projection driver is written as a `JobHandler` (`stage="graph_projection"`) so P3's outbox dispatcher can take it over. The P2 sweeper is a temporary driver behind a setting. |

---

## 3. Scope

**In scope:**
- packages `kg_ontology` (spec, loader, validator, rule catalog and evaluators, diff, canonical hash) and `kg_knowledge` (command pipeline, entity and fact store, projection orchestration);
- new `ScopedGraph` write and read methods and the Graphiti type compiler in `kg_graph`;
- migration 0002;
- the ontology, entity, relationship and command APIs;
- a generic idempotency store;
- the projection sweeper in the worker;
- CLI additions;
- admin pages: Ontology designer (types, relationships & rules, versions/diff/publish) and Entity Directory (with commands);
- `demo-p2`.

**Out of scope:**
- Graphiti episode ingestion and LLM extraction, identity *candidate* generation and merge review (P4);
- reviewer correction commands (`/entities/{id}/corrections`), supersession by correction, merge and split (P8);
- ontology simulation on sample episodes and re-ingest to a staging namespace (P8, Flow F);
- memory (P5); context profiles and memory policies are parsed and stored, not evaluated (P5/P6);
- agent scope ACLs (P7).

---

## 4. Package Layout and Boundaries

```text
packages/
├── ontology/   kg_ontology    # NEW: pure, no DB/graph imports
│   ├── spec.py         # OntologySpec Pydantic model (+ YAML loader/normaliser, canonical JSON, content hash)
│   ├── validate.py     # structural + semantic validation -> ValidationReport (errors with JSON paths)
│   ├── diff.py         # spec diff + breaking-change classification
│   ├── rules/catalog.py     # RuleKind registry: params model, stage, family, conflict policy doc, evaluator
│   ├── rules/evaluators/*.py  # one module per family (structural, validation, cardinality, temporal, authority, review)
│   ├── rules/context.py     # RuleContext (pre-fetched facts/entities supplied by the caller -> evaluators stay pure)
│   └── ports.py        # GraphCompilerPort protocol (compile check injected by the composition root)
├── knowledge/  kg_knowledge   # NEW: commands, entities, facts, projection orchestration
│   ├── commands.py     # CommandPipeline (blueprint evaluation order), command models
│   ├── store.py        # repositories: ontology versions, entities, facts, commands, review items, idempotency
│   ├── projection.py   # state-based projector: Postgres row -> ScopedGraph upsert (idempotent, order-independent)
│   ├── reads.py        # entity/fact reads with as_of / known_at / history
│   └── jobs.py         # GraphProjectionJob(JobHandler, stage="graph_projection") + sweeper loop
├── graph/      kg_graph       # EXTENDED
│   ├── compile.py      # OntologySpec -> Graphiti Pydantic entity/edge types + edge_type_map (from the spike compiler); implements GraphCompilerPort
│   └── scoped.py       # + upsert_entity_node, upsert_fact_edge, get_fact_edge, delete_* (not used yet), fact text rendering is NOT here
```

**import-linter additions:**
- `kg_ontology` → `kg_common`, `kg_domain` only (no `kg_db`, `kg_graph`, `graphiti_core`, `sqlalchemy`).
- `kg_knowledge` → `kg_ontology`, `kg_graph`, `kg_modelgw` (egress), `kg_db`, `kg_governance` (audit), `kg_domain`, `kg_common`, `kg_obs`.
- `kg_graph` → may import `kg_ontology` (to compile types).
- `kg_api`, `kg_worker` and `kg_cli` wire the `GraphCompilerPort` implementation into the publish service.

---

## 5. Domain Model

### 5.1 Ontology spec (canonical form)

The input is the blueprint YAML (`06-reference/02` §A) or the JSON from the designer. Both normalise to `OntologySpec`:

```yaml
ontology: {key: personal-knowledge, version: 1.0.0, mode: strict}        # mode: strict | permissive (permissive => unknown attributes kept, warned)
entity_types:
  Person:
    description: A human being
    identity:
      key: platform_uuid                      # platform_uuid | external_id
      external_id_systems: [hr]               # strong IDs that deterministically match (KGR-02: names never do)
      aliases_from: [name]                    # 'name' = node name (M1)
    allowed_classifications: [internal, confidential, restricted]
    attributes:
      name:       {type: string, required: true}
      birth_date: {type: date, sensitive: true, sensitivity: restricted, editable: true}
relationship_types:
  WORKS_AT:
    source: Person
    target: Organization
    cardinality: many_to_many                 # informative; enforced only via rules
    temporal: true
    fact_template: "{source} works at {target}{role?: as {role}}"   # deterministic text for the graph fact (no LLM)
    attributes: {role: {type: string}}
    rules:
      - require_source                        # shorthand => {kind: require_source}
      - preserve_valid_time
      - {kind: succession, params: {match: [source, target]}}        # new WORKS_AT for same person+org ends the previous one
rules: []                                     # workspace-wide rules (optional)
context_profiles: {...}                       # stored + schema-checked, evaluated in P6
memory_policies: {...}                        # stored + schema-checked, evaluated in P5
```

- **Attribute types:** `string | integer | number | boolean | date | datetime`. Anything else is rejected (ADR-014).
- **Attribute flags:** `required`, `default`, `editable` (false means set only at create), `sensitive` / `sensitivity` (§5.5), `description`, and constraints `pattern`, `min`, `max`, `max_length`.
- **Canonical JSON:** keys sorted, shorthand expanded, defaults filled. **`content_hash = sha256(canonical_json)`**. Two YAMLs with the same meaning get the same hash.

### 5.2 Rule catalog (fixed; `kg_ontology.rules.catalog`)

Each `RuleKind` has the same parts:
- `kind`, `family`, `stage` (from the blueprint evaluation order), `applies_to` (entity | relationship | workspace);
- a params Pydantic model;
- a default `severity` (`error | warning`) and `action` (`reject | queue_review | warn`), both overridable;
- `conflict_policy` (documentation text);
- an evaluator `evaluate(cmd, ctx) -> list[Violation]`, which is pure and deterministic;
- at least 2 fixture cases (one pass, one fail), checked by a meta-test.

| Family | Kind | Params | Behaviour |
|---|---|---|---|
| structural | `strict_types` (implicit) | — | Unknown entity or relationship type → reject. Unknown attribute → reject (`strict`) or warn (`permissive`) |
| structural | `endpoint_types` (implicit) | — | Source and target entity types must match the relationship declaration |
| validation | `attribute_types` (implicit) | — | Scalar type, `required`, `pattern`, `min`/`max`, `max_length`. Validity is not truth: this checks form only |
| validation | `valid_interval` (implicit on temporal) | — | `valid_from <= valid_to`; dates are not before 1900 |
| cardinality | `max_active_per_source` | `n`, `match?` | For example, one active `PRIMARY_EMPLOYER` per Person at any valid time. Overlapping active facts beyond `n` → action |
| cardinality | `unique_active_pair` | — | No two active facts of this type for the same (source, target) with overlapping validity, unless `succession` resolves it |
| temporal | `preserve_valid_time` | `require_valid_from: bool=true` | Temporal relationships need `valid_from` (or an explicit `valid_from_unknown: true`). The platform **never defaults to now** |
| temporal | `succession` | `match: [source] \| [source,target]` | A newly asserted fact whose `valid_from` falls inside an existing matching fact's open interval **ends** the predecessor at the new `valid_from` (historical succession). It does **not** supersede it. A new fact *before* an existing one → reject (`out_of_order_succession`) unless it fits a gap |
| authority | `require_source` | — | The command must carry `source_system` and `source_ref` |
| authority | `min_authority` | `level: authoritative_system \| admin_asserted \| user_asserted` | The command's authority class must be at or above the level |
| review | `require_review_if_inferred` | — | Stored and evaluated for `source_type=inferred` (P4). Commands are never inferred, so it passes in P2, with fixture tests for both cases |
| review | `queue_if` | `when: disallowed_endpoint \| cardinality_violation` | Turns a violation of the named implicit rule into `queue_review` instead of `reject` (ONT-04 "rejected **or queued** based on configured rule") |

**Ontology-level rule tests (blueprint "Rule … test cases"):** any rule entry may carry `tests: [{given: {...}, command: {...}, expect: accept|reject|queue_review}]`. Publish runs them and fails on any mismatch.

### 5.3 Commands and the evaluation pipeline

**Command types (P2):**
- `entity.create`
- `entity.update` (attributes, aliases, external IDs; needs `If-Match: revision`)
- `relationship.assert`
- `relationship.end` (sets `valid_to`)

`CommandPipeline.run(principal, cmd)` follows the blueprint order. Each stage's outcome is recorded in `commands.decision` as `[{stage, rule_id?, outcome, code, path?}]`:

1. **authenticate / resolve workspace** (P1 dependencies);
2. **load the active published ontology** (none → `409 no_active_ontology`);
3. **schema validation** (implicit structural and validation rules);
4. **source classification**:
   - authority class comes from the principal: `app_writer` app → `authoritative_system`, `workspace_admin` user → `admin_asserted`;
   - a request may *lower* its authority, never raise it;
   - the classification must be one of the type's `allowed_classifications` (default `internal`);
5. **identity resolution (deterministic only)**:
   - `entity.create` with a matching `(type, external_id_system, value)` → returns the existing entity (`matched_existing`);
   - relationship endpoints are given by `entity_id` or `{type, external_id}`; not found → `422 unknown_entity`;
   - names are **never** matched (KGR-02);
6. **factual conflicts / temporal rules**: cardinality, succession, valid interval. The `RuleContext` is pre-fetched with `SELECT … FOR UPDATE` on the source entity row, so commands on the same entity are serialised;
7. **authorization for mutation**: `entity.write` plus `min_authority` rules;
8. **decide**:
   - any `reject` → **422 `rule_violation`**, and the command is stored with `status=rejected`;
   - any `queue_review` → **202**, `status=needs_review`, plus a `review_items` row; nothing is written to entities or facts;
   - otherwise apply;
9. **persist and audit**: entities, facts and the command in one transaction, plus an audit row. Then projection (§5.4).

### 5.4 State-based projection (Postgres → Neo4j)

- The projector makes the graph **equal the current Postgres row**. It does not replay deltas, so it is idempotent and independent of order. The node UUID is the `entity.id` and the edge UUID is the `fact.id`.
- **Entity node:**
  - `EntityNode(uuid, name=display_name, group_id, labels=["Entity", <type>], attributes={non-sensitive attributes, platform_entity_id, ontology_version}, created_at=entity.recorded_at, summary="")`;
  - then `generate_name_embedding` → `nodes.entity.save`.
- **Fact edge:**
  - `EntityEdge(uuid, group_id, source_node_uuid, target_node_uuid, name=<REL_TYPE>, fact=<rendered fact_template>, episodes=[], valid_at, invalid_at=valid_to, created_at=recorded_at, expired_at=superseded_at, attributes={non-sensitive attributes, platform_fact_id, source_command_id, authority_class})`;
  - then `generate_embedding` → `edges.entity.save`.
- **Projection is attempted in the request after commit**, with a 5 s budget:
  - on success → `projection_status=projected`, and the API returns **201**;
  - on timeout or a retryable error → `pending`, the API returns **202** with a status URL, and the sweeper retries;
  - on a non-retryable error → `projection_failed` with `last_error_code`, an operator metric, and a retry from the CLI.

  Postgres reads are authoritative and immediately consistent either way. Responses expose `projection_status`.
- **Egress (M14):**
  - classification = `max(entity.classification, fact.classification)`;
  - `local_only` → save **without embeddings** (`embedding_status=skipped_by_policy`), which WP0 verifies;
  - a router error → retryable per the P1 categories.
- **Drift check:** `kg graph reconcile --workspace <slug>` compares every Postgres entity and fact with its node or edge (strips M7) and reports or repairs drift.

### 5.5 Sensitive attributes (data minimisation)

Attributes marked `sensitive: true` (e.g. `birth_date`, `sensitivity: restricted`):
- are stored in Postgres only;
- are **never** written to Neo4j, the fact text, embeddings or logs;
- are returned only to principals with `entity.read_sensitive` (workspace_admin). Others see the key with `"redacted": true`.

This keeps restricted data out of the graph, which P4 and P6 retrieval read.

### 5.6 Time semantics for command facts (ADR-017 applied)

| Column | Meaning | Set by |
|---|---|---|
| `valid_from` / `valid_to` | When the fact is true in the world | Command (`valid_to` also set by `succession` or `relationship.end`) |
| `source_recorded_at` | When the **source system** learned it (evt_002: 2024-06-10) | Command (optional; KGR-04 backdating) |
| `recorded_at` | When **this platform** recorded it | `now()` in the DB (M12) |
| `superseded_at` | When the platform stopped believing it current (correction) | P8. Null in P2 |
| `status` | `active \| superseded \| disputed \| deleted` (record status; `proposed` and `rejected` come from P4) | Pipeline |

- **Succession ≠ supersession.** An ended employment is still a *true historical fact* (`status=active`, `valid_to` set). It is not superseded.
- Reads:
  - `as_of(T)` = `valid_from <= T AND (valid_to IS NULL OR valid_to > T)`;
  - `known_at(T)` = `recorded_at <= T AND (superseded_at IS NULL OR superseded_at > T)`;
  - the default is "current" = `as_of(today) AND known_at(now) AND status='active'`.

---

## 6. Data, API, Authorization

### 6.1 Migration `0002_ontology_and_commands` (`down_revision="0001"`)

Follow the 0001 conventions:
- every tenant table has `workspace_id` and composite FKs `(workspace_id, x_id)`;
- RLS `ENABLE` + `FORCE` with the `tenant_isolation` policy on `public.current_workspace_id()`;
- explicit column-level grants to `kg_app`;
- timestamps default to `now()`.

| Table | Key columns | Integrity / grants |
|---|---|---|
| `ontology_versions` | `id`, `workspace_id`, `ontology_key`, `version` (semver), `state` (`draft\|published\|retired`), `spec jsonb` (canonical), `source_text` (original YAML, optional), `content_hash`, `revision` (draft edits), `base_version_id`, `validation_report jsonb`, `notes`, `created_by`, `created_at`, `published_by`, `published_at`, `retired_at` | UNIQUE `(workspace_id, ontology_key, version)`. **Trigger:** when `OLD.state <> 'draft'`, any change to `spec, source_text, content_hash, version, ontology_key` raises. The only allowed transitions are `draft→published`, `published→retired`, `retired→published`(no). Drafts may be deleted; published may not (no DELETE grant beyond drafts, enforced by trigger) |
| `ontology_activations` | `id`, `workspace_id`, `version_id`, `previous_version_id`, `activated_by`, `activated_at`, `reason_code` | Append-only (same trigger pattern as `audit_log`) |
| `workspace_ontology` | `workspace_id` PK, `active_version_id` (must be `published`, checked by trigger), `updated_at` | One active ontology per workspace (P2 decision) |
| `entities` | `id`, `workspace_id`, `type_key`, `display_name`, `aliases text[]`, `attributes jsonb`, `classification`, `status` (`active\|deleted`), `revision`, `ontology_version_id`, `created_by_command_id`, `recorded_at`, `updated_at`, `projection_status` (`pending\|projected\|failed`), `embedding_status`, `projected_at` | Index `(workspace_id, type_key, display_name)`; trigram index on `lower(display_name)` for directory search (`pg_trgm`, created by the owner) |
| `entity_external_ids` | `workspace_id`, `entity_id`, `type_key`, `system`, `value` | UNIQUE `(workspace_id, type_key, system, value)`: deterministic identity (KGR-01/02) |
| `facts` | `id`, `workspace_id`, `rel_type`, `source_entity_id`, `target_entity_id`, `attributes jsonb`, `valid_from`, `valid_from_unknown bool`, `valid_to`, `source_recorded_at`, `recorded_at`, `superseded_at`, `succeeded_by_fact_id`, `status`, `authority_class`, `classification`, `source_type` (`command` in P2; `extracted\|inferred` in P4), `source_command_id`, `source_system`, `source_ref`, `ontology_version_id`, `revision`, `projection_status`, `embedding_status`, `projected_at` | CHECK `valid_to IS NULL OR valid_from IS NULL OR valid_from <= valid_to`. Indexes `(workspace_id, source_entity_id, rel_type, status)` and `(workspace_id, target_entity_id, rel_type)` |
| `commands` | `id` (uuid7), `workspace_id`, `idempotency_key`, `request_hash`, `command_type`, `actor_principal_id`, `authority_class`, `classification`, `source_system`, `source_ref`, `occurred_at`, `ontology_version_id`, `payload jsonb`, `status` (`applied\|rejected\|needs_review`), `decision jsonb`, `result jsonb` (IDs), `http_status`, `projection_status`, `projection_attempts`, `last_error_code`, `created_at`, `projected_at` | UNIQUE `(workspace_id, idempotency_key)`. `kg_app` may UPDATE only `projection_*`, `last_error_code`, `projected_at` (column grants). No DELETE |
| `review_items` | `id`, `workspace_id`, `item_type` (`policy_violation` in P2), `status` (`open\|resolved\|dismissed`), `command_id`, `rule_kind`, `reason_code`, `created_at`, `resolved_by`, `resolved_at`, `resolution_code` | Minimal; P4 extends it (identity_match, contradiction, memory_proposal) |
| `idempotency_records` | `workspace_id`, `key`, `route`, `request_hash`, `status_code`, `response jsonb`, `created_at`, `expires_at` | PK `(workspace_id, key)`. Generic store for non-command mutations (ontology publish, activate, draft create). Expiry 24 h, judged by the DB clock |

**Functions:**
- `ops.workspaces_with_pending_projection(limit int) RETURNS SETOF uuid`: `SECURITY DEFINER`, owner `kg_owner`, pinned `search_path`, `EXECUTE` granted to `kg_app` (M9).
- `CREATE EXTENSION IF NOT EXISTS pg_trgm`.

**Downgrade:** drops everything in 0002.

### 6.2 Endpoints

Every mutation needs an **`Idempotency-Key`**. Mutable resources return an **ETag** (`W/"rev-N"`), and edits need `If-Match`.

| Method | Path | Action | Behaviour |
|---|---|---|---|
| GET | `/v1/ontologies/versions` | `ontology.read` | List versions (state, version, hash, active flag); cursor pagination |
| POST | `/v1/ontologies/versions` | `ontology.write` | Create a draft from `{format: yaml\|json, content}` or `{copy_of: version_id}`. The response includes the validation report |
| GET | `/v1/ontologies/versions/{id}` | `ontology.read` | Spec, report, hash, state; ETag |
| PUT | `/v1/ontologies/versions/{id}` | `ontology.write` | Replace the draft content (`If-Match`). A non-draft → **409 `immutable_version`** |
| DELETE | `/v1/ontologies/versions/{id}` | `ontology.write` | Drafts only |
| POST | `/v1/ontologies/versions/{id}/validate` | `ontology.read` | `ValidationReport{errors[], warnings[]}`, each item `{code, path, message}`. Includes the compile check (reserved names) and the rule-test results (ONT-01, ONT-06) |
| GET | `/v1/ontologies/versions/{id}/diff?against=active\|<id>` | `ontology.read` | Added, removed and changed types, attributes and rules, each `breaking: bool` with a reason; plus **impact counts** (entities and facts per affected type) (ONT-05 partial) |
| POST | `/v1/ontologies/versions/{id}/publish` | `ontology.publish` | `{notes, activate: true}`. Re-validates and compiles, then freezes the version (`state=published`, hash), records an activation if `activate`, and audits. Refused if the report has errors or a rule test fails |
| GET | `/v1/ontologies/active` | `ontology.read` | Active version plus the compiled summary (type list, relationship map) |
| POST | `/v1/ontologies/active` | `ontology.publish` | `{version_id, reason_code}`: activate an older published version (**rollback**, ONT-02). Appends an activation; nothing is mutated |
| POST | `/v1/ontologies/versions/{id}/retire` | `ontology.publish` | Published and not active → retired |
| GET | `/v1/ontologies/rule-catalog` | `ontology.read` | Kinds, families, param JSON schemas, defaults and conflict policies. Drives the designer forms |
| POST | `/v1/entities` | `entity.write` | `entity.create`. **201** (projected) / **202** (pending) / **200** (`matched_existing`) / **422** `rule_violation` / **202** `needs_review` |
| GET | `/v1/entities` | `entity.read` | Directory: `?type=&q=&status=&cursor=` |
| GET | `/v1/entities/{id}` | `entity.read` | Entity, current relationships and identity. `?include=history&as_of=&known_at=`; sensitive attributes redacted (§5.5); ETag |
| PATCH | `/v1/entities/{id}` | `entity.write` | `entity.update` (`If-Match`) |
| GET | `/v1/entities/{id}/relationships` | `entity.read` | `?direction=out\|in\|both&type=&as_of=&known_at=&include_history=` |
| POST | `/v1/relationships` | `entity.write` | `relationship.assert`, same status codes as entity create |
| POST | `/v1/relationships/{fact_id}/end` | `entity.write` | `relationship.end` `{valid_to, source_system, source_ref, source_recorded_at?}` |
| GET | `/v1/relationships/{fact_id}` | `entity.read` | Fact with validity, record times, status, authority and provenance (`source_command_id`, source system and ref) (KGR-05) |
| GET | `/v1/commands` | `command.read` | `?status=&type=&entity_id=&cursor=` |
| GET | `/v1/commands/{id}` | `command.read` | Command, decision trace, result IDs, projection status (the status URL for 202s) |

**New error codes:** `rule_violation` (422, `details.violations[]` = `{rule_kind, rule_id?, code, path}` with no attribute values echoed), `immutable_version` (409), `no_active_ontology` (409), `idempotency_mismatch` (409, same key with a different body), `precondition_failed` (412), `precondition_required` (428, a missing `If-Match` on an edit), `unknown_entity` (422).

**Foreign IDs** on every path → `404` identical to a random UUID (P1 rule, extended to every new route by T-SEC-01's route walker).

### 6.3 Authorization matrix additions (explicit; M13)

| Action | workspace_admin | operator | reviewer | app_writer | agent | reader |
|---|---|---|---|---|---|---|
| `ontology.read` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `ontology.write` (drafts) | ✓ | | | | | |
| `ontology.publish` | ✓ | | | | | |
| `entity.read` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `entity.read_sensitive` | ✓ | | | | | |
| `entity.write` (commands) | ✓ | | | ✓ | | |
| `command.read` | ✓ | ✓ | ✓ | ✓ | | |

Agents read entities but never write commands: agents propose through interactions and memories (P3/P7). P7 narrows reads further with scope ACLs.

### 6.4 Audit actions (closed metadata)

The new actions are:
- `ontology.draft_created|updated|deleted|published|activated|retired`
- `command.applied|rejected|queued`
- `projection.failed|repaired`

Extend `AuditMetadata` with closed fields only: `version_id`, `content_hash` (hex pattern), `command_type` (enum), `rule_kind` (enum), `violation_count`. No names or attribute values.

### 6.5 Metrics (no identifiers in labels; P1 audit test covers them)

- `ontology_publish_total{outcome}`
- `commands_total{command_type,outcome}`
- `rule_violations_total{rule_kind,action}`
- `projection_total{kind,outcome}`
- `projection_pending` (gauge)
- `projection_lag_seconds` (histogram)

---

## 7. Work Packages

### WP0: Projection compatibility checks (half a day, before WP4)
These are integration tests against the P1 Neo4j with the in-process mock router, to confirm the assumptions the projector depends on (graphiti-core 0.30.2):
- **PJ-01:** `EntityNode` with custom labels `["Entity","Person"]` and attributes holding `date`/`datetime` values. What is stored, and does it round-trip? (Expect ISO strings; decide on serialisation.)
- **PJ-02:** saving a node or edge with `name_embedding` / `fact_embedding` = None (the `local_only` path). Does `save` succeed, and do Graphiti searches tolerate it?
- **PJ-03:** an edge with `episodes=[]` saves and reads back; `invalid_at` / `expired_at` round-trip exactly.
- **PJ-04:** re-saving the same UUID updates in place (an idempotent upsert) and does not duplicate.
- **PJ-05:** the spy confirms 0 chat calls and N embedding calls per save.

**Output:** findings recorded in ADR-021. If PJ-02 fails, `local_only` entities are stored Postgres-only with `projection_status=skipped_by_policy`, and the ADR records that.

### WP1: `kg_ontology` spec, loader, canonical hash
- `OntologySpec` and sub-models with `extra="forbid"`, identifier validators (M3), and the six attribute types.
- YAML and JSON loader: shorthand expansion, defaults, `name` handling (M1), canonical JSON, `content_hash`.
- `examples/ontologies/personal-knowledge.yaml`: the blueprint §A YAML plus `identity.external_id_systems: [hr]`, `fact_template`s and `succession` on WORKS_AT. The spike YAML is the starting point.
- **Tests:** round-trip, hash stability (key order and shorthand variants → same hash), every invalid-identifier case, unsupported type rejection.

### WP2: Rule catalog and evaluators
- `RuleKind` registry, params models, the evaluators for §5.2, `RuleContext`, `Violation`.
- Fixture files: `packages/ontology/tests/fixtures/rules/<kind>.yaml`.
- **Meta-test:** every kind has ≥1 pass and ≥1 fail fixture and a non-empty `conflict_policy`.
- Ontology-level `tests:` runner.
- **Tests (ONT-06):**
  - every fixture;
  - succession ending the predecessor at the new `valid_from`;
  - an out-of-order succession is rejected;
  - `max_active_per_source` with overlapping and non-overlapping intervals;
  - `queue_if` converting a reject into queue_review;
  - property tests (hypothesis is optional; if used, pin it in `p2_additions`) that evaluators are pure (same input → same output) and never call I/O (no event loop, no network).

### WP3: Validator, diff, compile check
- `validate(spec, compile_port) -> ValidationReport`:
  - structural checks;
  - endpoints exist;
  - rule params valid for the kind and applicable to the target;
  - `fact_template` placeholders refer to known or attribute names;
  - **the compile check through `GraphCompilerPort`** (reserved-name collisions reported at the field path);
  - rule `tests:` executed.
- `kg_graph.compile`: the spike compiler promoted, typed and tested. It implements the port and keeps a cache of compiled types keyed by `content_hash` (used by P4).
- `diff(a, b)`: structural diff plus breaking classification:
  - **breaking:** type removed, attribute removed, type changed, `required` added without a default, cardinality tightened, `strict` toggled on;
  - **non-breaking:** additions, descriptions.

  Impact counts are added by `kg_knowledge` (a DB count per affected type).
- **Tests:**
  - ONT-01 error paths (missing endpoint, bad identifier, reserved-name collision reported at `entity_types.Person.attributes.labels`, bad rule params);
  - diff classification table;
  - compile output accepted by Graphiti's `validate_entity_types`, ported from GR-03.

### WP4: Migration 0002 and repositories
- The SQL from §6.1, following the 0001 style (one migration file, SQL strings, explicit grants, downgrade).
- `kg_knowledge.store` repositories: every query carries an explicit `workspace_id` predicate (the P1 primary control).
- `IdempotencyStore`: an `Idempotent` FastAPI dependency stores and replays `(status, body)` in the **same** tenant transaction as the mutation, or returns 409 `idempotency_mismatch`.
- **Tests (integration):**
  - published-version immutability trigger (UPDATE of `spec` raises, even as `kg_owner`);
  - activation append-only;
  - `workspace_ontology` refuses a non-published version;
  - `commands` column grants (`kg_app` cannot UPDATE `payload`);
  - RLS on every new table (cross-workspace SELECT, INSERT and UPDATE blocked);
  - the `ops` function returns only IDs;
  - migration up/down/up;
  - **T-DB-02 (pooled reuse) extended** to the new tables.

### WP5: Ontology service and API
- `OntologyService`: create, update and delete a draft; validate; diff (with impact counts); publish (validate → compile → run rule tests → freeze → optional activation → audit); activate (rollback); retire; rule catalog. Publish and activate take a row lock on `workspace_ontology` to serialise them.
- Router `ontology.py` added to `ALL_ROUTERS` (M10). Every handler is decorated (§6.3).
- **Tests:**
  - ONT-01: create an invalid draft → report with paths; fix → clean;
  - ONT-02: publish → immutable (PUT → 409), hash and audit present; rollback → pointer moves, both versions unchanged, the activation history has 2 rows;
  - publish with errors → 422;
  - a failing rule test blocks publish;
  - idempotent publish replay;
  - ETag/If-Match flow (412, 428);
  - cross-workspace 404s.

### WP6: Command pipeline, reads and API
- `CommandPipeline` (§5.3) with decision recording; authority derivation; deterministic identity by external ID.
- Transactions:
  - lock the source entity row (`FOR UPDATE`) during the relationship evaluation;
  - entity, fact and command rows plus audit in one transaction;
  - idempotency on `commands.idempotency_key`.
- Reads (§5.6) including `as_of` and `known_at`; sensitive redaction (§5.5).
- Routers `entities.py`, `relationships.py`, `commands.py` added to `ALL_ROUTERS`.
- **Tests:**
  - evt_001 / evt_002 → the predecessor ends at 2024-06-01, both stay `active`, `source_recorded_at` = 2024-06-10, `recorded_at` = DB now (KGR-04);
  - `as_of=2023-01-01` → Software Engineer; current → Engineering Manager (KGR-03);
  - a WORKS_AT Person→Topic → 422 `rule_violation` with no values echoed (ONT-04 reject);
  - a relationship type configured with `queue_if: disallowed_endpoint` → 202 `needs_review` plus a review item, with no fact written (ONT-04 queue);
  - the same key → identical replay; same key with a different body → 409;
  - an entity re-created with the same `hr` external ID → 200 `matched_existing` (KGR-01);
  - two "Sarah Johnson" entities without external IDs → two entities, never merged (KGR-02);
  - every fact response has `source_command_id` (KGR-05);
  - agents → 403 on writes, 200 on reads;
  - `birth_date` redacted for an app_writer and visible to an admin;
  - concurrent successions on the same person are serialised (two parallel requests → a consistent chain);
  - cross-workspace 404 for entity, fact and command IDs, including a reference to another workspace's entity inside a relationship body → 422 `unknown_entity` (no oracle).

### WP7: Projection
- `ScopedGraph` additions in `kg_graph`: `upsert_entity_node(...)`, `upsert_fact_edge(...)`, `get_fact_edge(uuid)` (group-checked), `read_back(uuid)` (strips M7). No `group_id` parameter anywhere (M8). A node or edge UUID belonging to another group → `NotFound`.
- `kg_knowledge.projection.Projector`: renders node and edge from Postgres rows (template rendering is deterministic and sensitive attributes are excluded). It runs inside `egress(classification, purpose="graph_projection", workspace_id, correlation_id)`. `local_only` follows the WP0 outcome.
- `GraphProjectionJob(JobHandler)` with `stage="graph_projection"`, registered in `kg_worker`. A **sweeper loop** (setting `PROJECTION_SWEEPER_ENABLED=true` by default in P2):
  - every 10 s it calls `ops.workspaces_with_pending_projection`;
  - for each workspace it claims up to N pending rows with `FOR UPDATE SKIP LOCKED` and projects them;
  - it retries with backoff and caps attempts at 8, then `failed` plus a metric.

  P3 replaces the sweeper with outbox jobs (M16).
- `compat/model-purposes.yaml`: add `graph_projection` (embedding-only), with a test in `kg_modelgw.config`.
- CLI: `kg graph reconcile --workspace <slug> [--repair]` and `kg commands retry-projection <command_id>`.
- **Tests:**
  - **the zero-chat gate:** a spy on `RouterTransport`'s inner transport across the whole demo command sequence counts chat calls = 0 and embedding calls > 0; `model_calls` rows for purpose `graph_projection` are all `operation=embeddings`;
  - graph equals Postgres after the sequence (reconcile reports zero drift);
  - Neo4j down → API 202 `pending`, then the sweeper projects after recovery (ING-02 analogue);
  - re-projection is idempotent (run twice, same graph);
  - a `local_only` entity → no router call, with the PJ-02 outcome;
  - cross-group UUID → `NotFound`;
  - restricted attributes absent from Neo4j (a Cypher scan for the sentinel value).

### WP8: Admin console
- **Ontology** (new nav item) with sub-tabs:
  - **Types** (entity type list → detail form: key, description, identity strategy and external ID systems, allowed classifications, attributes table with type, required, sensitive, constraints and description);
  - **Relationships & Rules** (relationship list → source and target pickers, temporal toggle, fact template, attributes; rules list with forms generated from `/rule-catalog` param schemas, plus rule tests);
  - **Versions** (list with state badges and the active marker → diff view against active, with breaking flags and impact counts; Publish dialog requiring notes and showing the diff summary; Activate dialog for rollback with a reason).
  - A **YAML tab** (read and edit for power users; it saves through the same PUT).
  - Validation errors are anchored to form fields by JSON path.
  - Read-only mode for published versions.
- **Entity Directory** (new nav item):
  - search by type and name;
  - a list with projection status;
  - detail: identity (external IDs), attributes (sensitive values shown as "redacted" unless allowed), current relationships, a **history toggle** showing valid-from/to, recorded-at and source-recorded-at, and status badges (current, historical, ended);
  - provenance links to commands.

  A **Commands** tab lists commands with status (applied, rejected, needs_review, projection pending or failed) and a detail drawer with the decision trace (stage, rule, code, no values).
- **Rules:**
  - the same deny, empty, loading and error states as P1;
  - deep links `/ontology/versions/:id`, `/entities/:id`, `/commands/:id`;
  - foreign IDs show the same "Not found" state;
  - all data through the regenerated OpenAPI client (`make openapi`).
- **Tests:**
  - Vitest for the form ↔ spec mapping, error anchoring and the redaction display;
  - **Playwright ADM-01:** `admin@demo` creates a draft from the template, adds `Person.nickname` through the form, validates, publishes, and sees v1.1.0 active; `reviewer@demo` sees the Ontology read-only (no Publish button); `admin@other` sees no demo versions or entities.

### WP9: Seed, demo, docs
- `kg seed demo --ontology examples/ontologies/personal-knowledge.yaml` (optional flag): publishes v1.0.0 for `demo` only.
- `make demo-p2` → `scripts/demo_p2.py`. It is re-runnable: it skips steps already done and checks the end state. It needs a fresh `make reset up seed` for the full first-run output. Steps:
  1. Draft from the YAML with a deliberate error → validation errors with paths (ONT-01).
  2. Fix → publish v1.0.0 → PUT on it gives 409; hash and audit present (ONT-02).
  3. `conversation_app` creates Sarah (`hr:E-1001`), Acme (`hr:ORG-1`), Michael and Entrepreneurship → 201s; a repeat → 200 `matched_existing`.
  4. `evt_001`: WORKS_AT Sarah→Acme, role Software Engineer, `valid_from 2022-01-10`, source `hr/evt_001`.
  5. `evt_002`: WORKS_AT, role Engineering Manager, `valid_from 2024-06-01`, `source_recorded_at 2024-06-10`, source `hr/evt_002` → the predecessor ends 2024-06-01.
  6. Current: EM at Acme. `as_of=2023-01-01`: SE. History shows both, with valid and record times (KGR-03/04).
  7. WORKS_AT Sarah→Entrepreneurship → 422 `rule_violation` (ONT-04).
  8. Idempotent replay; key reuse with a different body → 409.
  9. The graph has 4 nodes and 2 WORKS_AT edges with the expected `valid_at`/`invalid_at`/`created_at`; reconcile shows 0 drift.
  10. `model_calls` for this run: chat = 0, embeddings > 0 (**gate**).
  11. Publish v1.1.0 (adds `Person.nickname`), then activate v1.0.0 → rollback works; commands record their version.
  12. `admin@other`: demo entity, fact, command and version IDs → 404; lists are empty.
  13. `agent_planning`: POST /v1/entities → 403; GET Sarah → 200 with `birth_date` redacted.
- **Docs:**
  - `docs/architecture/p2-ontology.md` (spec, rule catalog, pipeline sequence diagram, projection, time semantics, deviations);
  - **ADR-021** command ledger and state-based projection (with the WP0 findings);
  - **ADR-022** ontology spec and rule catalog;
  - **ADR-023** succession vs supersession and source vs platform record time;
  - runbook `docs/runbooks/projection-repair.md`.

---

## 8. Sequencing

```
WP0 ─┐
WP1 ─► WP2 ─► WP3 ─┐
       WP4 ────────┼─► WP5 ─┐
                   └─► WP6 ─┼─► WP7 ─► WP9
                            └─► WP8 (needs WP5/WP6 OpenAPI)
```

**Parallel tracks:**
- **A:** WP1 → WP2 → WP3 (pure, no DB).
- **B:** WP0, then WP4 → WP6.
- **C:** WP8 once the OpenAPI for WP5/WP6 is exported.

## 9. Test and Gate Mapping

| Gate / requirement | Evidence |
|---|---|
| ONT-01 draft, edit, validation errors | WP3, WP5 tests; Playwright ADM-01; demo step 1 |
| ONT-02 immutable publish, audit, hash, rollback without mutation | Trigger tests (WP4), WP5 tests, demo steps 2 and 11 |
| ONT-03 (partial) version recorded per job | `commands.ontology_version_id`, `facts.ontology_version_id`; the projection log line carries `ontology_version` (WP7 test) |
| ONT-04 disallowed link rejected **or** queued | WP6 reject and queue tests; demo step 7 |
| ONT-05 (partial) migration preview | Diff + breaking flags + impact counts (WP3/WP5) |
| ONT-06 rule fixtures + conflict policy | Catalog meta-test, fixtures, ontology-level `tests:` (WP2) |
| Zero LLM for deterministic writes (ADR-007/016) | Spy and `model_calls` assertion (WP7); demo step 10 |
| KGR-01/02/03/04/05 (command subset) | WP6 tests; demo steps 3–6 |
| Isolation (SEC-01 continued) | Route walker over all new routes; RLS on new tables; reuse test extended; graph cross-group `NotFound` |
| Deny by default / no widening | T-REG (via `ALL_ROUTERS`); `test_policy` matrix updated explicitly |
| No content in telemetry | P1 redaction and label audits plus a sentinel test: a sentinel attribute value and display name never appear in logs, metrics, `audit_log` or `model_calls` |
| Data minimisation | Sensitive attributes absent from Neo4j and embeddings (WP7) |

## 10. Exit Criteria

- [ ] `make reset up seed demo-p2` passes all 13 steps, and re-running `make demo-p2` passes without a reset.
- [ ] `make check` is green: lint (including new import-linter contracts), mypy strict, unit, integration, security, contract, Vitest, Playwright (P1 smoke + ADM-01), OpenAPI drift.
- [ ] The P1 demo (`make demo-p1`) still passes.
- [ ] ADR-021/022/023 and `docs/architecture/p2-ontology.md` are written. `compat/model-purposes.yaml` has `graph_projection`. `versions.lock.yaml` has `p2_additions` (e.g. `pg_trgm` is an extension, not a package; list any new Python or JS packages).

## 11. Risks and Mitigations

| Risk | Mitigation |
|---|---|
| The rule catalog grows into a rules language | Fixed kinds with typed params only. A new kind needs code, fixtures and a conflict policy. No expressions or user code (blueprint) |
| Postgres and the graph drift | State-based idempotent projection, the sweeper, `graph reconcile`; Postgres is authoritative for reads |
| Projection blocks the API on a router or Neo4j outage | A 5 s in-request budget, then 202 and the sweeper |
| Two write paths diverge in P4 (commands vs extraction) | Both write `facts` rows (`source_type`) and use the same projector. P4 adds `extracted` facts through the same table |
| Ontology version churn breaks P4 extraction types | Compiled types are cached by `content_hash`; jobs pin `ontology_version_id` |
| Parallel P3 work conflicts | M16: merge revision, `JobHandler` seam, explicit matrix rows; coordinate `ALL_ROUTERS` and nav edits |

## 12. Rollback

- `kg db downgrade 0001` removes all P2 tables.
- `kg_ontology` and `kg_knowledge` are new packages, and the `kg_graph` additions are additive.
- The sweeper can be disabled (`PROJECTION_SWEEPER_ENABLED=false`).
- Neo4j projections can be cleared per workspace with `clear_data(driver, [group_id])` (ADR-015 §5) and rebuilt from Postgres with `kg graph reconcile --repair`.

## 13. Decisions (resolved 2026-10-11, the user accepted the recommendations)

1. **One active ontology per workspace.** Adopted for v1. Several ontologies per workspace would need a namespace on every type key.
2. **Authority mapping stays role-based:** `app_writer` → `authoritative_system`, `workspace_admin` → `admin_asserted`. A per-principal "trusted app" flag, which can only *narrow* this, is scheduled for P7 with agent and app registration.
3. **Sensitive attributes are Postgres-only.** Adopted: never in Neo4j, fact text, embeddings or telemetry.
4. **The YAML tab is a plain monospace textarea, with no code-editor dependency.** It can be revisited if users ask; only that tab would change.
5. **`local_only` data is Postgres-only** (`projection_status=skipped_by_policy`), because WP0 PJ-02 showed Graphiti cannot save without an embedding (ADR-021).
