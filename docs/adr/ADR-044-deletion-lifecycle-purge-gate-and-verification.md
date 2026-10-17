# ADR-044: Deletion is freeze, plan, purge, verify, receipt, and the database enforces the plan

**Status:** Accepted  **Decision refs:** SEC-05, AT-16, MEM-07, Flow E, ADR-018 (correction and deletion), phase-8 plan §5.4  **Evidence:** `tests/integration/test_p8_deletion*.py`, `tests/integration/test_p8_graph_deletion.py`, `tests/security/test_deletion_read_block.py`, `tests/security/test_deletion_residuals.py`, `tests/integration/test_db_p8.py`, `scripts/demo_p8.py` step 9

## Context
Erasure touches Postgres, pgvector, Neo4j, traces, jobs and idempotency records, and must be provably complete, safe to interrupt, and impossible to turn into a general delete.

## Decision
1. **Freeze in the request transaction.** The closure (the subject and merged descendants; the interactions linked to it; facts touching it or sole-sourced from deleted things; memories only about it or resting only on deleted things; extracted entities left with nothing) is set `deletion_pending` before the `202`. Every read surface answers 404 from that commit, identically to a missing id (lists, counts, search, neighbourhoods, contexts, traces, commands and review lists included); the projector and jobs skip frozen rows.
2. **Plan, purge, verify are three jobs** (`deletion_plan|purge|verify`, dedupe key `deletion:{request}:{stage}:g{generation}`). Steps are fixed per store, each in its own transaction, each idempotent; a crash repeats work only. A failed job marks the request `failed`; `retry` resumes.
3. **The purge gate.** Every gated table has a trigger calling `public.purge_gate(workspace, store, kind, id, action)`, true only for an owner role, or for a request in `purging` in the same workspace that **lists that exact row with that action** (or `kind = workspace`). `audit_log` is never alterable. The application role has DELETE on those tables but cannot use it outside a plan; `tests/integration/test_db_p8.py` forges requests, wrong workspaces and wrong actions.
4. **Retained knowledge keeps independent evidence.** A fact or memory with evidence outside the deleted set stays; only its citation of the deleted source goes, and its first-evidence pointer is re-attached.
5. **Graph purge is mine, not Graphiti's.** Episodes (including those a half-finished extraction left, found by their deterministic id), nodes and edges are removed by uuid; summaries that cite the subject are cleared; dangling references pruned; orphans swept; survivors are **re-projected from the final Postgres state**.
6. **Verification blocks `completed`.** Id scans across every store, residual-text scans for the manifest's names and identifiers (names a surviving entity also carries are skipped), and a graph scan. A residual raises a `deletion_exception` item (the memory is named by id, never quoted) and the request ends `completed_with_exceptions` until a person redacts, deletes or retains with a reason, then `retry_verification`.
7. **Exceptions for text that merely names the subject.** Another subject's memory is not deleted automatically (privacy over recall applies to what *rests on* the subject, not to what mentions them): a reviewer redacts it (the original goes) or decides.
8. **Receipt.** Counts per store, what was kept and why, the verification result, `backup_copies_expire_by` (the workspace's `backup_retention_days`), the journal reference. Ids and counts only.

## Consequences
- A bug in the planner can under-delete (caught by verification) but not over-delete.
- Deleting is slow relative to freezing on purpose: the user-visible guarantee is the freeze; the proof is the receipt.
- Telemetry carries counters by store and action only (`deletion_residuals_total{store}`), never ids.
