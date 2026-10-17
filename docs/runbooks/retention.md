# Runbook: retention windows

Defaults keep everything. Settings → **Retention** sets how long each kind of data lives; **empty means keep**. A change is a new settings version (history is kept) and applies at the next sweep.

| Setting | What the sweep does |
|---|---|
| Raw interaction content | removes the original text after N days (the normalized form stays, so replay still works) |
| Normalized content | removes the normalized form; such an interaction can no longer be replayed |
| Graph episode text | blanks the text on Neo4j episode nodes; the nodes stay as provenance |
| Context traces | removes older traces and their items |
| Model call log | removes older rows (the log never held content) |
| Unconfirmed memories | expires an active memory nobody confirmed for N days (kept, not served) |
| Backup retention | shown on deletion receipts as when backups stop holding deleted data |

Expiry of content runs as a **deletion request of kind `retention`** through the purge gate: it has a receipt (Deletions) and an audit entry (`deletion.completed`, kind `retention`). An interaction whose raw form expired says **raw content expired** on its page.

Operate
- The worker sweeps on a timer (`RETENTION_SWEEP_SECONDS`); run it once by hand with `kg deletions retention-sweep`.
- Everything is idempotent: an interrupted sweep is simply run again.
- Lowering a window deletes data permanently at the next sweep: raise it back before then if it was a mistake. Raising it never brings data back.

Egress (same page): you can **deny** classes for this workspace; you cannot allow what the platform denies. A change reaches the worker within `WORKSPACE_EGRESS_TTL_SECONDS` (30 s by default).
