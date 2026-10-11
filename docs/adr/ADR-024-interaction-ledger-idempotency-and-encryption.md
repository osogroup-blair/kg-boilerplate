# ADR-024: Interaction ledger, idempotency and encryption at rest

**Status:** Accepted  **Decision refs:** ADR-016, ADR-019, ADR-020, ADR-021; blueprint `02-domain/03` (EVD-01..06), `03-runtime/02` (ING-01, ING-05, ING-06)  **Evidence:** `tests/integration/test_p3_interactions.py`, `packages/ingestion/tests`, `tests/security/test_p3_isolation.py`, demo-p3 steps 1–4, 8, 9

## Context
Every event the platform learns from must be stored once, be explainable later, and be protected: raw content is the most sensitive thing the platform holds, and the graph and memories derived from it come later (P4, P5). Graphiti does not deduplicate episodes (GR-09), so the ledger owns idempotency.

## Decision
1. **One immutable envelope per accepted event** (`interactions`), keyed `(workspace_id, source_system, idempotency_key)`. Only the processing status can change (column grants, plus a trigger that also binds the table owner).
2. **Only accepted events consume an `Idempotency-Key`.** A request that fails validation (4xx) creates no logical event, so its key stays free. This differs from commands, which record refusals, because a refused command is itself a decision worth auditing while an invalid event never existed as evidence.
3. **Same key + same body → `200` with the same `event_id` and the current status; same key + different body → `409 idempotency_mismatch`.** Concurrent duplicates are serialised with `pg_advisory_xact_lock` on the key; the unique index is the backstop.
4. **The request hash is a keyed HMAC** (`HMAC-SHA256` under a per-workspace key derived with HKDF from `CONTENT_HMAC_SECRET`), over the canonical validated request. The same keyed hash covers stored content, so a dictionary attack on low-entropy text is not possible from the database alone, yet per-workspace duplicate statistics still work. The HKDF master is **not** the payload key: rotating payload keys must not change request hashes.
5. **Raw and normalised content are AES-256-GCM encrypted at rest.** A keyring (`PAYLOAD_KEYS`, `kid:base64(32 bytes)`) with one active kid; a random 96-bit nonce per encryption; **associated data `workspace_id|interaction_id|form`**, so a ciphertext copied to another row or tenant fails authentication. Rows store `kid, nonce, ciphertext`. Production refuses the development key and an active kid missing from the ring.
6. **Plaintext leaves the platform through exactly one route**, `GET /v1/interactions/{id}/content` (`interaction.read_content`, admin only), which audits the read in the same transaction and answers `Cache-Control: no-store`. The grant model does not protect content (the application role can SELECT the ciphertext columns); the encryption does. A separate content-reader database role was considered and rejected for P3 (one more credential for little gain) and is a P9 hardening option.
7. **Metadata that may carry content** (`metadata`, unresolved external identifiers) is stored inside the encrypted raw form; unresolved identifiers keep only a keyed hash in `interaction_subjects` so P4 can match them later.
8. **Classification is `max(requested, workspace default)`** and is never lowered. It is policy metadata and is stored in the clear; content is not.
9. **Content is data.** No field of an event can influence stage selection, routing, classification, roles or policy. The normaliser reshapes text; it never interprets it.
10. **Rotation:** `kg payloads reencrypt --workspace <slug> --to <kid> --actor <handle>` re-encrypts in resumable batches inside tenant sessions and audits each batch.
11. **Seam for large originals:** `payload_ref` is accepted by the envelope and answered `422 pointer_not_supported` (CAP-18); object storage is a later phase.

## Consequences
- Losing every key that encrypted a row loses that row's content. The runbook makes key backup an explicit step and the ciphertext is bound to its row, so a restored backup cannot be spliced.
- Debugging content needs the audited reveal; the normalised form is also stored so a replay is deterministic.
- Idempotency scope is the source system: two systems may reuse the same key string safely.
- Validation errors report field paths only; no value is ever echoed (tests send sentinels through every failure path).
