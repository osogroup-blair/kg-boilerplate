# Ingestion Pipeline and Knowledge Quality

**Parent:** [Blueprint index](../README.md)  
**Capability owners:** CAP-05 / CAP-06 / CAP-15

## Synchronous acceptance (API)

1. Authenticate application/agent and derive workspace and allowed target subject scope.
2. Validate content limits, sensitivity, format, timestamps, schema version and idempotency key.
3. Store normalized interaction envelope + encrypted raw content or content pointer as policy permits.
4. Append outbox record in **the same PostgreSQL transaction** and return event ID/status.

## Asynchronous processor

`claim job -> load pinned ontology/policies -> normalize text/JSON/message -> prepare extraction -> Graphiti add episode -> collect graph references -> propose curated memories -> run validation and conflict checks -> publish or queue review -> update event status -> record metrics`.

## Pipeline stage states

| Stage | Outcome status | Recovery behavior |
|---|---|---|
| Normalize | `normalized` / `invalid` | Invalid is terminal until source fixed |
| Graph ingest | `graph_indexed` / `graph_failed` | Retry with stable source key; reconcile duplicates |
| Memory propose | `memory_indexed` / `memory_failed` | Retry idempotently by source and candidate hash |
| Review | `needs_review` / `approved` / `rejected` | Human or deterministic policy resolution |
| Finalize | `indexed` / `partial` / `failed` | Derived state visible to callers |

## Duplicate/identity workflow

Generate identity candidates from canonical identifiers, aliases, names and context. High-confidence deterministic matches can be linked automatically only according to configured rules. Ambiguous matches go to review. Provide human-visible side-by-side attributes and source evidence; preserve alias and lineage on merge.

## Fact conflict policy

- **New independent fact** → proposed or active based on source policy.
- **Historical replacement** → update valid time and preserve prior assertion.
- **Concurrent contradiction** → disputed fact, review required for high impact.
- **Unsupported inference** → candidate labeled as inferred; cannot silently displace verified fact.
- **Malicious content** → isolate payload as data; no execution or policy mutation.

## Model failure containment

Structured output parsing errors, invalid type mapping, provider timeouts and token exhaustion can happen. Constrain extraction schemas, log safe error metadata, cap retries and quarantine irrecoverable inputs. Do not send exact personal source content into telemetry by default.

## Reprocessing and replay

Worker execution is at-least-once. Deduplication is by stable source ID, job stage and derivative ID. Have a reconciliation command that compares source ledger with graph/memory status. Re-ingestion under a new ontology/model version should target an isolated staging namespace if meaning can change, then evaluate/cut over.

## Quality review queue

Each review item includes: type (`identity_match`, `contradiction`, `memory_proposal`, `policy_violation`, `deletion_exception`), evidence links, before/after representations, suggested action, source authority, ontology version, deadline, actor and audit reason. Admin actions must be concurrency safe and reversible via compensating commands where feasible.

## Requirements / acceptance

- `ING-01` A duplicate API request with the same key does not create duplicate logical source/episodes/memories.
- `ING-02` A graph outage leaves accepted interactions retryable and visible as partial/failed.
- `ING-03` A model extraction error cannot mutate ontology or trusted system instructions.
- `ING-04` A conflicting employer inference is queued rather than silently replacing a verified employer.
- `ING-05` Operators can replay a stuck event with audit record and without duplicate derivatives.
- `ING-06` Source sensitivity controls whether content can be sent to external model providers.
