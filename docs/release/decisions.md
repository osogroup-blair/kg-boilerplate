# Open production decisions register (DEC-01 … DEC-12)

Status after phase 9. "Owner" items need a person's value before production; the platform ships a safe default for each and says so.

| DEC | Question | Status | Where it is settled |
|---|---|---|---|
| DEC-01 | Exact Graphiti, Neo4j and Pydantic versions | **Closed** | ADR-011; `compat/versions.lock.yaml` (`refreshed_on`); upgrade procedure in `docs/runbooks/upgrade-pin-dependencies.md` |
| DEC-02 | Model providers; where private data is processed | **Closed once the live session is applied** | ADR-012 (Provisional until `make live-session-apply` flips it to Accepted); the egress policy and per-workspace denies (ADR-047) are enforced and tested now |
| DEC-03 | Identity provider | **Closed** | OIDC for people (ADR-049), API keys for agents/apps, break-glass flag; `docs/runbooks/oidc-setup.md` |
| DEC-04 | Representing corrected extracted edges | **Closed** | ADR-018, ADR-041 |
| DEC-05 | Runtime Pydantic classes from the ontology | **Closed** | ADR-014 (allow-listed typed classes + validated mapping) |
| DEC-06 | Graph tenancy: shared database + `group_id` vs a database per tenant | **OPEN production decision** | See below |
| DEC-07 | Source retention and backup windows | **Owner-set** | Retention is configurable per workspace (ADR-047). Platform defaults: backups kept `BACKUP_RETENTION_DAYS=30` (always the newest 3); raw events minimised; deleted data returns after a restore for **at most the interval between a deletion and the next restore**, and `kg deletions reapply` removes it again. **Owner: set the retention and the maximum backup age they accept, then record it here.** |
| DEC-08 | Embeddings for all records or selective | **Closed** | ADR-013 (searchable active curated memories only) |
| DEC-09 | Which fields agents may treat as procedural instructions | **Closed** | ADR-030, ADR-038 (published preferences only) |
| DEC-10 | UI framework and visualisation | **Closed** | React 18 + TypeScript + TanStack Query + Cytoscape (`apps/admin/package.json`; exact versions in `package-lock.json`) |
| DEC-11 | p95 and freshness SLOs | **Owner-set from the baseline** | Proposals in `eval/reports/baseline-<date>.md`; the alert thresholds in `deploy/observability/rules.yml` marked `SLO (proposal)` change with the owner's values. **Owner: record the values here.** |
| DEC-12 | Bitemporal point-in-time guarantee | **Closed for MVP** | Valid-time and record-time metadata are kept and queryable (`as_of`, `known_at`); a formal point-in-time *guarantee* (an SLA over history rewrites) is out of scope and documented |

## DEC-06 — graph tenancy (open production decision)
- **Today:** one Neo4j database, `group_id` per workspace, derived from the authenticated workspace only and never accepted from a client; every graph result is re-checked against Postgres ACLs; leak suites and route walkers cover it. Neo4j Community Edition cannot create databases.
- **Options:** (1) keep shared database + `group_id` (cheapest; isolation is application-enforced); (2) one Neo4j **instance** per tenant (strong isolation; operations scale with tenant count); (3) Neo4j Enterprise multi-database (strong isolation, licence cost).
- **Triggers to move off (1):** a regulated or contractual isolation requirement from a tenant; any cross-tenant graph finding in the sweep or in production; a tenant that needs its own backup/retention/region.
- **Owner:** the platform owner decides with security. **Until then** the threat model carries it as accepted finding F-02.

## Other decisions recorded in phase 9
| Topic | Decision |
|---|---|
| Communities (V12) | **Stay disabled.** Their value cannot be measured offline (the mock extraction has no community structure); revisit after a live-session measurement. |
| ANN index (V9) | **Exact scan until a measured trigger.** The size curve (S/M/L) in the baseline report records context p95 against memories per workspace; add HNSW when the L-size p95 exceeds the proposed SLO (ADR-019 keeps the exact scoped search as the safe default). |
| Per-principal RLS | **Not adopted** (ADR-048). Workspace-level RLS + application ACLs. |
| Content-reader database role | **Not adopted** (ADR-048). |
| LISTEN/NOTIFY outbox dispatch | **Kept as polling.** Cache invalidation uses NOTIFY (ADR-050); job dispatch stays on `SKIP LOCKED` polling unless the baseline's queue-wait numbers say otherwise. |
| Production target | Single VM + Docker Compose reference (ADR-053); Kubernetes is not provided. |
