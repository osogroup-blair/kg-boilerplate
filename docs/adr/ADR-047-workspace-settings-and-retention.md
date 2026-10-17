# ADR-047: Workspace settings are versioned and can only narrow egress; retention is a deletion

**Status:** Accepted  **Decision refs:** SEC-06, P8 plan §5.6, ADR-044  **Evidence:** `tests/integration/test_p8_settings.py`, `tests/security/test_egress_matrix.py`, `tests/security/test_injection_corpus.py`, `tests/security/test_deletion_residuals.py`

## Decision
1. **Versioned, optimistic, audited.** `workspace_settings` is append-only (`version`); `PUT` needs `If-Match` of the version read and an idempotency key, writes a `settings.update` command and an audit entry; the history is readable.
2. **Egress can only be narrowed.** The setting is a *deny list* unioned with the platform's own. There is no field that allows a class; `extra = forbid` and the transport (`WorkspaceEgress`) refuse any class either list names. A lookup failure denies (fail closed). The cache window is `WORKSPACE_EGRESS_TTL_SECONDS` (30 s; a change through a process is seen by it at once).
3. **Retention is a deletion.** The sweep asks `ops.retention_candidates()` (workspace ids and counts only) and carries each class out per workspace: raw / normalized content as a `retention`-kind request through the purge gate with a receipt and audit entry; episode text blanked in Neo4j (the node stays as provenance); model-call rows removed; unconfirmed memories expired (kept, not served). Defaults keep everything.
4. **Visibility.** An interaction shows `raw_content_expired` / `normalized_content_expired`; replay of an interaction whose normalized form expired is not possible and is flagged.

## Consequences
- A tenant cannot weaken the platform's egress policy by configuration, only by the platform's own change control.
- Retention failures are repeatable: every class is idempotent.
