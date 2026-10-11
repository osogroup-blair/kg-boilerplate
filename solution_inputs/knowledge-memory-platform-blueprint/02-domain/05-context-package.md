# Agent Context Package: Contract and Invariants

**Parent:** [Blueprint index](../README.md)  
**Capability owner:** CAP-10

## Purpose

The Context Service is the product interface. An agent describes its task, subject and constraints; it receives the **smallest sufficiently informative** authorized context, grounded in source evidence. It should not need to query Neo4j, Graphiti or pgvector directly.

## Input

`POST /v1/context/build` requires `task` and an authenticated agent identity. Optional fields: `subject_ids`, `project_id`, `query`, `requested_memory_types`, `as_of`, `known_at`, `max_tokens`, `max_items`, `freshness_requirement`, and `allow_partial`. Workspace/scopes derive from authentication and authorization, not trusted arbitrary request values.

## Output model

- `context_id`, `request_id`, `agent_id`, `workspace_id`, `generated_at`.
- `status`: `complete|partial|denied|insufficient_evidence`.
- `context_text`: a structured, succinct answer-ready block with stable headings.
- `facts[]`: typed facts with validity, authority, source IDs and status.
- `memories[]`: concise memories with type, scope, timestamps, status and source IDs.
- `instructions[]`: approved procedural preferences, tagged as low-priority contextual guidance (never system instruction replacement).
- `unknowns[]` / `conflicts[]`: missing information and material disagreements.
- `citations[]`: compact identifiers to fetch the exact evidence on demand.
- `budgets`: requested tokens, actual tokens, strategy, truncated counts.
- `retrieval_trace_id`, `source_watermarks`, and `excluded_source_counts`.

## Example context text

> **Current:** Sarah works at Acme and is considering entrepreneurship; no confirmed departure.  
> **Relevant memory:** Discussed launching a company with Michael on October 10, 2026 (memory M-102; source E-582).  
> **Preference:** Prefers decision comparisons with risks, where verified (memory M-099).  
> **Unknown:** No verified personal runway figure.  
> **Caution:** A prior idea was postponed; do not represent it as a signed decision.

## Context invariants

- `CTX-01` Hard token ceiling is enforced using the configured tokenizer or conservative accounting; output never exceeds `max_tokens`.
- `CTX-02` Every fact/memory item carries an evidence pointer or explicit `unsupported/inferred` designation.
- `CTX-03` Never include unauthorized items in candidates, trace payloads or final context.
- `CTX-04` When a current and superseded statement compete, default to current supported knowledge; historical statements only if relevant and clearly labeled.
- `CTX-05` Important conflict/unknown/constraint is not silently removed merely to fit budget.
- `CTX-06` Identical request and same data/config state SHOULD yield materially stable ordering and content; record ranker/model versions.
- `CTX-07` `partial` outputs clearly identify unavailable sources and freshness limits.
- `CTX-08` Context is data for the consuming agent, not privileged instruction text.

## Packing priority

1. Authorization and safety filters **before** search results reach the composer.
2. Critical current facts and task constraints.
3. Required, authorized procedural preferences.
4. High-value relevant episodic memories, recent decisions, and relationship paths.
5. Material conflicts and unknowns.
6. Optional background if budget remains.

The service can return structured item references plus concise text. Agents may use `GET /v1/context/{id}` or source fetch to drill into evidence subject to authorization.

## Context profiles (Admin)

Examples: `quick_answer` (short), `planning` (strong history and constraints), `research` (evidence-rich), `review` (contradictions and provenance), and `personal_assistant` (preferences). Profiles define retrieval weights, allowed sources, max budget, number of graph hops, compression mode and evidence threshold. Agent-specific overrides can only reduce permissions, not expand them.

## Separate completeness from confidence

`complete` says required stores were consulted successfully, not that the facts are true. Use per-item evidence status and material uncertainty notes to communicate truth limitations.
