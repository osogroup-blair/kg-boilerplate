# End-to-End Flows

**Parent:** [Blueprint index](../README.md)

## Flow A — Interaction ingestion and memory formation

```mermaid
sequenceDiagram
 participant App as Client App
 participant API as API
 participant DB as PostgreSQL
 participant W as Worker
 participant G as Graphiti / Neo4j
 participant M as Memory Service
 App->>API: POST /v1/interactions (idempotency key)
 API->>API: AuthZ, workspace, schema, content checks
 API->>DB: Transaction: event + outbox
 DB-->>API: event_id, accepted
 API-->>App: 202 + status URL
 W->>DB: Claim outbox job
 W->>G: Add source episode (authorized namespace)
 G-->>W: Episode and fact references
 W->>M: Evaluate memory candidates, provenance
 M->>DB: Upsert / propose curated memories
 W->>DB: Update derivative states + watermark
```

**Branching:** The interaction can produce graph facts, memories, both or neither. Failure of one derivative records `partial`, not silent success. Retry is idempotent.

## Flow B — Deterministic authoritative fact

A trusted application submits a typed change, such as an employment termination with effective date. The API validates ontology, authorization, expected revision and time rules; it records the command and applies a deterministic graph mutation (through a controlled adapter). An LLM MAY produce explanatory text but MUST NOT decide whether the command occurred. Persist history and source command ID.

## Flow C — Context retrieval

```mermaid
sequenceDiagram
 participant Agent as Agent
 participant API as Context API
 participant Auth as Scope Policy
 participant KG as Graph Retrieval
 participant MEM as Memory Retrieval
 participant CMP as Context Composer
 Agent->>API: POST /v1/context/build (task, subject, budget)
 API->>Auth: Resolve authorized workspace and scopes
 Auth-->>API: Allowed sources and policy
 par Retrieval
  API->>KG: Search facts/episodes with namespace scope
  API->>MEM: Filtered lexical + vector memory search
 end
 KG-->>CMP: Facts, paths, source IDs
 MEM-->>CMP: Memories, timestamps, source IDs
 CMP->>CMP: Resolve time/conflicts, rank, dedup, compress
 CMP->>CMP: Hard token budget + provenance + caveats
 CMP-->>Agent: Structured context + context_id + freshness
```

**Graceful degradation:** If a backend fails, the response must identify `partial` completeness and excluded sources or fail closed, based on request policy. It must never silently claim complete context.

## Flow D — Correction or identity merge

Admin selects disputed fact or duplicate identity → sees sources/dependents → submits reason and target correction → command is audited → projection is updated → memory references/context caches are invalidated → tests verify old belief is no longer presented as current. Original evidence is not fabricated or silently rewritten.

## Flow E — Deletion

Authorized subject/workspace deletion → freeze future retrieval immediately → enumerate episode/memory/embedding/graph/trace/source copies → delete or redact by policy → verify derivative removal and backup expiry requirements → expose completion receipt. Where historical integrity or legal hold prevents full erasure, clearly communicate restriction and applicable policy.

## Flow F — Rebuild / ontology change

Publish ontology version after impact check → route new ingestion to pinned config → optionally re-ingest historical sources into a **separate namespace** → run evaluations → cut over atomically at routing layer → retire old projection after review. Do not assume a schema change retrospectively fixes existing entities.
