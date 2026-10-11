# Events, Episodes and Evidence Provenance

**Parent:** [Blueprint index](../README.md)  
**Capability owners:** CAP-05 / CAP-06

## Different levels of recorded history

1. **Business interaction event (PostgreSQL):** an immutable envelope recording what an application/agent submitted; payload may be a safe inline body or encrypted object pointer.
2. **Graphiti episode (Neo4j):** the text/message/JSON input unit Graphiti processes to extract entities and facts. It is itself represented in the graph with provenance edges.
3. **Curated memory (PostgreSQL):** a durable, compressed or selected interpretation of one or more events/episodes, built for agent reuse.

These must not be conflated. One event may produce one Graphiti episode and zero or many memory candidates; one memory may consolidate many events.

## Event envelope contract

| Field | Required? | Rationale |
|---|---|---|
| `event_id` | Generated | Stable dedupe/reference |
| `workspace_id` | From auth | Security boundary |
| `source_system` and `source_ref` | Yes | Provenance and replay |
| `actor_id` / `subject_ids` | When known | Identities involved |
| `occurred_at` | Yes or explicit unknown | Event time |
| `recorded_at` | Generated | System time |
| `kind` | Yes | Message, observation, structured change, document etc. |
| `content_type` and `payload_or_pointer` | Yes | Source representation |
| `sensitivity` / `consent` | Policy-derived | Processing and retention |
| `ontology_version` / `pipeline_version` | Generated | Reproducible interpretation |
| `idempotency_key`, checksum | Yes | Safe repeat processing |
| `processing_status` | Generated | Accepted, processing, indexed, partial, failed |

**Important:** A conversation can contain untrusted instructions. The data plane must not treat it as an instruction to the memory pipeline, policy engine or agent orchestrator.

## Source-of-truth priority

- A structured change originating from an authenticated authoritative service may be projected directly into graph facts after validation.
- A conversation is evidence of what someone said, **not necessarily that the assertion is true**.
- A generated summary is derivative evidence, not equal in authority to the original.
- An agent proposal is a candidate, not automatically a verified fact.

## Deduplication and idempotency

Unique index on `(workspace_id, source_system, idempotency_key)`; store a request checksum so reuse of a key with different content fails. Outbox processing MUST record per-stage progress and deterministic external operation keys where supported. Review Graphiti's behavior on repeated episode submission; if it cannot guarantee idempotency by itself, enforce it in the adapter/ledger.

## Corrections vs rewriting history

Don't mutate the original event to hide a mistake. Append a correction command linked to the erroneous source/derivative and update projected current facts and memories. Hard deletion remains separate and must satisfy privacy requirements.

## Retention policy

A configuration determines raw source retention, Graphiti episode retention, curated memory retention, retrieval trace retention, and backups. A source should not be deleted solely because it was summarized if replay/audit must remain possible. If source retention is intentionally shorter than memory retention, document the associated loss of provenance fidelity and deletion scope.

## Requirements / acceptance

- `EVD-01` The API accepts JSON/text/message events and returns a stable event ID and status.
- `EVD-02` Re-sending the same event with same idempotency key creates no duplicate logical event.
- `EVD-03` Every derivative records the event/episode provenance path.
- `EVD-04` Source-only interactions remain valid even if nothing is learned.
- `EVD-05` Ingestion failures have retryable stage metadata and can be safely replayed.
- `EVD-06` Original sensitive source access is restricted and audited.

## Reference

Graphiti v3 episode documentation: https://help.getzep.com/graphiti/getting-started/quick-start
