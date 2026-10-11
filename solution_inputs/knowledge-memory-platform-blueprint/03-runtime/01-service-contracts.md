# Service Boundaries and API Contracts

**Parent:** [Blueprint index](../README.md)  
**Pattern:** Single FastAPI modular monolith, with dedicated worker processes. API examples are platform proposals.

## Logical modules

| Module | Input | Output | State owner |
|---|---|---|---|
| Auth & Workspace | Credential, requested action | Principal, allowed workspace/scope | PostgreSQL |
| Ontology & Rule Policy | Draft definitions/commands | Published version, decisions | PostgreSQL |
| Ingestion | Interaction, structured commands | Accepted event and status | PostgreSQL |
| Graph Adapter | Normalized episode / typed fact | Graph references / query results | Neo4j via Graphiti |
| Memory | Memory proposal, event references | Curated records, embeddings, lifecycle | PostgreSQL |
| Knowledge Quality | Conflicts / reviewer decisions | Resolution command and audit | PostgreSQL + projection |
| Retrieval | Query + access scope | Filtered graph facts/memories | Both |
| Context | Task + token budget | Attributable package and trace | PostgreSQL metadata |
| Operations | Runtime/job events | Metrics and safe repair actions | PostgreSQL + metrics |

## Proposed v1 endpoints

| Method | Endpoint | Auth use | Essential behavior |
|---|---|---|---|
| `POST` | `/v1/interactions` | App/agent | Idempotent accept, async job |
| `GET` | `/v1/interactions/{event_id}` | Reader | Processing status and derivative IDs |
| `POST` | `/v1/entities` | Trusted app/admin | Typed validated command, no LLM dependency |
| `GET` | `/v1/entities/{entity_id}` | Reader | Entity + current facts / optional history |
| `POST` | `/v1/entities/{entity_id}/corrections` | Reviewer | Correction command and audit |
| `POST` | `/v1/memories/proposals` | Agent/app | Submit evidence-backed candidate |
| `POST` | `/v1/memories/search` | Reader | Filtered lexical/vector search |
| `GET` | `/v1/memories/{memory_id}` | Reader | Content, history and evidence |
| `POST` | `/v1/memories/{memory_id}/supersede` | Reviewer/owner | Explicit memory correction |
| `POST` | `/v1/context/build` | Agent/app | Token-bounded context package |
| `GET` | `/v1/context/{context_id}/trace` | Admin/owner | Retrieval explanation; privacy-filtered |
| `GET/POST` | `/v1/ontologies` | Admin | Draft/version/publish actions |
| `GET/POST` | `/v1/reviews` | Reviewer | Review queue and resolution |
| `GET` | `/v1/operations/jobs` | Operator | Pipeline health and reprocessing metadata |
| `POST` | `/v1/deletion-requests` | Authorized actor | Initiate and track complete purge workflow |

For asynchronous operations, use `202 Accepted` with a status URL; typed validation errors use standard 4xx errors. Every response has a request ID. IDs/paths are illustrative and must be finalized in an OpenAPI contract.

## API shape rules

- Version routes from day one and publish generated OpenAPI documentation.
- Require `Idempotency-Key` on ingestion and mutation endpoints; clients can retry safely.
- Resolve tenant/workspace IDs from authenticated principal, never accept arbitrary group IDs from agent payloads.
- Use cursor pagination for graph neighbors, histories, memory search, and review queues.
- Include schema version or ETag on mutable commands for optimistic concurrency.
- Return cited data with source IDs even when content was summarized.
- Use structured `error_code`, `message`, `request_id` and optional `retry_after`.
- Rate-limit by agent/workspace and expensive method, with budget/quota visibility.

## Package boundaries

```text
apps/api/  (routes, auth, serialization)
packages/domain/ (typed contracts, validations)
packages/ontology/ (configuration + schema compilation)
packages/ingestion/ (ledger + outbox + adapters)
packages/graph/ (Graphiti adapter + graph read models)
packages/memory/ (curated memory + pgvector repository)
packages/context/ (retrieval orchestration + packing)
packages/governance/ (access, reviews, audit)
packages/observability/ (trace and metrics)
apps/worker/ (same packages, background entrypoint)
```

## Interface evolution

Separate public API types from Graphiti internal models so Graphiti library upgrades do not break clients. Use contract tests with representative payloads; reserve changes in query semantics behind versioned context profiles. Any future MCP gateway must simply expose the same authorized service calls and never bypass policy.

See [sample payloads](../06-reference/02-example-config-and-payloads.md).
