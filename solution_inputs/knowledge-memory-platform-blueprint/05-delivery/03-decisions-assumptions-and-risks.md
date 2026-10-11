# Architecture Decisions, Assumptions and Risks

**Parent:** [Blueprint index](../README.md)  
**Decision status:** The baseline below reflects the discussion; it is not a declaration that engineering compatibility testing has been completed.

## Baseline decisions (proposed acceptance)

| ADR | Choice | Rationale | Consequence |
|---|---|---|---|
| ADR-001 | Two DB engines: Neo4j and PostgreSQL+pgvector | Temporal graph and governed curated memory/events have different access patterns | Cross-store reconciliation needed |
| ADR-002 | Graphiti for source episodes and graph extraction | Existing temporal episode/fact/extraction/search primitives | Adapter and version pinning needed |
| ADR-003 | Curated memory is a first-class custom module | Agent sharing, scoped working memory and consolidation need explicit control | Extra schema and lifecycle work |
| ADR-004 | FastAPI modular monolith + workers | Simple initial operational model with separated async processing | Guard package boundaries to prevent tangling |
| ADR-005 | Context API is the primary agent interface | Optimize for concise, task-correct, attributable context | Retrieval evaluation is product-critical |
| ADR-006 | Admin-configured, versioned ontology and declarative rules | Reusable across domains without arbitrary code execution | Requires compiler/validation and migration process |
| ADR-007 | Trusted structured commands bypass LLM fact extraction | Deterministic business truth and reliability | Two coordinated write paths |
| ADR-008 | PostgreSQL transactional outbox | Durable ingestion without Kafka at MVP scale | At-least-once worker semantics, idempotency required |
| ADR-009 | Propose/validate/publish for shared agent memory | Prevent agent-written hallucinations becoming global truth | Reviewer/policy workflow needed |
| ADR-010 | Scope-checked retrieval and evidence | Privacy and cross-agent/workspace boundaries | Identity/authorization must precede data query |

## Important unresolved decisions

| Ref | Question | Proposed default / validation |
|---|---|---|
| DEC-01 | Which exact Graphiti, Neo4j and Pydantic versions? | Pin after quickstart, custom type, namespacing, retrieval and deletion compatibility tests |
| DEC-02 | Which model providers and where is private data processed? | Provider abstraction with approved local/cloud profiles and egress policy |
| DEC-03 | Which JWT/identity provider? | External OIDC or simple local dev auth, with production path documented |
| DEC-04 | Exactly how to represent corrected extracted graph edges? | Spike Graphiti update/remove capabilities; maintain correction projection if needed |
| DEC-05 | Can an ontology compiler safely generate Graphiti Pydantic classes at runtime? | Prototype; fallback to allowlisted typed classes and validated mapping |
| DEC-06 | Graph tenancy: shared DB group_id vs separate DB per tenant? | Default namespaced dev; production decision based on isolation requirements |
| DEC-07 | What are source/raw episode retention and backup windows? | Configurable policies, default minimized, needs privacy/compliance decision |
| DEC-08 | Memory embeddings for all records or selective? | Only searchable active curated memories initially; evaluate cost/recall |
| DEC-09 | Which fields may agent memories treat as procedural instructions? | Published preferences only; high privilege rules remain in code/config |
| DEC-10 | What is the initial UI framework and visualization library? | React/TypeScript and vetted graph component; validate accessibility/performance |
| DEC-11 | Which exact p95 and freshness SLOs? | Measure on representative seeded dataset first |
| DEC-12 | Does data require bitemporal point-in-time guarantees? | Valid-time and record-time metadata at MVP; formal bitemporal SLA later |

## Risks and mitigations

- **Model extraction drift:** a later model version changes entity identity or edge semantics. *Mitigation:* version metadata, sample gold set, staging namespace and evaluations.
- **Temporal ambiguity:** natural language doesn't specify exact date. *Mitigation:* allow null validity, mark inference, avoid fabricate-now default.
- **Cross-store stale state:** graph updated but memory not yet consolidated. *Mitigation:* job progress, watermarks, reconciliation and partial-status context.
- **Memory poisoning and prompt injection:** agent-generated claims silently alter behavior. *Mitigation:* proposal workflow, source validation, instruction/data boundary.
- **Over-compression:** compact context loses caveat or conflict. *Mitigation:* hard-required unknowns/constraints and evaluation.
- **Sensitive over-retention:** private histories outlive their purpose. *Mitigation:* purpose-limited retention, data classification and deletion orchestration.
- **Tenant vector recall degradation:** vector filter/index combination misses candidates. *Mitigation:* exact scoped search at MVP and per-workspace evaluation before ANN optimization.
- **Rebuild feasibility:** deleting/re-ingesting Graphiti derivatives may be nontrivial. *Mitigation:* compatibility spike and bounded reprocessing namespace.

## Framework references (verification, not endorsements)

- Graphiti source and dependencies: https://github.com/getzep/graphiti
- v3 custom types: https://help.getzep.com/graphiti/core-concepts/custom-entity-and-edge-types
- v3 namespace scope: https://help.getzep.com/graphiti/core-concepts/graph-namespacing
- v3 retrieval: https://help.getzep.com/graphiti/working-with-data/searching
- pgvector filtering and ANN caveats: https://github.com/pgvector/pgvector
