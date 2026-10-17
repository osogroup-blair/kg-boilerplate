# Runbook: re-embedding memory (a new embedding model or version)

Vectors live in `memory_embeddings (memory_id, embedding_version)`. A search reads exactly one version (`MEMORY_EMBEDDING_VERSION`, default 1), so you can fill a new version while production keeps serving the old one, and switch when it is complete. Never compare vectors of different versions; never edit a vector in place for a different model.

1. **Write the new version** (resumable: only memories without that version are touched; a router outage just stops it, run it again):
   ```bash
   uv run kg memories reembed --workspace <ws> --to-version 2
   ```
   It prints `{"embedded": …, "remaining": …, "coverage": …}`. `local_only` memories are skipped by policy (they are lexical-only by design). Every run is audited (`memory.reembedded`).
2. **Check coverage is 1.0** for every workspace, and compare quality on the labelled set: `make eval-memory-live` with `MEMORY_EMBEDDING_VERSION=2` in the environment.
3. **Switch**: set `MEMORY_EMBEDDING_VERSION=2` on the API and the worker and restart both. New memories are embedded at version 2 from then on. A search shows `embedding_version` and `embedding_coverage`.
4. **Retire the old version** once nothing needs rollback: `uv run kg memories reembed --workspace <ws> --to-version 2 --drop-version 1` (deletes version 1 only if version 2 is at 100%).

**Rollback** before step 4: set `MEMORY_EMBEDDING_VERSION=1` again; nothing else changed. A different **dimension** (not 384) is a schema change (add a column, backfill, switch in one migration: PV-04 path B), not a version bump.

**When a memory shows `embedding_status = failed`** its embed job died (see [stuck-job-replay](stuck-job-replay.md)); it is still found lexically. Retry the job from Operations, or run `reembed` for its version.
