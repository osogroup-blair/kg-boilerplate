# ADR-023: Succession vs supersession, and source vs platform record time

**Status:** Accepted  **Decision refs:** KGR-01..05, ADR-017, ADR-018  **Evidence:** `tests/integration/test_p2_commands.py` (`test_kgr03_04_sarah_evt_001_and_evt_002`), `packages/ontology/tests/fixtures/rules/succession.yaml`

## Decision
- **Succession** is how a chain of true facts grows. Asserting `WORKS_AT Sarah→Acme, Engineering Manager, valid_from 2024-06-01` while a Software Engineer fact is open since 2022-01-10 **ends** that fact at 2024-06-01 (`valid_to` set, `succeeded_by_fact_id` set). The predecessor stays `status=active`: it is still a true historical fact, and `as_of(2023-01-01)` still answers "Software Engineer".
- **Supersession** (`status=superseded`, `superseded_at` set) means the platform stopped believing a fact. It is a correction (P8). P2 never supersedes; `superseded_at` is always null.
- Rules for the chain: a new fact before an existing one is `out_of_order_succession` unless it is closed and fits the gap; a fact that would sit inside another is `succession_conflict`; two facts starting at the same instant are `succession_conflict`. Concurrent assertions for one source serialise on a row lock, so the chain is always consistent (tested with eight parallel requests).
- **Three clocks, never confused.** `valid_from`/`valid_to`: when the fact is true in the world, supplied by the command, **never defaulted to now** (`valid_from_unknown` is explicit). `source_recorded_at`: when the source system learned it (evt_002: 2024-06-10), supplied by the command. `recorded_at`: when this platform stored it, the database's `now()` (never the host clock, never a request field).
- Reads: `as_of(T)` = `valid_from ≤ T < valid_to` (an unknown start counts as "always"); `known_at(T)` = `recorded_at ≤ T` and not superseded by `T`; the default is current = `as_of(now) ∧ known_at(now) ∧ active`.
- The graph edge mirrors the row: `valid_at = valid_from`, `invalid_at = valid_to`, `created_at = recorded_at`, `expired_at = superseded_at`. `created_at` is therefore the platform's record time, never the source date.

## Consequences
History is append-and-end: nothing is edited in place, so every earlier answer can be reproduced. A fix to a past fact is a P8 correction, not a command.
