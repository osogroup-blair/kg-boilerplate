# ADR-017: Temporal field mapping and bitemporal scope

**Status:** Accepted for MVP  **Decision refs:** DEC-12, KGR-*  **Evidence:** GR-06

## Mapping
| Platform | Graphiti `EntityEdge` | Notes |
|---|---|---|
| valid_from | `valid_at` | from extraction, or the command |
| valid_to | `invalid_at` | a backdated successor sets its predecessor's `invalid_at` to its own `valid_at` (observed: 2022-01-10 → 2024-06-01) |
| recorded_at | `created_at` | **REQUIRED field**. `add_episode` stamps wall-clock now, **not** the event's recorded time; direct save or a post-write `SET e.created_at` carries the platform value (both shown) |
| superseded_at | `expired_at` | `add_episode` sets wall-clock now at invalidation |
| (episode) | `EpisodicNode.valid_at` / `created_at` | event time / wall clock |

## Decision
- **Postgres fact table is the system of record** for `recorded_at`, `superseded_at`, `known_at` and status; the graph carries valid-time and an overwritten `created_at` where cheap.
- `as_of(T)` = `valid_at <= T AND (invalid_at IS NULL OR invalid_at > T)` and `known_at(T)` = `created_at <= T AND (expired_at IS NULL OR expired_at > T)` both work as `SearchFilters` (OR across outer list, AND inside). `known_at` is only faithful when `created_at` holds the platform's recorded_at.
- MVP provides valid-time and record-time metadata and these two filters; a formal bitemporal point-in-time SLA stays out of scope.
- Unknown dates stay null: Graphiti leaves `valid_at` null when the text has no time (observed for CONSIDERING), and the platform does not default it to now.
