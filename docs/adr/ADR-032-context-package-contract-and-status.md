# ADR-032: The context package contract, status semantics and the single query embedding

**Status:** Accepted for the offline build; the live thresholds are confirmed by `make eval-context-live`  **Decision refs:** CTX-01..08, RET-01..06, AT-06..AT-12  **Evidence:** `tests/integration/test_p6_context.py`, `test_p6_api.py`, `tests/security/test_context_leaks.py`, `eval/context/benchmark.yaml`

## Context
`POST /v1/context/build` is the product. An agent describes a task and receives the smallest sufficiently informative package that is authorised, grounded, temporally correct, bounded and explainable. The contract must say, without ambiguity, what is in the package, what a status means, and what happens when part of the platform is down.

## Decision
1. **One entry point.** `ContextService.build(principal, request, act_as_by=None)` is the only way a context is made. The public API and the Context Lab (ADR-035) both call it. The workspace and the identity come from the credential; a body that names `workspace_id`, `group_id` or `agent_id` is a 422 (`extra="forbid"`).
2. **The package** is `context_text` (stable headings, one cited bullet per record, inside a data envelope) plus the same content as typed arrays (`facts`, `memories`, `instructions`, `unknowns`, `conflicts`), `citations[]`, `budgets`, `unavailable_sources`, `freshness`, `source_watermarks`, `excluded_source_counts`, `versions`, `retrieval_trace_id`. The arrays are exactly the packed items (same handles), never a superset.
3. **Status** answers one question: *what did we consult?* It never answers *is it true?* (that is per item: `evidence_status`, `authority_class`).

| status | when | body |
|---|---|---|
| `complete` | every source the profile needs answered | the package |
| `partial` | a source failed, timed out, was skipped by the egress policy, or is stale beyond `freshness_requirement` | the package, `unavailable_sources[{source, reason}]` and `freshness` |
| `insufficient_evidence` | no authorised fact or memory supports an answer and no source is down | only unknowns and the data notice; nothing is invented |
| `insufficient_budget` | the required tier does not fit `max_tokens` | no items, a notice that fits, `budgets.required_tokens` = what to ask for |
| `denied` | every requested subject is invisible to the caller (another workspace's, a merged-away, a deleted, an unknown id: indistinguishable) | **nothing**: no counts, names, watermarks or trace; no row is written |

   With `allow_partial=false`, anything that would be `partial` is a `503 context_incomplete` and **no package is returned**. Postgres is required: without it the request fails (503). A request that asks for more than the profile allows is a 422 `exceeds_profile` naming the fields; a profile that does not exist is a 422 `unknown_profile`.
4. **Sources and their reasons.** `facts` (Postgres), `graph`, `memory`, `memory_vector`, `conflicts`, `watermarks`. Reasons: `timeout`, `unavailable`, `egress_policy` (the task or its subjects are `local_only`: no query embedding, no graph search, memory lexical-only, no summary call), `embedding_unavailable`, `vector_unavailable`, `stale`. A source that fails never fails the build.
5. **Critical current state is Postgres.** A subject's current facts come from the fact reads (the same valid-time and record-time rules as `GET /v1/entities/{id}/relationships`); the graph adds relevance search and every hit is joined back to Postgres. A Neo4j outage therefore degrades relevance, never correctness.
6. **The query is embedded once (CX-01).** Graphiti's `search_` embeds the query itself and cannot take a vector; the module-level `graphiti_core.search.search.search` it wraps can (`query_vector=`). `ScopedGraph.search_facts(query_vector=)` calls that function when given a vector, and `MemorySearch.run(embed=False, query_vector=)` accepts one, so the engine calls the router once per build and both halves use the same E5 `query:` vector (ADR-013: Graphiti's embedder and the gateway use the same prefix and model). If the embedding fails the graph cannot run (`embedding_unavailable`) and memory is lexical-only (`vector_unavailable`).
7. **Egress.** The query's classification is `max(request.sensitivity, the subjects' entity classifications, the highest classification among their facts)`. This replaces `GraphSearch`'s hard-coded `internal` (found while planning P6). A `local_only` task makes zero router calls.
8. **Citations.** Short handles: `F:` fact, `M:` memory, `C:` conflict, `U:` unknown, `R:` review, `E:` evidence (a source's `source_ref` when it is a safe token such as `evt_002`, else the id). `F:`/`M:`/`R:` use the **last** 8 hex digits of the id (uuid7 ids start with a timestamp, so their prefix is shared by everything written in the same minute) and grow on a collision. `citations[]` resolves each handle to an id and an endpoint whose own authorisation applies. A fact or memory without evidence is rendered `[unsupported]` (CTX-02), or dropped where the profile sets `evidence_required`.
9. **Time.** Current means true at `as_of` (default: the database clock) and known at `known_at`. A historical fact is shown only when the profile includes `history` and the task asks (an `as_of`, or words like "previous", "formerly", a year) or the profile says `always`; it is labelled and placed below the current items. A `proposed` claim is never current: it is a conflict line (`Unverified claim under review: …; <the current fact> remains current`). With an `as_of` the facts heading says so (`VERIFIED FACTS AS OF 2023-01-01`).

## Consequences
- Callers can rely on `status` for completeness and must read items for truth; the SDK (P7) exposes both.
- `denied` is deliberately uninformative; an operator debugging a denial uses the audit log and the Lab, never the response.
- Subject-level ACLs on facts do not exist until P7: `agent_support`-style principals receive workspace-readable facts and none of a subject's memories (S7).
