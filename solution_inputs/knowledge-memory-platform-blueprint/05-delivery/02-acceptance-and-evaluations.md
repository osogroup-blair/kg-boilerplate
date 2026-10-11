# Acceptance Tests and Evaluation Plan

**Parent:** [Blueprint index](../README.md)  
**Intent:** Give implementation planners concrete verification obligations, not only features.

## End-to-end reference scenario

1. Admin publishes Person, Organization, Topic, WORKS_AT, KNOWS, CONSIDERING ontology and policies.
2. App ingests Sarah's employment and friendship histories as signed, source-linked events.
3. Conversation states Sarah is considering entrepreneurship and she discusses it with Michael.
4. Episode yields temporal fact proposal and a curated memory; current employment remains Acme.
5. Research Agent writes a source-backed project finding; review/policy publishes it.
6. Planning Agent requests 600-token context and receives only relevant current graph facts, memory, constraints and unknowns.
7. New confirmed job event changes current status; old status becomes historical.
8. Unauthorized agent cannot read Sarah's restricted memory.
9. Reviewer corrects a mistaken relationship, and subsequent retrieval respects correction.
10. A deletion request blocks retrieval and executes planned purge/reconciliation.

See [Sarah walkthrough](../06-reference/01-sarah-end-to-end.md) for concrete data.

## Contract test matrix

| ID | Test | Expected |
|---|---|---|
| AT-01 | Repeat `POST /interactions` with same idempotency key | Same logical event, no duplicate derivative |
| AT-02 | Retry worker after Neo4j temporarily unavailable | Event eventually indexed or visibly actionable |
| AT-03 | Same name, two different people | No unjustified identity merge |
| AT-04 | Backdated promotion inserted later | Both valid-time and record-time distinguishable |
| AT-05 | Contradictory agent claim vs verified source | Claim disputed/reviewed, verified state not silently displaced |
| AT-06 | Context for Sarah entrepreneurship | Aspiration memory included with correct evidential status |
| AT-07 | Context for bicycle ownership | Irrelevant career memories excluded |
| AT-08 | Context budget 600 tokens | Actual count <= 600 under selected tokenizer |
| AT-09 | Context request without enough evidence | Unknowns reported, no fabricated answer |
| AT-10 | Agent A accesses Agent B private memory | Denied, including vector candidates and traces |
| AT-11 | Workspace A accesses workspace B graph | Denied, even with known IDs |
| AT-12 | Context Inspector explainability | Shows selected source IDs, status, filters, tokens |
| AT-13 | Admin corrects a mistaken employer | Subsequent current context fixed; audit linked |
| AT-14 | Explicit procedural preference proposed by unauthorized agent | Not activated/shared |
| AT-15 | Source content has prompt injection | No policy changes, no data exfiltration |
| AT-16 | Delete subject | Immediate read block, verified purge of derivatives |
| AT-17 | Publish ontology version 2 | New jobs pinned to v2, existing provenance retains v1 |
| AT-18 | Backup/restore in test environment | Consistent historical records or reconciled projection |

## Retrieval evaluation methodology

Create a fixed benchmark containing query, principal, workspace, expected evidence IDs, expected facts, forbidden facts, temporal qualifier, memory type, token budget and an approved ground-truth narrative. Score:

- **Evidence recall@k:** did retrieval surface the supporting source(s)?
- **Precision@k / irrelevant inclusion:** what percentage of packed items actually support the task?
- **Temporal correctness:** did it distinguish current from superseded facts?
- **Attribution:** does each selected assertion link to a real authorized source?
- **Unsupported statement rate:** number of model-introduced unsupported claims.
- **Authorization leakage:** any unauthorized item in result, candidates, traces or debug metadata (must be zero).
- **Budget adherence:** tokenizer-measured tokens <= ceiling (must be 100%).
- **Update freshness:** time from accepted source to indexed/retrievable status.
- **Cost/latency:** tokens/call and p50/p95 by scenario.

Use separately labeled test sets for ranking, extraction, conflicts, authorization and prompt-injection. Test model/version changes against the frozen dataset before promotion.

## Suggested release gating

Security and deterministic invariants (`AT-01`, `AT-08`, `AT-10`, `AT-11`, `AT-15`, and read-block in `AT-16`) are strict blockers. Other numeric targets need a measured baseline and team-approved threshold, not a fabricated guarantee. A lower recall should be visible and measured rather than hidden behind convincing generated prose.

## Coverage traceability

`P-*` from [platform brief](../00-overview/01-platform-brief.md) → `CAP-*` from [capability map](../00-overview/02-capability-map.md) → domain IDs (`ONT`, `KGR`, `EVD`, `MEM`, `CTX`, `ING`, `RET`, `AGT`, `ADM`, `SEC`) → `AT-*` above → implementation ticket/test IDs.
