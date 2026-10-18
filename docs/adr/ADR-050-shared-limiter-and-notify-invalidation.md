# ADR-050: One rate limit across replicas, and cache invalidation over LISTEN/NOTIFY

**Status:** Accepted  **Decision refs:** SEC-03, F-03 in `docs/security/threat-model.md`, phase-9 plan WP7  **Evidence:** `tests/integration/test_p9_shared.py`, `tests/resilience/test_p9_failure_injection.py` (FI-17)

## Context
Per-process limiters multiplied the budget by the replica count; per-process caches (workspace egress policy, quotas, graph namespace registry) let a revocation or a settings change lag on other replicas.

## Decision
- **Limiter:** counters and concurrency slots live in UNLOGGED Postgres tables behind SECURITY DEFINER functions (`ops.rate_hit`, `ops.slot_acquire/release`, `ops.rate_sweep`), using the **database clock**; `kg_app` has no grant on the tables. `LIMITER_BACKEND=postgres|memory`. Expensive calls (context builds) **fail closed** when the limiter's database is unreachable; cheap calls fail open (`LIMITER_FAIL_MODE`).
- **Invalidation:** triggers `pg_notify('kg_invalidate', '<workspace>:<kind>')` on the tables whose rows a cache depends on; every API and worker process runs one LISTEN connection (`Invalidator`) and drops the affected entries; the TTL stays as the fallback if the listener is down (`invalidation_listener_up`).
- **No broker.** Postgres is already the system of record and a hard dependency.

## Consequences
A revocation, a narrowed egress policy or a new quota reaches every replica in well under a second (tested at < 1 s). The limiter adds one cheap round trip per limited call. Job dispatch stays on polling (the baseline does not justify NOTIFY there).
