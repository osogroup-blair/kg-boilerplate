# ADR-027: Reconcile-then-restore and the authority policy

**Status:** Accepted  **Decision refs:** ADR-021 (state-based projection), ADR-023 (succession vs supersession), ADR-026  **Evidence:** `packages/ontology/tests/test_conflicts.py` + `fixtures/conflicts/policy.yaml`, `tests/integration/test_p4_extraction.py`, `test_p4_reviews.py`, `test_p4_lock.py`, `tests/resilience/test_p4_failure_injection.py`, demo-p4 steps 2, 4, 8

## Context
Graphiti writes to the graph while it extracts (new nodes, new edges, invalidation of old edges, rewritten summaries). Left alone, one sentence in a conversation ("I work at NewCo now") could end the HR-verified Acme edge. The platform's rule (ING-04, KGR-03): *a model-extracted claim never displaces a higher-authority fact.*

## Decision
**Graphiti's output is a proposal.** After `add_episode` the handler (a) reads what Graphiti produced, (b) decides in Postgres, (c) makes the graph equal Postgres again.

1. **Authority ranking:** `agent_generated`(1) < `conversation_report`(2) < `user_asserted`(3) < `admin_asserted`(4) < `authoritative_system`(5). Extraction is capped (`EXTRACTION_MAX_AUTHORITY`) at `conversation_report`, or `agent_generated` when an agent submitted the event.
2. **Fact states for extracted facts:** `active` + `evidence_status=unverified` (no conflict), `proposed` (conflicts with an equal-or-higher fact, awaits review), `disputed`, `rejected`. Verified command facts keep `evidence_status=verified`.
3. **Pure policy** (`kg_ontology.conflicts.resolve`): input = the incoming fact, the existing facts on the same endpoints/relationship, and a `PolicyInputs` bundle; output = a `Resolution` (status, reviews to open, invalidations to apply, reason codes). Hard rejects (relationship type or endpoint outside the ontology → `out_of_ontology`, kept for audit) happen first; a hold (`identity_pending`) makes a fact `proposed` with that code when its endpoint is an unresolved candidate.
4. **Succession is not conflict** (ADR-023): a new role on a cardinality-1 relationship that starts after the old one ends the old one's valid time; an *unverified* extracted fact never does this to a verified one. A backdated claim is dated by the text (`valid_from` from `EdgeTimestamps`) and keeps platform record time separately.
5. **Revert, don't merge:** every Graphiti invalidation of a fact that the policy does not allow is recorded as a `Revert`, the edge is restored from Postgres, and a `contradiction` review is opened with both facts attached (AT-05).
6. **Restore:** the projector is state-based. For the episode it renders each node and edge from Postgres (`expired_at` set for non-active facts; the edge's `episodes` list comes from `fact_evidence`; a changed endpoint is delete-and-recreate with the same uuid; the Graphiti summary is preserved), so the graph equals Postgres whatever Graphiti did. `kg graph reconcile` proves it (drift = 0 in demo-p4).
7. **Serialisation:** `graph_ingest` and `graph_projection` jobs share `concurrency_key="graph:{workspace}"` and take a Postgres session advisory lock (`GraphLock`). The command API's fast path waits 0.25 s for it, then answers 202 (the outbox job finishes the projection).
8. **Review decisions are ledger commands** (`review.decide`, `entity.merge`) with `If-Match` and `Idempotency-Key`; `rationale_code` is a closed enum, free text lives only on the review row. A decision on facts re-runs the pure policy and restores the graph. `mark_disputed` flags **both** sides (KGR-06).

## Alternatives considered
- *Run Graphiti into a scratch group and import the result:* doubles storage and loses Graphiti's own dedupe against the real graph.
- *Forbid Graphiti invalidation (patch it):* couples us to library internals; reverting from Postgres is independent of how Graphiti decides.
- *Treat extracted facts as `proposed` until reviewed:* the review queue would drown; unverified-but-active with a visible badge matches how the blueprint treats conversation reports.

## Consequences
- Between Graphiti's write and the restore the graph briefly holds Graphiti's version; the lock keeps everyone else out of that window, and a crash in it is healed by the retry (the stage finds the episode already extracted and only reconciles and restores).
- Reads that must be authoritative join graph hits to Postgres (`search_facts` returns only ids; status and evidence come from Postgres).
- Split (undoing a wrong merge) is a P8 compensating command; until then see the identity-correction runbook.
