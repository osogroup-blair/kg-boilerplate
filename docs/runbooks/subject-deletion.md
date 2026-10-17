# Runbook: deleting a person (or an interaction, memory or principal's data)

Deletion is a request, not a button press: it **freezes** the target at once (every read answers 404), then a job chain purges every store, verifies, and writes a **receipt**. Read [ADR-044](../adr/ADR-044-deletion-lifecycle-purge-gate-and-verification.md) once; this page is the procedure.

## Before you start
- Who may ask: a workspace **admin** for any kind; a **trusted-source app** for a subject it can see or an interaction it submitted (the end subject's identifier goes in `on_behalf_of`, kept only as an HMAC); a **reviewer** for a memory.
- Is a **legal hold** in place? A held target is frozen but not purged ([legal-hold](legal-hold.md)).
- Deletion is permanent. What stays is only what the receipt lists (the ledger rows with redacted payloads, audit ids and codes, tombstones).

## Requesting
Console: **Deletions → Request a deletion…** (kind, the id, reason code, optional legal basis, acknowledgement). API: `POST /v1/deletion-requests {kind, target_id, reason_code}` → `202`, `Location: …/{request_id}`. A workspace deletion also needs `confirm_slug` and waits out the cooling-off.

From that commit: the entity, its facts, mentions, memories, linked interactions, search hits, graph neighbours, context lines and trace entries are gone from every read, for every role. Check it: `GET /v1/entities/{id}` is `404`, identical to a random id.

## Following it
Deletions → the request. States: `frozen → planned → purging → verifying → completed`. The page polls while it runs.
| State | Meaning | You do |
|---|---|---|
| `completed` | verified clean; the **receipt** is shown | keep the receipt; note `backup_copies_expire_by` |
| `completed_with_exceptions` | something needs a person | work the exceptions below |
| `blocked_legal_hold` | a hold covers the target | release the hold, or leave it |
| `failed` | a step failed after retries (the code is shown) | **Retry…** (operator or admin): it resumes where it stopped |

## Exceptions (`deletion_exception` in the Review Queue)
- **A memory about someone else that names the person** (`shared_memory_mentions_subject`): open the memory (the wording is not shown on the item), then decide: **Redact** (give replacement wording; the original is removed for good), **Delete** the memory, or **Retain** with a reason (e.g. a hold). **Check again** re-runs the verification.
- **Residual text/ids** after the purge (`residual_text`, location only): find it in the named store; if it is a derivative the plan missed, report it (this is a defect). **Delete** adds the item to the plan and re-runs.
When every exception is decided and verification is clean the request becomes `completed`; the manifest (the names it searched for) is deleted then.

## Afterwards
- The identifier is **suppressed**: the source system cannot recreate the person (`409 subject_suppressed`). If the person asks to be known again, Deletions → Suppressions → **Lift…** (audited).
- Backups: the receipt says when copies stop holding the data. A restore re-applies the journal ([ADR-045](../adr/ADR-045-suppression-tombstones-and-the-deletion-journal.md)): `kg deletions journal --since <backup time>` lists those lines; `kg deletions journal` verifies the chain.
- CLI: `kg deletions list|show --workspace <ws>`; `kg deletions retry --workspace <ws> <id>`.

## If something looks wrong
- Still readable after the `202`: a defect in a read surface; capture the route and report it (the read-block suite covers every route).
- Stuck in `purging`: `kg deletions show`; the worker log names the stage; retry is safe.
- Wrong target: only a **workspace** deletion can be cancelled (in its cooling-off: `kg deletions cancel`). Any other kind freezes at once and purges next; there is no undo, so check the id (open the entity or memory first, in another tab) before confirming.
