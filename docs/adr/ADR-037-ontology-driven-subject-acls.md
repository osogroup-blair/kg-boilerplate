# ADR-037: Ontology-driven subject ACLs on entities and facts

**Status:** Accepted  **Decision refs:** SEC-01, MEM-03, AT-10, phase-7 plan §5.2, T3, T4, T5, decisions 1, 2  **Evidence:** `tests/security/test_fact_acl_leaks.py`, `tests/security/test_p7_isolation.py`, `packages/governance/tests/test_grants.py`, `packages/ontology/tests/test_access.py`, `eval/context` C15–C16

## Context
P2–P6 made entities and facts readable by every principal of a workspace; only memory was scoped (ADR-029). A personal assistant's graph is mostly about people, and "the agent that supports Michael can read everything about Sarah" is not acceptable once agents are registered by admins.

## Decision
1. **The active ontology declares it.** `entity_types.<T>.access: workspace | subject_scoped` (default `workspace`). It is validated, diffed and published like any other field. An unset or `workspace` value is **not serialised**, so every version published before P7 keeps its content hash. The example ontology v1.4.0 marks `Person` `subject_scoped`; rolling back is activating v1.3.0 (open access again, no migration).
2. **Tightening is a breaking change with a preview.** The diff marks `access: workspace → subject_scoped` breaking ("access tightened") and reports `access_impact`: the active entities that become scoped, the principals that lose sight of them (everyone but admins, reviewers and `subject:*` holders), and the principal×entity pairs hidden. The console shows it as a banner.
3. **`SubjectFilter`** = (scoped types of the active ontology, may-see-everything, granted subject ids). `full` is true for `workspace_admin` and `reviewer` (by role, as in P5 deviation 13) and for `subject:*` holders. It is a **required keyword argument** of every `kg_knowledge` read (entities, facts, neighbourhoods, mentions, claims, evidence, commands, graph search, the join of graph hits), of the ingestion reads and of the memory-subject loader. There is no default, so a caller that forgets it does not type-check (mypy `--strict`), and an `EXPLAIN` test proves each read path carries the predicate in its SQL.
4. **The predicate is inside the statement** (`e.type_key <> ALL(:types) OR e.id = ANY(:ids)`, bound arrays); nothing is filtered after the query runs. A fact is visible only when **both** endpoints are, so a hidden person stays fully hidden: degree counts, "N relationships" totals, neighbourhood edges and graph-search hits all exclude the fact, and a neighbourhood never steps through a hidden node.
5. **A hidden id is the same 404** as a missing or foreign one (identical body modulo request id) on every route; a walker discovers every route with an entity, fact or command id and probes it. A hidden graph-search hit is dropped and counted in `acl_hidden_total{surface}` (never in `unreconciled_hits`, which stays for edges Postgres does not know yet).
6. **What is not filtered.** Memory keeps its own scopes (a workspace-scoped memory about a hidden person is still visible by its scopes, but the person's id is omitted from its `subject_ids`). Interactions an agent did not submit are not visible to it at all; for staff roles a subject reference to a hidden entity is shown as `hidden` without its id. Reviews are admin/reviewer only (a full view by role).
7. **Merges follow the canonical entity.** `entity.merge` re-points `principal_scopes` (privileges unioned where the holder already had the canonical row), `memory_scopes` and `memory_subjects` in the same transaction. The database narrows those column grants with triggers to this one move (the old id is a merged entity whose canonical id is the new one).
8. **Candidates** of a scoped type that extraction creates are invisible to agents until granted. Conservative by design.

## Consequences
- Privacy over recall: a context about two people, one hidden, is thinner. Admins grant explicitly; the Lab (act-as) shows the target's view.
- Visibility is decided from the *active* ontology: an entity written under an older version is scoped as soon as its type is.
- Operators hold no implicit subject grants; staff who need to see people must be admins or reviewers, or be granted.
