# Administration Console: Information Architecture and Workflows

**Parent:** [Blueprint index](../README.md)  
**Intent:** Operational user interface to configure, inspect and govern the entire platform, not just view a pretty graph.

## Primary navigation

| Screen | Main objects | Minimum user operations |
|---|---|---|
| Dashboard | workspace health, storage, processing | See active workspace, ingestion outcomes, recent errors |
| Ontology Types | entities and attributes | Create drafts, edit, validate, preview schema, publish |
| Relationships & Rules | typed edges, cardinality, temporal rules | Define constraints, simulate, publish versions |
| Entity Directory | people, orgs, assets etc. | Search, inspect, correct names/attributes, request merge |
| Graph Explorer | selected entity neighborhoods | Pan/zoom/filter, inspect edge, temporal view, source links |
| Interactions & Episodes | input ledger, Graphiti episodes | View provenance, processing stages, retry authorized failures |
| Memory Explorer | working, episodic, semantic, procedural | Search, inspect evidence, approve, expire, supersede |
| Context Lab | agent, task and context profile | Test query, inspect final context/token budget and reasons |
| Review Queue | conflicts, suspected duplicates, proposals | Side-by-side review, approve/reject/correct, audit reason |
| Agents & Access | roles, principals, scopes | Register/revoke agents, control read/propose/publish rights |
| Settings | model profiles, retention, rate quotas | Configure approved providers and policies |
| Operations | workers, error queues, metrics | Monitor/replay jobs, view cost and latency |

## UI design principles

- Prioritize evidence lineage and actionability over decorative visualization.
- Show **current, historical, inferred, disputed** distinctly, including exact validity dates and what the system knew when.
- Every edit is a versioned command and may be asynchronous; display status and rollback path where possible.
- Use a three-pane graph explorer: type-aware search left, graph center, entity/fact/source detail right.
- Set caps on graph visualization breadth (hops, node count) so dense neighborhoods remain usable.
- Never expose inaccessible nodes in search suggestions, graph topology, page counts or retrieval traces.
- Consistent empty/error/loading and permission-denied states.

## Detailed page: Ontology designer

Fields: type key, description, identity strategy, attributes with primitive types/required/sensitive/default flags, allowed relationships, temporal/cardinality rules, sample objects, and publishing diff. UI actions: draft, duplicate, validate, preview on sample inputs, publish. The published version is immutable.

## Detailed page: Memory explorer

List filters: workspace, person/project, source, type, status, recent updates, sensitivity, agent origin. Detail: exact content, generated summary, source snippets (permission filtered), embedding/model metadata, confidence/authority distinction, previous revisions, graph links, retrieval history where safe. Actions: review, mark incorrect, supersede, expire, delete subject to role.

## Detailed page: Context Lab

Choose authorized test agent, subject and context profile, enter task and token budget. Show `retrieval plan`, candidates by store, filters, deduplication, final pack, actual tokens, known uncertainties, freshness and total cost/latency. **The Context Lab MUST run through the same authorization and composer path as the public API**, not an unrestricted admin-only query code path.

## Detailed page: Review Queue

Reviewer sees proposed action, claim, current conflicting belief, timestamps, provenance links and policy reason. Decision choices: approve, reject, request more evidence, link as different person, merge/split identity, mark superseded. Reviewer ID and rationale required; save as command with audit, not direct graph DB mutation.

## Admin interaction requirements

- `ADM-01` Non-developer creates and publishes a draft Person + Organization ontology and sees it used for subsequent ingestion.
- `ADM-02` Selecting a graph fact shows its original episode/event reference and current/historical status.
- `ADM-03` Reviewer can correct a disputed memory and verify context output changed.
- `ADM-04` Context Lab shows why a relevant memory was included and why another was excluded.
- `ADM-05` The console cannot circumvent workspace permissions by specifying arbitrary IDs.
- `ADM-06` Worker failures are surfaced with retry-safe controls and audit reasons.

## Admin front-end seams

Use typed API client generated from OpenAPI; no browser-side direct Neo4j access. The frontend should not embed model/API provider secrets. URL deep-links should be stable for entities, memories, review items and context traces. Document role visibility and permission states for every screen.
