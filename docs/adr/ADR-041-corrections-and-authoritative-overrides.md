# ADR-041: Corrections are audited ledger commands, and a person may override an authoritative source

**Status:** Accepted  **Decision refs:** AT-13, ADM-03, Flow D, ADR-018 (correction and deletion), phase-8 plan §5.1  **Evidence:** `tests/integration/test_p8_corrections.py`, `scripts/demo_p8.py` steps 4, 5, 7

## Context
Until P8 a wrong fact could only be ended by its source or left disputed. ADR-018 promised corrections that "append, never rewrite". The questions: what a correction is, how it relates to the authority ranking of ADR-027, and how far its effect reaches.

## Decision
1. **A correction is a ledger command** (`correction.fact_reject | fact_supersede | fact_end | fact_restore | attribute | rename | memory`), with `If-Match` on the record's revision, an `Idempotency-Key`, a closed **reason code** and an optional note kept on the `corrections` row only. Its audit entry carries ids and codes, never text.
2. **It is not ranked against authority.** The conflict policy (ADR-027) decides between *claims*; a correction is a decision by a person with `correction.apply`. A correction of an `authoritative_system` fact applies, and an `authoritative_override` review item records it so the source's owner is told. The alternative (refuse, and make the admin fix the source first) leaves the platform serving a known-wrong fact for as long as the source stays wrong.
3. **Reject / supersede / end / restore.** Reject marks the fact `rejected` (kept as a record), expires its edge, and by default makes the facts it ended by succession current again. Supersede creates the replacement (`source_type = correction`) and rejects the old fact. End sets `valid_to`. Restore undoes a reject.
4. **Dependants are flagged, not rewritten.** Shared memories that rest on a rejected fact become `disputed` with a `memory_conflict` item (`source_fact_rejected`); a restore reverses exactly those. Contexts re-materialise on read.
5. **Every correction has an impact preview** (`GET …/corrections/preview?kind=`), computed with the caller's own visibility: the counts never exceed what the caller could open.
6. **Postgres first.** The correction commits with its projection job; the graph is reconciled by the projector and `graph reconcile` shows no drift.

## Consequences
- A wrong employer from HR is fixed in minutes with a trail; the override notice is the place to chase the source.
- Overriding an authoritative system is a privilege (`correction.apply`: reviewer and admin), not an ontology rule; an ontology cannot forbid it. A deployment that wants two-person control can require the notice to be acknowledged.
- The correction history is read through `GET /v1/corrections`.
