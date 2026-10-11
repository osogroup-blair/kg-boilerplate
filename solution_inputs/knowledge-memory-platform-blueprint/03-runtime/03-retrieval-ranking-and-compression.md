# Retrieval, Ranking and Context Compression

**Parent:** [Blueprint index](../README.md)  
**Capability owners:** CAP-09 / CAP-10  
**Objective:** Minimal token consumption without losing task-critical truth, constraints or evidence.

## Multi-source retrieval

1. **Plan:** parse task/subject/intent and select a versioned `context_profile`.
2. **Authorize:** determine allowable workspace and subject/project/agent/session scopes, permitted sensitivity and information classes.
3. **Graph retrieval:** use Graphiti's hybrid search and targeted graph traversal for entities, relationships and source episodes.
4. **Memory retrieval:** use PostgreSQL metadata/lexical search and pgvector similarity on authorized memory records.
5. **Merge:** deduplicate claims across graph facts and memories; preserve original source pointers.
6. **Temporal reconciliation:** remove stale default-state facts, retain relevant historic memories explicitly labeled as historical.
7. **Rank and pack:** prioritize current relevant constraints, direct evidence, decisions and preferences. Handle unknowns and conflicts explicitly.
8. **Trace:** store explainability metadata, scoring, exclusions and token usage; do not retain full sensitive prompt by default.

## Security: prefilter, never clean up afterward

Authorized candidate sets must be enforced at SQL and graph query boundaries. Filtering only after unscoped vector or graph retrieval risks leakage through search results, logs, re-ranking or LLM compression. For pgvector HNSW, filtering and recall interact: test per-tenant partitioning/exact filtered search and iterative scans before asserting ranking quality. Ref: https://github.com/pgvector/pgvector

## Candidate scoring (illustrative, not fixed formula)

- Task semantic relevance and lexical overlap.
- Entity proximity and relationship type relevance.
- Fact validity/status and source authority.
- Recency of observation **only when appropriate** (not the same as validity).
- Memory importance and independent evidence count.
- Diversity to avoid redundant context items.
- Token cost per item; summaries may outperform raw history.

Use deterministic rule-based prioritization before introducing an expensive LLM reranker. Record feature weights and run ablations against a labeled benchmark.

## Compression modes

| Mode | Behavior | Use case |
|---|---|---|
| `extractive` | Select existing statements/fields, no generative rewriting | High-risk/low-latency |
| `structured` | Deterministic grouped facts with evidence IDs | Default MVP |
| `summarized` | LLM summarizes already filtered candidates, retains citations and caveats | Long multi-event histories |

The compressor is untrusted to invent facts. Validate entity IDs, source references and temporal qualifiers. A low-token response may omit optional context, **never** omit a mandatory constraint or fabricate certainty.

## Budget strategy

Reserve tokens for headings/metadata/critical constraints. Allocate remaining budget to facts, memories, relations and unknowns according to profile; perform actual tokenizer measurement and truncate optional candidates after packing. If even required facts don't fit, return `insufficient_budget` rather than violating ceiling.

## Retrieval observability

Context Inspector shows query plan, filters, candidate counts per source, retrieval scores, dedupe groups, excluded reasons, final token counts, compression version, latency, backend watermarks and sample citations. Visibility of excluded items is also permission-limited.

## Evaluation set

Queries must include: Sarah's current employer; historical employer; business aspiration and conflicting evidence; shared agent findings; unrelated preference distractor; private memory not allowed to other agents; insufficient evidence; backdated update; context budget of 250 vs 1,200 tokens; missing graph or vector backend.

## Requirements / acceptance

- `RET-01` Request with explicit 600-token budget uses no more than 600 tokens measured by configured tokenizer.
- `RET-02` A task about entrepreneurship retrieves the relevant decision memory but not an irrelevant cycling ride.
- `RET-03` Invalidated employment is not stated as current.
- `RET-04` Private memories never leak via search, trace, compression or context output.
- `RET-05` A result provides correct provenance IDs and material unknowns/conflicts.
- `RET-06` Backend unavailability produces accurately labeled partial result or a defined failure.
