# ADR-029: Curated memory: model, scopes and where visibility is enforced

**Status:** Accepted  **Decision refs:** ADR-013 (embeddings), ADR-019 (exact scoped search, RLS backstop), ADR-024 (keyed hashes), ADR-025 (outbox), ADR-027 (authority)  **Evidence:** `packages/memory/tests`, `tests/integration/test_p5_memory.py`, `tests/integration/test_db_p5.py`, `tests/security/test_memory_leaks.py`, `tests/security/test_p5_isolation.py`, demo-p5

## Context
Curated memory is a governed derivative of the interaction ledger (blueprint 02-domain/03, 04): agent-ready statements with provenance, a lifecycle and a visibility, stored beside the temporal graph and not inside it. Two things are easy to get wrong: letting a model or an agent decide who may see a memory, and filtering visibility *after* a search has already ranked everything.

## Decision
1. **Postgres is the system of record.** `curated_memories` (immutable content: a change is a supersession), with `memory_scopes`, `memory_subjects`, `memory_evidence`, `memory_lineage` and a separate, versioned `memory_embeddings` (`vector(384)`, `CHECK vector_dims = embedding_dim`, **no ANN index**). Content is plaintext (curated, minimised, scope-protected, never logged) so lexical search works; raw interactions stay encrypted (ADR-024).
2. **Four types** (`working`, `episodic`, `semantic`, `procedural`), **five scope kinds** as visibility (`workspace`, `subject`, `project`, `agent`, `session`), **seven statuses**. A memory may carry several scopes; scopes never inherit from one another (`project:*` reveals nothing that carries only a `subject:` scope).
3. **Visibility is a SQL predicate inside the query that reads or ranks.** `ScopeGrants` is loaded once per request from `principal_scopes` and `visibility_sql` embeds an `EXISTS` over `memory_scopes` with bound parameters. RLS stays the workspace backstop; the per-principal scope check is application SQL (a per-principal RLS predicate is a P9 option).
   * `workspace:*` is implicit for `memory.read`; `agent:<principal>` and `session:<principal>/<run>` are implicit and private to that principal; `subject:` and `project:` come from explicit grants (`kg scopes`), managed by seed and CLI until P7 adds the API.
   * An admin may reveal other principals' private memory only with `memory.read_private`, explicitly per request, **audited in the same transaction**.
   * A classification ceiling applies to every read: agents and apps read up to `confidential`; `restricted` needs a `classification:restricted` grant; admins and reviewers read every class.
4. **A principal may only write scopes it can read** (`InvalidScope`, one answer for "does not exist" and "not yours"). Agents never save shared memory; they **propose**, and policy decides (ADR-030).
5. **An invisible memory is a 404 identical to a random id**, for reads and for every verb on `/{id}`; list endpoints and counts (`candidates_considered`, the `memories` count on an entity) cover **authorised rows only**.
6. **Writes are ledger commands** (`memory.*`): idempotent by `Idempotency-Key` (the principal is part of the request hash, so another caller cannot replay or learn an answer), audited with a closed metadata model, and **not listed** by `/v1/commands` (their visibility depends on scopes that endpoint does not know).
7. **Duplicates reinforce, they do not multiply.** `candidate_hmac` = keyed hash of (type, normalised content, sorted subjects, **sorted scopes**), unique among `proposed|active`. An independent source adds an evidence row to the existing memory. Including the scopes means the same words in a private and a shared scope are two memories and a duplicate check can never reveal a memory the writer could not already see.
8. **Working memory** is agent/run scoped with a TTL; its expiry is a **delayed outbox job** enqueued in the same transaction (`available_at` = deadline, dedupe `memory:{id}:expire:g1`). Search also excludes `expires_at <= now()`, so a note disappears at its deadline whether or not the job has run.

## Consequences
- Private memory is invisible to peers, curators and counts by construction, not by filtering; the EXPLAIN test pins the predicate inside the candidate scan.
- `principal_scopes` is now enforced for memory; P7 adds the grant/revocation API and the console.
- Exact scoped search is the only retrieval path until the size trigger in ADR-031.
