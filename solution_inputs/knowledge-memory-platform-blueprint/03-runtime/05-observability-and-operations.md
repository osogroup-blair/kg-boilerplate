# Operations, Deployment and Observability

**Parent:** [Blueprint index](../README.md)  
**Capability owners:** CAP-15 / CAP-16 / CAP-17

## Recommended development topology

Use Docker Compose for `api`, `worker`, `postgres+pgvector`, `neo4j`, and optional `admin` dev server. Provide `.env.example`, secrets never checked in, dependency health checks, migrations, initial demo workspace, and a one-command seed of the Sarah dataset. Pin image/library versions after a compatibility smoke test.

## Production topology (not a v1 dependency)

Container services for API and worker; managed PostgreSQL if available; hardened Neo4j; encrypted backups; model provider via restricted network adapter; secure frontend hosting; aggregated logs and metrics. No message broker necessary until measured throughput warrants it. Scale workers separately from API.

## Required metrics

| Area | Minimum instrumentation |
|---|---|
| Ingestion | accepted, failed, retried, partial, backlog, per-stage duration |
| Graph | episode ingestion success, identity collisions, fact conflicts, query failures |
| Memory | proposals, approved/rejected, active/superseded, duplication, embedding failures |
| Retrieval | latency p50/p95, candidate count, reranker cost, token count, partial responses |
| LLM usage | calls, token counts, provider/model version, estimated USD cost, error classes |
| Governance | access denials, review queue age, correction/deletion completion |
| Storage | event count/bytes, memory count/bytes, vector index size, graph size, object store bytes |

All request paths carry `request_id` and `correlation_id`; a source interaction can be traced through jobs and eventual context usage. Avoid raw personal text in metrics, errors and distributed traces.

## Reliability and recovery

- PostgreSQL outbox is a durable queue. Worker lease/heartbeat avoids concurrent claims; expired leases can be reclaimed.
- Retry transient provider/database outages with exponential backoff and bounded attempts.
- Dead-letter events remain inspectable and replayable with audit.
- Consistency reconciliation compares ledger derivatives with Neo4j and memory store.
- Backups include Postgres, Neo4j, config and encryption key recovery procedures. Test restore to separate environment.
- Delete/retention rules must include expired backup lifecycle and exports.

## Cost controls

- Configurable max episode size, daily tokens per workspace, per-agent context token ceiling and rate limits.
- Use deterministic writes for structured facts; call LLM only where interpretation is valuable.
- Summarize/consolidate with reuse-aware thresholds, not after every event.
- Store one embedding per curated memory/version when useful, not indiscriminately embed all raw events in an additional index.
- Report projected vs observed memory/graph/vector storage and model cost.

## Initial service-level targets (planning hypotheses, not promises)

- 99% of accepted sample events reach either `indexed` or a visible actionable `needs_review` state under healthy backends.
- No silent loss of accepted events in failure injection tests.
- Demo workload returns context within an agreed local p95 target; measure before assigning production latency SLA.
- Hard context token budget respected at all times.
- Cross-workspace leakage tests always return zero unauthorized objects.

## Runbooks to author during delivery

`startup`, `upgrade/pin dependencies`, `graph outage`, `LLM outage`, `stuck job replay`, `identity correction`, `reindex ontology`, `subject deletion`, `backup + restore`, `rotate secrets`.
