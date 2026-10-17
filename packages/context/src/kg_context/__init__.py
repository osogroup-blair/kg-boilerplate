"""kg_context: the task-aware context engine (phase 6, the MVP).

`POST /v1/context/build` returns the smallest sufficiently informative package for a task: authorised (the SAME scoped searches every other read uses), temporally
correct, deterministic (`ctx-rank.v1`), inside a hard token budget enforced on the exact rendered bytes, and explainable through a metadata-only trace.

    models    request / response and the internal candidate
    text      stemming, term overlap and the neutraliser that keeps item text inside its own line
    profiles  builtin -> ontology -> request narrowing (a request can only narrow)
    planner   the retrieval plan (pure, no model)
    candidates / merge / temporal / unknowns / rank / pack / render   the pure core: same inputs, same bytes
    retrieve  the parallel, individually-timed sources (Postgres facts, graph, memory, conflicts, watermarks)
    compress  the optional, validated, fall-back-able `summarized` mode
    trace     metadata-only traces, re-materialisation, encrypted opt-in debug snapshots
    service   ContextService.build: the ONLY entry point (the public API and the Context Lab both call it)

It reaches the graph only through `kg_knowledge.GraphSearch` and memory only through `MemorySearch` / `MemoryReads` (import-linter): no code here queries
`curated_memories` or Neo4j itself, so a memory or fact the caller may not see cannot influence a score, a count or a timing bucket."""

__version__ = "0.1.0"
