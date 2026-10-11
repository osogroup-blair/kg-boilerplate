# ADR-011: Pinned stack

**Status:** Accepted  **Decision refs:** DEC-01  **Evidence:** GR-01, PV-01..04, MR-01, G-01..04 · `compat/versions.lock.yaml`

## Context
The blueprint left Graphiti, Neo4j and Pydantic versions open until compatibility had been tested.

## Decision
Pin the combination in `compat/versions.lock.yaml`, verified together by the Phase 0 offline run:
Python 3.12.15, **graphiti-core 0.30.2**, **Neo4j 5.26.31 Community + APOC 5.26.31**, **PostgreSQL 17.11 + pgvector 0.8.7**, **Pydantic 2.14.0**, openai SDK 3.29.0, FastAPI 0.143.0, psycopg 3.3.6. Docker images are pinned by digest. Python packages are locked exactly (`spikes/uv.lock`); P1 copies the pins into its own lock file.

## Consequences
- Graphiti is the least stable dependency (APIs we hit that a minor upgrade could move: `group_id` now selects a database on drivers that implement `clone`, `add_episode(uuid=)` means "continue an existing episode", `search_` defaults to the cross-encoder recipe, `Graphiti.search` mutates a shared recipe). All Graphiti use goes through one adapter package (`packages/graph`, P1) whose contract tests are the GR-* checks, ported. Upgrading Graphiti means re-running them.
- Neo4j Community has no multi-database (GR-04), which fixes the tenancy model (ADR-015).
- pgvector 0.8.x is required only if ANN is ever enabled (ADR-019).

## Open
- None blocking. Review pins at each phase start; run `make spike-offline` first.
