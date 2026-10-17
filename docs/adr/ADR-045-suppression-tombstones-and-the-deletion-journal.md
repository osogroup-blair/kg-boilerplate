# ADR-045: A deleted identity is remembered as hashes, and the journal outlives the database

**Status:** Accepted  **Decision refs:** SEC-05, AT-16, AT-18 groundwork, phase-8 plan §5.4.3  **Evidence:** `tests/integration/test_p8_deletion.py`, `tests/security/test_deletion_residuals.py`, `tests/integration/test_p8_cli.py`

## Context
Deleting a person must not be undone by the source system re-sending them, and a restored backup must forget again. Both need a memory of the deletion that holds no personal data.

## Decision
1. **Suppression.** For each strong identifier of a deleted entity the request stores an HMAC (`value_hmac`, keyed by the content secret; prefix shown in the console). Creating or updating an entity, or resolving an interaction subject, with a suppressed identifier is refused (`409 subject_suppressed`). An admin may lift it (`POST /v1/suppressions/{prefix}/lift`, audited). A re-recorded suppression resets only `lifted_*`.
2. **Tombstones.** One append-only row per deleted entity, fact, interaction and memory: kind, id, request, time. No content, no name.
3. **The journal.** `deletions.jsonl` in `DELETION_JOURNAL_DIR`, outside the database and its backups, one line per finished request: request id, workspace, kind, completion time, the ids deleted and the suppression HMACs. Each line carries the HMAC of the previous one, so removing, editing or reordering a line is detected by `kg deletions journal` (`verify`). Production requires the directory to be set (a separate append-only volume).
4. **Backup contract for P9.** A restore from a backup taken at *T* re-applies every journal line after *T* before serving traffic: it deletes the listed ids and reloads the suppressions. `kg deletions journal --since T` lists those lines; `kg deletions reapply` is P9's.

## Consequences
- A name never reaches the journal, so the journal can sit on weaker storage than the database.
- Suppression is a deliberate friction: a returning person needs an explicit lift, which is itself audited.
