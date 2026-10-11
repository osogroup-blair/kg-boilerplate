# Knowledge & Memory Platform — Solution Blueprint

**Document set version:** 0.1.0 (planning baseline)  
**Status:** Proposed architecture / not yet implemented  
**Last updated:** 2026-10-11  
**Purpose:** A self-contained, hierarchical Markdown specification for generating implementation plans and work packages for a reusable multi-agent knowledge-and-memory boilerplate.

## The core proposition

Build a reusable platform in which applications and authorized AI agents can: (1) ingest interactions, (2) preserve trustworthy evidence, (3) maintain a temporal knowledge graph, (4) create and evolve useful memories, and (5) receive concise, task-specific context through a standard API.

**Context is the product.** Neither a graph database nor long-term storage alone is the intended user experience. The central interface is `POST /v1/context/build`, which returns a bounded, attributable, permission-filtered context package that agents can use immediately.

## Read in this order

| Layer | File | Why it exists |
|---|---|---|
| 0 · Intent | [Platform brief](00-overview/01-platform-brief.md) | Problem, users, goals, non-goals, success criteria |
| 0 · Capabilities | [Capability map](00-overview/02-capability-map.md) | What the boilerplate must standardize |
| 1 · System | [System context](01-architecture/01-system-context.md) | Boundaries, actors, external dependencies |
| 1 · Components | [Components and data ownership](01-architecture/02-components-and-data-ownership.md) | Deployment units, two data stores, truth boundaries |
| 1 · Behavior | [End-to-end flows](01-architecture/03-end-to-end-flows.md) | Write, retrieval, correction, deletion, recovery |
| 2 · Schema | [Ontology and rules](02-domain/01-ontology-and-rules.md) | Configurable object types, attributes, edge types, policy |
| 2 · Graph | [Temporal graph](02-domain/02-temporal-graph.md) | Identity, facts, time, conflicts, provenance |
| 2 · Inputs | [Events and episodes](02-domain/03-events-episodes-and-provenance.md) | Interaction contracts, source evidence, replay |
| 2 · Memory | [Memory model](02-domain/04-memory-model-and-lifecycle.md) | Categories, scoping, writing, consolidation, deletion |
| 2 · Context | [Context package](02-domain/05-context-package.md) | Output schema, budgets, grounding, freshness |
| 3 · APIs | [Service contracts](03-runtime/01-service-contracts.md) | Application-facing endpoints and ownership |
| 3 · Write path | [Ingestion pipeline](03-runtime/02-ingestion-and-knowledge-quality.md) | Workers, validation, review, idempotency |
| 3 · Read path | [Retrieval pipeline](03-runtime/03-retrieval-ranking-and-compression.md) | Search, filtering, ranking, compression, traces |
| 3 · Agents | [Multi-agent memory](03-runtime/04-agent-integration.md) | Sharing, proposals, access, SDK/MCP |
| 3 · Operate | [Operations](03-runtime/05-observability-and-operations.md) | Telemetry, deployment, backup, cost, reliability |
| 4 · Control plane | [Admin console](04-administration/01-admin-console.md) | Pages, admin tasks, UX acceptance criteria |
| 4 · Trust | [Security and governance](04-administration/02-security-governance-and-privacy.md) | Isolation, auth, privacy, prompt injection, audit |
| 5 · Planning | [Delivery slices](05-delivery/01-implementation-slices.md) | Dependency-ordered implementation plan inputs |
| 5 · Quality | [Acceptance and evaluations](05-delivery/02-acceptance-and-evaluations.md) | Test matrix, measurable release gates |
| 5 · Decisions | [ADRs and open decisions](05-delivery/03-decisions-assumptions-and-risks.md) | Adopted baseline versus items to verify |
| 6 · Demonstration | [Sarah walkthrough](06-reference/01-sarah-end-to-end.md) | Full two-agent, temporal, memory scenario |
| 6 · Contracts | [Illustrative payloads](06-reference/02-example-config-and-payloads.md) | YAML/JSON examples to seed engineering work |

## Stable vocabulary

- **Interaction/event:** a recorded occurrence, request, observation, or action in the application.
- **Graphiti episode:** the source unit ingested by Graphiti to derive entities and facts; distinct from a curated memory.
- **Fact:** an attributable proposition, often represented as a typed relationship, with time and status.
- **Memory:** a deliberately retained, reusable representation of an experience, fact, preference, instruction, or agent outcome.
- **Context package:** an access-controlled, token-bounded selection of facts, memories, evidence and uncertainty for a particular task.
- **Ontology:** published, versioned schema and constraints for allowed entities and relationships.
- **Workspace:** authorization and data boundary. An agent may access only authorized scopes within it.

## Normative convention

- **MUST** = requirement for the baseline; **SHOULD** = default recommendation; **MAY** = optional capability.
- Identifiers such as `CTX-01` refer to planned acceptance requirements, not implemented features.
- API names and payloads are **proposed platform contracts**, not Graphiti-native endpoints.
- Mermaid diagrams are conceptual and should be refined into runtime/deployment diagrams during implementation planning.

## Architectural baseline

1. **Neo4j + Graphiti** for source episodes, extracted entities, temporal facts, graph/hybrid retrieval.
2. **PostgreSQL + pgvector** for ingestion ledger, curated memory records and embeddings, jobs, reviews, agents, policy/configuration versions, context traces, audit metadata.
3. **FastAPI modular monolith + separate worker processes**; one backend repository, not microservices.
4. **React/TypeScript admin UI** is proposed, but framework choice is replaceable.
5. **Configurable LLM + embedding providers** behind adapters; pin Graphiti and model versions before coding.
6. **Optional object store** for originals that should not be stored inline (large docs, attachments, audio).

## Non-negotiable invariants

1. Storage and retrieval MUST enforce authorization; Graphiti `group_id` is not an authorization system.
2. Every retained assertion MUST be traceable to its origin or explicitly marked as a user/admin assertion.
3. Proposed agent findings MUST NOT silently supersede verified facts or system instructions.
4. Validity time (when true in the world) MUST be distinguishable from record time (when learned).
5. Context packages MUST include only relevant, authorized information within the requested budget; uncertainty is not discarded merely to save tokens.
6. Structured authoritative writes MUST NOT depend on LLM extraction.
7. Cross-store updates are eventually consistent; observable states and recovery behavior MUST be defined.
8. Any deletion workflow MUST address source data, graph derivatives, memory, embeddings, traces, and backups according to policy.

## How to use with an implementation planner

Feed the planner this `README.md`, then [implementation slices](05-delivery/01-implementation-slices.md), [acceptance criteria](05-delivery/02-acceptance-and-evaluations.md), and the domain/runtime documents for the target slice. Request an implementation plan with: repositories/files, migrations, contracts, sequence diagrams, dependency checks, unit/integration/security tests, demo scenario, and rollback strategy. Keep unresolved decisions explicitly open rather than allowing the planner to invent silent assumptions.

## Source / framework reference

- Graphiti: https://github.com/getzep/graphiti
- Graphiti v3 quickstart: https://help.getzep.com/graphiti/getting-started/quick-start
- Graphiti custom entity/edge types: https://help.getzep.com/graphiti/core-concepts/custom-entity-and-edge-types
- Graphiti graph namespacing: https://help.getzep.com/graphiti/core-concepts/graph-namespacing
- Graphiti searching: https://help.getzep.com/graphiti/working-with-data/searching
- pgvector: https://github.com/pgvector/pgvector

**Verification note:** The docs reflect the stated capabilities available in October 2026; concrete packages, APIs, provider compatibility and version-specific behaviors must be verified and pinned during build planning.
