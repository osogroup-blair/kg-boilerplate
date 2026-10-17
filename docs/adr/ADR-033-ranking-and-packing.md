# ADR-033: The `ctx-rank.v1` ranker and the exact-budget packer

**Status:** Accepted (weights are recorded and ablatable; the token margin is provisional until TK-02)  **Decision refs:** CTX-01, CTX-05, CTX-06, ADR-020, ADR-027, ADR-031  **Evidence:** `packages/context/tests/test_core.py`, `test_properties.py` (Hypothesis), `tests/contract/test_context_budget.py`, `make eval-context --ablate`

## Context
The blueprint asks for deterministic rules before any model reranker, recorded weights and ablations, and a hard token ceiling that is never exceeded and never silently removes a required constraint.

## Decision
### Ranking (`ctx-rank.v1`, pure)
`score = Σ w_f · f − w_div · max_jaccard(item, already chosen items of its section)`; features each in [0, 1]:
`relevance` (source rank and term overlap, see below), `authority` (ADR-027 rank / 5), `evidence` (`ln(1+n)/ln 4`), `importance` (a memory's own, 0.5 for a fact), `proximity` (1 for a requested subject's own item, 0.5 otherwise), `recency` (`exp(-age/90d)`, **only** when the profile weight is above zero: recency is not validity). The diversity penalty is MMR-style so a section is not filled with rephrasings. Ties break by `(section order, kind, id)`; the score is rounded to 9 places. The same candidates in any arrival order give byte-identical output (a Hypothesis property). Weights, features and the ranker version are recorded per item in the trace; `CONTEXT_ABLATE=<feature>` (evaluation only, refused in production) zeroes one weight.

**Relevance** is computed once, from two honest signals. *Lexical*: stemmed topical-term coverage of the task (the subjects' own names removed: a name matches everything about the subject). *Source rank*: the graph hit order, or the memory search score relative to the best hit. A subject's own current fact is relevant by membership when the profile says `current_facts: always` (planning, review) and by term coverage when it says `relevant` (quick answers), which is what keeps a cycling question free of career facts. A memory the task did not find but that belongs to the working set (a subject's approved guidance; a named project's shared memory) is marked *standing* and never scores below 0.5. A profile may add `intent_terms` (planning: decision, risk, trade-off…) to the **memory** query, because what a plan needs from a person's memory is their decision style whatever the topic.

### Tiers (packing priority, blueprint 02-domain/05)
`required`: unknowns, conflicts, each requested subject's top `required_current_k` current facts, the top `required_procedural_k` approved guidance. `high`: unverified facts, related facts, episodic and working memory. `optional`: semantic memory, history. In `relevant` mode a conflict or unknown is material only if the task touches it (its words or its signal's label).

### Packing (pure, exact)
1. Render and count the **required tier**. If it does not fit `max_tokens`: `insufficient_budget`, a notice that fits, and `required_tokens` (the figure that works if asked for). It is never trimmed.
2. Admit `high` then `optional` items by score per token, within soft per-group shares (`section_budget`), then a second pass with the shares lifted. An item is admitted only if the **whole rendered package** (headings, separators, envelope) still counts at or under the ceiling, measured by the same `TokenCounter` whose id goes into the trace. Whole items only: nothing is cut mid-sentence. `max_items` caps what is added beyond the required tier.
3. The returned text is counted once more as returned; a safety net would drop the lowest-value non-required item if that ever exceeded the ceiling (it cannot: admission was exact), and `context_budget_violations_total` would fire.
4. `response_mode=items` is measured by the compact JSON of the items instead.

The counter is `ceil(max(o200k, cl100k) × (1 + margin))` (ADR-020). The 10% margin is provisional until TK-02 measures the router's `prompt_tokens`; because admission is exact against the same counter, calibrating the margin changes what fits, never whether the ceiling holds.

### Compression
`structured` (default): our statements and qualifiers. `extractive`: the source's own words (a fact's ontology-rendered text, a memory's content), no rewording. `summarized` (opt-in per profile, allowed only where `compression` lists it): the packed, already-authorised records go to `ModelGateway.complete(purpose="context_compression")` at the most sensitive item's (and the task's) classification; the answer is accepted only if the envelope is intact, every cited handle exists and every required item's handle survives, every unknown and conflict line is verbatim, every number, date and capitalised name appears in the inputs, and it counts within budget. Anything else falls back to `structured` with a reason (`compression_fallback`). The compressor is never trusted to invent a fact.

## Consequences
- The budget is a property of the algorithm, not a hope: 14 benchmark cases × 10 budgets and thousands of fuzzed packages hold it.
- A tight budget produces a smaller, still-correct package or an honest `insufficient_budget`; it never produces a package with a caveat silently missing.
- An LLM reranker is not added; if the live evaluation shows rules plateau, the ablation report is the evidence to justify one.
