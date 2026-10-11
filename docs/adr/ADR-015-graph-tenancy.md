# ADR-015: Graph tenancy and namespace mapping

**Status:** Accepted for MVP/dev; production isolation level deferred to a security decision  **Decision refs:** DEC-06, SEC-01  **Evidence:** GR-04, GR-08, PV-02

## Context
Shared database with `group_id` versus a database per tenant.

## Decision
1. **Shared database, `group_id` = one graph partition per workspace**, mapped as `group_id = "ws-<workspace uuid>"`. This is forced on Neo4j Community: `CREATE DATABASE` is unsupported (`UnsupportedAdministrationCommand`) and the Neo4j driver's `clone()` is a no-op, so `group_id` never selects a database. Allowed charset is `^[A-Za-z0-9_-]+$` (hyphenated UUIDs are valid).
2. **Isolation is only as strong as the adapter** (GR-04). With scoped calls nothing crosses groups (search, node/edge/episode listing). These calls are **not** scoped and are therefore forbidden outside the adapter:
   - `Graphiti.search(...)` / `search_(...)` with `group_ids=None` (searches every group);
   - `get_by_uuid` for nodes/edges/episodes (returns any group's object);
   - `group_id=None` on writes (silently becomes the `''` default partition).
3. **Adapter rules (`packages/graph`, P1/P4):** derive `group_id` from the authenticated workspace only; reject None/empty; always pass `group_ids=[group_id]`; after any by-uuid fetch assert `obj.group_id == expected`; negative tests for each trap above run in CI.
4. **Defence in depth is Postgres RLS (ADR-019), not Neo4j**: Community has no per-database or role-based graph security.
5. **Whole-tenant purge:** `clear_data(driver, [group_id])` removes one group only (GR-08).
6. **Production:** if per-tenant isolation guarantees stronger than adapter-enforced `group_id` are required, the options are Neo4j Enterprise (database per tenant, Graphiti's `clone` path) or one Neo4j instance per tenant. Record that decision against the first regulated customer; the adapter boundary makes it a swap, not a redesign.

## Consequences
Graph reads must go through the adapter; scope (subject/agent) rules beyond workspace are applied in Postgres/ledger and as Cypher predicates on edge/episode references, never by filtering after retrieval.
