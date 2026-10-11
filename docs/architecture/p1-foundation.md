# Phase 1 architecture: runtime, contracts, auth

Plan: [phase-1-foundation.md](../plans/phase-1-foundation.md). This note records what was built and why. Tests named here live in `tests/` and `packages/*/tests`.

## Request lifecycle

```mermaid
sequenceDiagram
  participant C as Client (agent / app / admin)
  participant M as RequestMiddleware
  participant G as guard (route policy)
  participant A as Authenticator
  participant S as tenant_session
  participant DB as Postgres (kg_app, RLS)
  C->>M: request (+x-request-id?)
  M->>M: request_id / correlation_id (UUID, else new UUIDv7); 1 MiB body limit
  M->>G: route matched
  G->>G: policy for this endpoint? none => 500 (also fails startup)
  G->>A: public routes skip; others authenticate
  A->>DB: auth.lookup_credential(prefix) / auth.credential_status(key_id, principal_id) (SECURITY DEFINER)
  A-->>G: Principal (workspace from the key record, never the request)
  G->>G: requires(action)? role matrix; deny => audit + 403
  G->>S: handler dependency tenant_db
  S->>DB: BEGIN; set_config('app.workspace_id', ws, true); queries with explicit workspace_id predicate
  S-->>C: response (+x-request-id, x-correlation-id); COMMIT; pool resets on return
```

## Tenant isolation, in three layers
1. **Principal:** the workspace comes only from the API-key record or a verified JWT. `X-Workspace-Id`, `?workspace_id=` and bodies are never read (`T-AUTH-HDR`). A JWT is re-validated against the database on every request, so revoking a key or principal, or suspending a workspace, stops it at once (`T-AUTH-REVOKE`).
2. **Application predicate:** every repository query carries `WHERE workspace_id = :ws`; a resource in another workspace is a `404` indistinguishable from one that does not exist (`T-SEC-01`). The principal is held in the per-request ASGI scope, not `request.state`: some servers hand every request the same `scope["state"]` dict, which leaked an identity between requests during development (regression test `test_requests_never_share_identity_across_sequential_calls`).
3. **Row-level security (backstop):** `ENABLE` + `FORCE` on every tenant table, policy on `current_workspace_id()` which maps a missing or empty setting to NULL, so no workspace means no rows. The setting is transaction-local and set as the first statement of an explicit transaction (`T-DB-02` proves a pooled connection never carries tenant A into request B). The application role cannot bypass RLS and refuses to start otherwise (`T-DB-01`).

Neo4j Community has one database, so `group_id = ws-<workspace uuid>` is the boundary. `kg_graph` exposes only `ScopedGraph(ctx)`: it always passes `group_ids`, re-checks `group_id` after by-uuid fetches, and refuses Cypher without `$group_id` (`T-GR-01..04`). Importing `graphiti_core` anywhere else fails `make lint`.

## Role to action matrix (P1)
| Role | workspace.read | principal.read | audit.read | operations.read | diagnostics.run |
|---|---|---|---|---|---|
| workspace_admin | yes | yes | yes | yes | yes |
| operator | yes | | | yes | yes |
| reviewer / app_writer / agent / reader | yes | | | | |

Every route must be decorated `@requires(action)`, `@authenticated(reason)` or `@public(reason)`. `check_route_policies` runs at startup and in `T-REG`; a route without a policy stops the app from starting. Scope ACLs (`principal_scopes`) are stored but not enforced until P7.

## Router traffic
```
ModelGateway.complete/embed ─┐
Graphiti (OpenAIGenericClient,├─► httpx.AsyncClient(transport=RouterTransport) ─► mock-router | live router
          OpenAIEmbedder)    ─┘   egress check (fail closed) · per-service key · x-request-id
                                  metadata/routing injection · CallRecord -> model_calls · metrics
```
`RouterTransport` is the only way to the router, so Graphiti's own OpenAI client gets the same egress policy, key, metadata and call log as the gateway (`T-MG-01/02`). A request with no egress context is refused before it is sent. `local_only` is never sent. `model_calls` rows hold metadata only (`T-MG-04` fuzzes sentinel strings through the gateway and asserts they never reach the database or the logs).

## Database
`0001_foundation`: `workspaces`, `principals`, `role_assignments`, `principal_scopes`, `api_keys`, `audit_log` (append-only: update/delete/truncate raise, even for the owner), `model_calls`, `worker_heartbeats`. Composite foreign keys `(workspace_id, principal_id)` make cross-workspace links impossible at the schema level. `api_keys.key_hash` is excluded from `kg_app`'s column privileges.

## Observability
Structured JSON logs go through one redaction processor (deny-listed keys at any depth, strings truncated to 256 characters, exception messages dropped because they can echo content). The workspace appears only as an HMAC hash. Prometheus labels carry no identifiers (a test audits the registry); the HTTP route label is the route template. OpenTelemetry exports only when `OTEL_EXPORTER_OTLP_ENDPOINT` is set and span attributes pass the same deny-list.

## Package rules
`kg_common` <- `kg_domain`, `kg_obs` <- `kg_db` <- `kg_governance`; `kg_modelgw` <- `kg_graph`; apps depend on packages; nothing imports `kg_api`. Enforced by import-linter (`make lint`), including: `graphiti_core` only in `kg_graph`, `openai` only in `kg_modelgw` and `kg_graph`, production code never imports `tools` or `spikes`.

## Deviations from the plan (and why)
- **Admin dev port 5273**, not 5173: 5173 and 5174 are in use by other projects on this machine.
- **`kg_owner` has `BYPASSRLS`:** the `SECURITY DEFINER` auth functions must see credentials before any workspace is known, and `FORCE ROW LEVEL SECURITY` would otherwise hide every row from the function owner. The role is only used for migrations.
- **`api_keys` column privileges:** `kg_app` cannot select `key_hash` at all (stronger than the plan's "SELECT through RLS").
- **`kg openapi export` shells out to `python -m kg_api.openapi_export`:** the CLI may not import `kg_api` under the package rules.
- **FastAPI 0.143 wraps included routers lazily,** so `app.routes` no longer lists API routes; `kg_api.routers.ALL_ROUTERS` is the single registry the policy check walks.
- **Demo step 4:** the plan lists `agent_other -> GET /v1/principals -> only other_demo principals` and also says agents lack `principal.read` (step 5). The demo uses `admin@other` for step 4 so both statements hold.
- **Neo4j driver:** Graphiti's `Neo4jDriver` builds indices at construction, so `kg_graph` uses a subclass that cancels that task; only the worker builds indices (L12).
- **Admin stack majors:** React 18, Vite 6, Tailwind 3, Vitest 3, ESLint 9 and TypeScript 5.9 were used although newer majors exist. Revisit at P9.
