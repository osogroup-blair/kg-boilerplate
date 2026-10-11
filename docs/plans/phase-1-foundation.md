# Phase 1: Foundation (Runtime, Contracts, Auth) — Detailed Implementation Plan

**Roadmap:** [ROADMAP.md](../../ROADMAP.md), Phase 1 (blueprint Slice 0)
**Blueprint inputs:**
- `01-architecture/02` (components, data ownership)
- `03-runtime/01` (service contracts, API shape rules, package boundaries)
- `03-runtime/05` (topology, metrics)
- `04-administration/01` (admin principles)
- `04-administration/02` (authorization model, audit, SEC-01/02/03)
- `05-delivery/01` (Slice 0)

**Phase 0 inputs (binding):**
- ADR-011 (pins), ADR-012 (gateway and router contract), ADR-015 (graph tenancy), ADR-019 (RLS pattern), ADR-020 (auth, networking, tokenizer)
- `compat/versions.lock.yaml`, `compat/model-purposes.yaml`, `compat/pricing.yaml`
- `docs/router-contract.md`
- `tools/mock_router`
- Spike prototypes in `spikes/gateway`, `spikes/auth`, `spikes/tokenizer`, `spikes/graphiti/harness.py`

**Status (2026-10-11): implemented; all exit criteria met except WP0's `git init` (needs the user's approval).** Runs entirely against the mock router. Differences from this plan are listed in [docs/architecture/p1-foundation.md](../architecture/p1-foundation.md#deviations-from-the-plan-and-why); run it with [docs/runbooks/startup.md](../runbooks/startup.md).

---

## 1. Objective

Stand up the production skeleton that every later phase builds on:
- one uv-workspace Python monorepo (API and worker from one codebase) plus the React admin app;
- a reproducible Docker Compose stack;
- the security foundation: authenticated principals bound to a workspace, a deny-by-default authorization policy on every route, Postgres RLS as defence in depth, and append-only audit;
- the two external adapters (the router-backed `ModelGateway` and the tenant-guarded graph adapter) with health checks and contract tests;
- telemetry that never carries content;
- a versioned `/v1` OpenAPI with a generated TypeScript client;
- a seeded `demo` and `other_demo` workspace.

**Demo:** after `make up && make seed`, an agent in `demo` sees its own workspace, principals and health. An agent in `other_demo` gets `404` for every `demo` resource, even with known IDs. A revoked key stops working on the next request.

**Gate:**
- a repeatable clean start (`make reset up seed demo-p1` twice gives identical results);
- **zero cross-workspace reads**, the first SEC-01 tests;
- every route has an authorization policy;
- no content in logs or metrics.

---

## 2. What Phase 0 Taught Us (and how P1 applies it)

| # | Phase 0 finding (source) | P1 decision |
|---|---|---|
| L1 | Superusers and `BYPASSRLS` roles ignore RLS even with `FORCE` (PV-02) | Two DB roles: `kg_owner` runs migrations and owns the tables; `kg_app` is `NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE` and is what the API and worker connect as. **The API and worker refuse to start** if their role can bypass RLS (startup assertion on `pg_roles`). |
| L2 | `conn.transaction()` on a non-autocommit psycopg connection is only a SAVEPOINT, so the workspace setting leaked to the next tenant (PV-02) | One pattern only: the SQLAlchemy async engine, `async with engine.connect() as c, c.begin():`, then `set_config('app.workspace_id', …, true)` as the first statement. Pool `reset_on_return="rollback"`. A **pooled-connection reuse test** is a release blocker. |
| L3 | Auth must find a key before the workspace is known, but RLS hides rows when no workspace is set (PV-02 fail-closed) | Key lookup goes through a `SECURITY DEFINER` function `auth.lookup_credential(prefix)`, owned by `kg_owner` with a pinned `search_path`. It returns only the fields auth needs. The app role has no direct `SELECT` on `api_keys` across workspaces. |
| L4 | Workspace must come from the key record or claim, never from a header or body (AU-01) | `Principal` is built only by `kg_governance.authn`. There is no `workspace_id` parameter on any route; `X-Workspace-Id` is ignored, and a test sends one. |
| L5 | Revocation must be immediate (AGT-05); a 15-minute JWT would outlive a revoked key | JWTs carry `key_id` and `principal_id`. **Every request re-checks key and principal status** with one indexed lookup (the JWT only saves re-hashing). The key and the JWT stop working on the very next request. |
| L6 | The router ignores a non-UUID `x-request-id` (router-contract §2) | `request_id` and `correlation_id` are **UUIDv7**. An incoming header that is not a UUID is replaced, and the original is logged as `client_request_ref`. |
| L7 | Graphiti calls the router through its own OpenAI SDK client, so it bypasses a gateway that only wraps `complete()`/`embed()` (harness.py) | Build the choke point at **HTTP-transport level**: `RouterTransport` (an `httpx.AsyncBaseTransport`). It applies egress, auth, metadata, request IDs and the call log to *every* router request, including Graphiti's. `ModelGateway` and the Graphiti factory both use it. |
| L8 | `local_only` must never reach the transport; denied classes become `skipped_by_policy` (G-02) | `RouterTransport` **fails closed**: a request with no classification in the egress context is refused before sending. This is tested with a spy transport. |
| L9 | Admin router keys expose provider/model headers and break attribution (ADR-012 §10) | Separate keys `OSO_ROUTER_API_KEY_API` and `OSO_ROUTER_API_KEY_WORKER`. The transport detects `x-gateway-provider` in a response and logs `router_key_is_admin` (error in `ENV=production`, warning in dev). |
| L10 | Graph tenancy is enforced only by the adapter: `search()` without `group_ids`, `get_by_uuid` and `group_id=None` leak (GR-04) | `kg_graph` exposes a `ScopedGraph` facade built from a `WorkspaceContext`, never from a string. The raw `Graphiti` object is private. `group_id = "ws-<workspace uuid>"`. The GR-04 trap tests are ported as contract tests. import-linter forbids `graphiti_core` imports outside `kg_graph`. |
| L11 | Graphiti's default cross-encoder needs logprobs (dropped by the router); its default client calls `/v1/responses` (404) (GR-02, GR-10) | The factory always builds `OpenAIGenericClient`, `NoCrossEncoder` (raises) and the `E5Embedder` wrapper (`query: ` prefix, 384 dims). A test asserts the wiring. |
| L12 | The Neo4j driver schedules index builds at construction, so an explicit build races it (GR-01) | Indices are built **once by the worker at startup** (tolerating "equivalent index already exists"). The API never builds indices. |
| L13 | The spike gateway imports `tools.mock_router.tokens` (production code coupled to a test tool) | `TokenCounter` (ADR-020) moves to `kg_common.tokens`. The mock router may import it, not the other way round. import-linter forbids `tools.*` imports from `packages/*` and `apps/*`. |
| L14 | Host ports 5432–5437 are taken on this machine; the spike stack uses 5543/7474/7687/8781 (spikes/README) | The P1 stack uses separate ports so both can run: Postgres **5544**, Neo4j **7475/7688**, mock router **8782**, API **8000**, admin **5273** (5173 and 5174 turned out to be taken too). |
| L15 | Containers reach a loopback-only router via `host.docker.internal` (NW-01) | Compose sets `extra_hosts: host.docker.internal:host-gateway`. Live mode uses `http://host.docker.internal:8780/v1`; mock mode uses `http://mock-router:8781/v1`. |
| L16 | `results_report.py` renders the static sections one character per line (`lines += STATIC_SECTIONS` at `spikes/results_report.py:76`) | Housekeeping WP0: change it to `lines.append(...)` and regenerate `docs/compatibility-report.md`. |
| L17 | The project is not a git repository | WP0: `git init` and a first commit of the Phase 0 artefacts. **The coding agent asks the user before committing.** |
| L18 | ADR-019's PV-01 latency (p50 2.5 / p95 9.7 ms) and the report (p50 9.0 / p95 63.2 ms) disagree (different runs) | Not blocking. P1 makes no latency claims. P9 measures baselines on a fixed seed. Note it in the ADR as "run-to-run variance; see P9". |

---

## 3. Scope

**In scope:**
- monorepo, toolchain and boundaries;
- Compose stack;
- migration `0001_foundation`;
- authentication (API keys and JWT), authorization (RBAC role→action matrix plus workspace binding), RLS, audit;
- request context, error envelope, `/v1` routes for identity, workspace, principals and health;
- `RouterTransport` and `ModelGateway` (promoted from the spike);
- `kg_graph` connection and `ScopedGraph` facade (health and guards only);
- worker skeleton with heartbeat and index bootstrap;
- telemetry (logs, metrics, optional OTel traces);
- OpenAPI export, TypeScript client;
- admin shell with Login, Dashboard and Agents & Access (read-only);
- CLI, seed, `demo-p1`, CI-equivalent `make check`.

**Out of scope (later phases):**
- ontology (P2), interactions and outbox (P3), Graphiti ingestion (P4), memory and pgvector tables (P5), context (P6);
- agent registration and revocation APIs, and scope ACL enforcement beyond the workspace (P7; the tables are created now);
- deletion (P8);
- OIDC (path documented only, ADR-020);
- production hardening (P9).

---

## 4. Repository Layout (end of P1)

```text
kg_boilerplate/
├── pyproject.toml              # uv workspace root: members = packages/*, apps/api, apps/worker, apps/cli; ruff, mypy, pytest, import-linter config
├── uv.lock                     # exact pins; versions copied from compat/versions.lock.yaml (ADR-011)
├── Makefile                    # up/down/reset/seed/demo-p1/test*/lint/typecheck/openapi/admin-client/check (+ existing spike targets)
├── .env.example                # extended (§11)
├── contracts/openapi.json      # exported, committed; drift-tested
├── deploy/
│   ├── docker/python.Dockerfile           # one image for api, worker and cli (python:3.12-slim by digest, uv sync --frozen --no-dev, non-root)
│   ├── compose/docker-compose.yml         # project name "kg"
│   └── compose/postgres-init/01-roles.sh  # creates kg_owner, kg_app, DBs kg + kg_test, extension vector
├── packages/
│   ├── common/        kg_common      # settings (pydantic-settings), ids (uuid7), clock, base errors, tokens (TokenCounter from TK-01)
│   ├── domain/        kg_domain      # public Pydantic contracts: Principal, WorkspaceContext, enums, ErrorEnvelope, Page[T]
│   ├── db/            kg_db          # async engine, tenant_session(), role assertion, Alembic env + migrations/versions/0001_foundation.py
│   ├── governance/    kg_governance  # authn (keys, JWT), authz (role→action matrix, policy decorator), audit writer, repositories
│   ├── modelgw/       kg_modelgw     # RouterTransport, ModelGateway, error categories, purposes/pricing loaders, call-log sink
│   ├── graph/         kg_graph       # Neo4j connection, GroupId, ScopedGraph facade, Graphiti factory (guarded), E5Embedder, NoCrossEncoder
│   └── observability/ kg_obs         # structlog config + redaction, request context vars, Prometheus metrics, OTel setup
├── apps/
│   ├── api/     kg_api      # FastAPI app factory, middleware, deps, routers (/health, /v1/...), exception handlers
│   ├── worker/  kg_worker   # process entrypoint: startup checks, index bootstrap, heartbeat loop, graceful shutdown
│   ├── cli/     kg_cli      # Typer: db upgrade|downgrade, seed demo, keys create|revoke|list, openapi export
│   └── admin/               # Vite + React + TS (§10)
├── scripts/demo_p1.py      # runs the demo assertions and prints a pass/fail table
├── tests/                  # cross-package: integration/, security/, contract/ (unit tests live next to each package)
├── tools/  (mock_router, verify_router — unchanged; the mock imports kg_common.tokens after L13)
├── spikes/ (unchanged; disposable)
└── docs/   (+ docs/runbooks/startup.md, docs/architecture/p1-foundation.md)
```

**Not created yet:** `packages/ontology|ingestion|memory|context` (each phase creates its own) and `sdk/`, `examples/`.

### Package dependency rules (enforced by import-linter in `make lint`)

```
kg_common      → (stdlib + third party only)
kg_domain      → kg_common
kg_obs         → kg_common
kg_db          → kg_common, kg_obs
kg_governance  → kg_domain, kg_db, kg_common, kg_obs
kg_modelgw     → kg_domain, kg_common, kg_obs, kg_db (call-log sink only)
kg_graph       → kg_domain, kg_common, kg_obs, kg_modelgw (for RouterTransport)
kg_api         → any package;  kg_worker → any package except kg_api;  kg_cli → any package except kg_api
```

**Forbidden imports:**
- `graphiti_core` outside `kg_graph`;
- `openai` outside `kg_modelgw` and `kg_graph`;
- `tools.*` and `spikes.*` from `packages/*` and `apps/*`;
- `kg_api` from anything else.

---

## 5. Work Packages

The order follows the dependency graph in §9. Each WP lists files, behaviour and tests. "DoD" means definition of done.

### WP0: Housekeeping and repository bootstrap
- **Ask the user, then** run `git init`, extend `.gitignore` (`.local/`, `apps/admin/node_modules`, `apps/admin/dist`, `*.env`) and make an initial commit of the Phase 0 artefacts (L17).
- Fix `spikes/results_report.py:76` (`lines += STATIC_SECTIONS` → `lines.append(STATIC_SECTIONS)`) and run `make spike-report` (L16).
- Add a note on PV-01 run variance to ADR-019 (L18).
- **DoD:** the report renders correctly; the repo is under version control (if the user approved).

### WP1: Toolchain and workspace
- Root `pyproject.toml` as a uv workspace. Each member has its own `pyproject.toml` with a `src/` layout. Python `==3.12.*`.
- **Runtime dependencies:** pin the ones Phase 0 verified, exactly as in `compat/versions.lock.yaml` (fastapi, starlette, uvicorn, sqlalchemy, alembic, psycopg, pydantic, httpx, pyjwt, tiktoken, graphiti-core, neo4j, openai, pyyaml).
- **New dependencies:** pin at install time and record them in `compat/versions.lock.yaml` under a new `p1_additions` key:
  - `pydantic-settings`, `structlog`, `prometheus-client`, `typer`;
  - `opentelemetry-sdk`, `opentelemetry-exporter-otlp`, `opentelemetry-instrumentation-fastapi|httpx|sqlalchemy`, `asyncpg` *or* psycopg async (choose **psycopg async** to reuse the verified driver);
  - dev: `ruff`, `mypy`, `import-linter`, `pytest-cov`, `pytest-asyncio`.
- `ruff` (lint and format) and `mypy --strict` on `packages/*`; `pytest` config with markers `unit`, `integration`, `security`, `contract`.
- import-linter contracts (§4).
- **DoD:** `uv sync --frozen` works; `make lint typecheck` passes on the empty skeleton.

### WP2: `kg_common`, `kg_domain`, `kg_obs`
- **`kg_common`:**
  - `settings.py`: `Settings(BaseSettings)` with groups `db`, `neo4j`, `router`, `auth`, `obs`, `env` (`dev|test|production`). Secrets are `SecretStr`. Validation: in production, `auth.pepper` and `auth.jwt_secret` must be set, ≥ 32 bytes, and not equal to the dev defaults.
  - `ids.py`: `uuid7()` (small local implementation with a test; no new dependency), `is_uuid()`.
  - `tokens.py`: move `TokenCounter` from `spikes/tokenizer/counter.py`, keeping the ADR-020 rule and tokenizer ID. Update `tools/mock_router/tokens.py` to import it (L13), and re-run `make test-mock`.
  - `errors.py`: `PlatformError(code, http_status, message, retryable=False, details=None)` and subclasses `NotFound`, `Unauthenticated`, `Forbidden`, `Conflict`, `ValidationFailed`, `DependencyUnavailable`.
- **`kg_domain`:**
  - `PrincipalKind = user|app|agent|service`;
  - `Role` (§6.3);
  - `Principal(principal_id, workspace_id, kind, handle, roles: frozenset[Role], key_id | None, auth_method: api_key|jwt)`;
  - `WorkspaceContext(workspace_id, group_id)`, constructible only from a `Principal`, via `WorkspaceContext.of(principal)`;
  - `ErrorEnvelope{error:{code,message,request_id,retry_after?,details?}}`;
  - `Page[T]{items, next_cursor}`;
  - response models for `/v1` (§7).
- **`kg_obs`:**
  - `context.py`: contextvars `request_id`, `correlation_id`, `principal_id`, `workspace_hash` (HMAC, never the raw ID in logs), `service`.
  - `logging.py`: structlog JSON. The **redaction processor** drops keys in a deny-list (`content, messages, input, text, payload, prompt, body, embedding, authorization, api_key, password, token`) at any depth and truncates other strings to 256 characters.
  - `metrics.py`: Prometheus registry and these metrics:
    - `http_requests_total{route,method,status}`, `http_request_duration_seconds{route,method}`;
    - `auth_failures_total{reason}`, `authz_denials_total{action}`;
    - `router_calls_total{purpose,alias,operation,outcome}`, `router_tokens_total{purpose,direction}`;
    - `worker_heartbeat_unixtime`, `dependency_up{dependency}`.

    **No label may carry a workspace, principal or resource identifier**, and a test enforces this.
  - `tracing.py`: OTel providers. The OTLP exporter is enabled only if `OTEL_EXPORTER_OTLP_ENDPOINT` is set. Span attributes go through the same deny-list.
- **Tests:**
  - uuid7 monotonicity and format;
  - settings production validation;
  - redaction (log a nested payload containing `content` → absent in the output);
  - metrics label audit (iterate the registry and assert there is no label named `workspace*|principal*|*_id` except `route`).

### WP3: Database foundation (`kg_db`) and migration `0001_foundation`
- **`deploy/compose/postgres-init/01-roles.sh`:**
  - creates `kg_owner` (LOGIN, owns DBs `kg` and `kg_test`) and `kg_app` (LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE);
  - `CREATE EXTENSION IF NOT EXISTS vector` (unused until P5, but proves the image);
  - passwords come from env.
- **`engine.py`:**
  - `create_app_engine(settings)` (psycopg async, `pool_pre_ping`, `reset_on_return="rollback"`);
  - `assert_role_is_safe(engine)` runs `SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user` and raises `UnsafeDatabaseRole` (L1).
- **`session.py`:**
  - `tenant_session(ctx: WorkspaceContext, principal_id)` is an async context manager. It opens `engine.connect()` and `begin()`, then runs `SELECT set_config('app.workspace_id', :ws, true), set_config('app.principal_id', :p, true)` and yields the connection or session. It commits on success and rolls back on error (L2).
  - `system_session()` is for the worker heartbeat and auth lookups only: no workspace set, so RLS tables return nothing.
- **Alembic:** env in `kg_db/migrations`. It connects as `kg_owner` (`DATABASE_URL_OWNER`). It refuses to run as `kg_app`.
- **`0001_foundation`** (all IDs `uuid`, all timestamps `timestamptz`; `created_at default now()`):

| Table | Key columns | Notes |
|---|---|---|
| `workspaces` | `id`, `slug` unique, `display_name`, `status` (`active\|suspended`), `graph_group_id` generated `'ws-' \|\| id::text` stored unique | RLS: `id = current_setting('app.workspace_id', true)::uuid` |
| `principals` | `id`, `workspace_id` FK, `kind`, `handle` (unique per workspace), `display_name`, `status` (`active\|revoked`), `owner_principal_id` null, `revoked_at` | RLS by `workspace_id` |
| `role_assignments` | `principal_id` FK, `workspace_id`, `role`, `granted_by`, `granted_at`; PK `(principal_id, role)` | RLS |
| `principal_scopes` | `id`, `principal_id`, `workspace_id`, `scope_kind` (`workspace\|subject\|project\|agent\|session`), `scope_ref`, `actions text[]` | Created now, enforced in P7. RLS |
| `api_keys` | `id`, `workspace_id`, `principal_id`, `prefix` unique, `key_hash`, `created_by`, `expires_at`, `revoked_at`, `last_used_at` | RLS. **`kg_app` has SELECT only through the RLS policy**; lookup goes through the function below |
| `audit_log` | `id` uuid7, `workspace_id`, `actor_principal_id`, `action`, `resource_type`, `resource_id`, `outcome` (`allowed\|denied\|error`), `request_id`, `correlation_id`, `metadata jsonb` (IDs, counts, hashes only), `created_at` | **Append-only:** `kg_app` gets INSERT and SELECT only, and a `BEFORE UPDATE OR DELETE` trigger raises. RLS |
| `model_calls` | `id`, `workspace_id` null (system calls), `purpose`, `alias`, `operation`, `classification`, `correlation_id`, `router_request_id`, `prompt_tokens`, `completion_tokens`, `usage_estimated`, `est_cost_usd`, `latency_ms`, `attempts`, `outcome`, `fallback_used`, `tokenizer`, `service`, `created_at` | Metadata only (ADR-012 §6). RLS with the policy `workspace_id IS NULL OR workspace_id = current` |
| `worker_heartbeats` | `worker_id` PK, `hostname`, `version`, `started_at`, `last_beat_at`, `status` | Not tenant data, no RLS. `kg_app` gets INSERT and UPDATE |

- **Functions** (owned by `kg_owner`, `SECURITY DEFINER`, `SET search_path = pg_catalog, public`, `REVOKE ALL FROM PUBLIC`, `GRANT EXECUTE TO kg_app`) (L3):
  - `auth.lookup_credential(p_prefix text)` returns `(key_id, key_hash, principal_id, workspace_id, kind, handle, principal_status, key_expires_at, key_revoked_at, workspace_status, roles text[])`;
  - `auth.credential_status(p_key_id uuid, p_principal_id uuid)` returns the same status fields for the per-request JWT re-check (L5);
  - `auth.touch_key(p_key_id uuid)` updates `last_used_at`. The caller throttles it to once every 5 minutes per key in process.
- **RLS on every tenant table:** `ENABLE` and `FORCE`; a policy `USING (workspace_id = current_setting('app.workspace_id', true)::uuid)` with a matching `WITH CHECK`. When the setting is missing, `current_setting(..., true)` returns NULL and nothing matches (fail closed).
- **Grants:** `kg_app` gets `USAGE` on schemas, DML per the table notes, no DDL.
- **Downgrade** drops everything (greenfield).
- **Tests (integration, `kg_test` DB):**
  - **T-DB-01:** role assertion: connecting as `kg_owner` raises `UnsafeDatabaseRole` in the API startup check, and `kg_app` passes.
  - **T-DB-02:** **pooled reuse**. Pool size 1: request A sets workspace demo and reads principals; request B on the same physical connection *without* a context reads `principals` and gets 0 rows; request C with other_demo sees only other_demo (L2, release blocker).
  - **T-DB-03:** RLS backstop. A deliberately unscoped `SELECT * FROM principals` inside a demo `tenant_session` returns only demo rows.
  - **T-DB-04:** an `audit_log` UPDATE or DELETE raises; an INSERT with a foreign `workspace_id` is blocked by `WITH CHECK`.
  - **T-DB-05:** `auth.lookup_credential` returns at most one row, works with no workspace set, and `kg_app` cannot `SELECT` api_keys outside RLS.
  - **T-DB-06:** `alembic upgrade head` → `downgrade base` → `upgrade head` is clean.

### WP4: Authentication and authorization (`kg_governance`)
- **`authn/keys.py`** (promoted from `spikes/auth/devauth.py`, same format and hashing):
  - keys look like `kgk_<prefix>_<secret>`;
  - `hash = SHA-256(pepper ‖ key)`, compared in constant time;
  - an unknown prefix is still hashed and compared, so timing does not reveal which prefixes exist;
  - `mint()` returns the plaintext once.
- **`authn/jwt.py`:** HS256 local signer.
  - Claims: `iss, aud, sub, iat, nbf, exp (15 min), jti, workspace_id, kind, key_id`.
  - `algorithms=["HS256"]` pinned, all claims required, `leeway=0`.
  - The OIDC verifier interface (`JwksVerifier`) is stubbed and documented, not wired.
- **`authn/resolver.py`:** `authenticate(authorization_header) -> Principal`.
  - `Bearer kgk_…` → `lookup_credential` → hash compare → status checks (key revoked or expired, principal revoked, workspace suspended).
  - `Bearer <jwt>` → verify the signature → `credential_status(key_id, principal_id)` on **every request** (L5).
  - Failures map to a single `401 unauthenticated` with a generic message. The reason goes only to logs and the `auth_failures_total{reason}` metric.
- **`authz/policy.py`:**
  - `Action` enum for P1: `workspace.read`, `principal.read`, `audit.read`, `operations.read`, `diagnostics.run`.
  - The role→action matrix is code-reviewed data (§6.3).
  - A `@requires(action)` decorator and FastAPI dependency raise `403 forbidden` when the role lacks the action (an action the principal may not take in its *own* workspace).
  - **Cross-workspace resources always raise `404 not_found`**, never 403, so no existence oracle exists (SEC-01).
- **`authz/registry.py`:** every `/v1` route must be decorated with `@requires(...)` or `@public(reason)`. At app startup a check walks `app.routes` and **raises if any route lacks a policy**. A test also asserts this.
- **`audit.py`:** `AuditWriter.record(action, resource, outcome, metadata)` writes inside the request's tenant session. Metadata is validated to hold only IDs, counts, hashes or enums: a Pydantic model with `extra="forbid"` and no free-text fields.
- **Tests (unit and security):**
  - the AU-01 negative set ported: tampered claims, `alg=none`, wrong key, wrong audience, missing `workspace_id`, expired, algorithm confusion;
  - an unknown prefix takes the same code path (spy on the hash calls);
  - **T-AUTH-REVOKE:** revoke a key via the CLI → the next request with that key **and** with a JWT minted from it → 401 (L5, SEC-03 start);
  - a revoked principal → 401; a suspended workspace → 401;
  - **T-AUTH-HDR:** `X-Workspace-Id: <other_demo>` and `?workspace_id=` are ignored (L4).

### WP5: API application (`kg_api`)
- **App factory** `create_app(settings)`. Its lifespan:
  - DB role safety assertion (L1);
  - Neo4j connectivity check (no index build, L12);
  - router transport init;
  - metrics and tracing init;
  - the route-policy registry check.
- **Middleware, in order:**
  1. request context: `request_id` = a valid incoming `x-request-id` UUID or a new uuid7; `correlation_id` = a valid `x-correlation-id` or `request_id` (L6); both are echoed in the response headers;
  2. access log (metadata only);
  3. metrics (route template, not the raw path);
  4. body size limit (1 MiB in P1).
- **Dependencies:**
  - `principal = Depends(authenticate)`;
  - `ws = Depends(workspace_context)`;
  - `db = Depends(tenant_db)`, which wraps `tenant_session(ws, principal)` (L2).
- **Exception handlers:**
  - every error goes into the `ErrorEnvelope` (§7.2), including 404/405 for unknown routes and 422 validation errors (normalised to `validation_failed` with field paths, never echoing values);
  - unhandled exceptions → `500 internal_error` with `request_id` only.
- **Routers:** §7.1.
- **OpenAPI:**
  - title, version `0.1.0`, `/v1` prefix, bearer security scheme, error envelope component;
  - the `ErrorEnvelope` response is declared on every route;
  - `/docs` only when `ENV != production`.
- **CORS:** off in dev (the Vite proxy makes it same-origin); an explicit allow-list from settings in production.
- **Tests:** contract tests per endpoint (status, schema, headers), §8 security tests, and an **OpenAPI drift test** (the generated spec equals `contracts/openapi.json`).

### WP6: Router transport and model gateway (`kg_modelgw`)
Promoted from `spikes/gateway`, with these changes:
- **`transport.py` — `RouterTransport(httpx.AsyncBaseTransport)`**, the single choke point (L7). It wraps an inner transport. For every request to the router base URL it:
  1. Reads the `EgressContext` contextvar `{classification, purpose, workspace_id, service}`. If there is none → `EgressDenied("unclassified")` without sending (**fail closed**, L8). A denied class → `EgressDenied` (G-02 semantics, stage `skipped_by_policy`).
  2. Sets `Authorization` from the per-service key (L9).
  3. Sets `x-request-id` = the correlation ID (UUID).
  4. For JSON bodies on `chat/completions`: merges `metadata` (application_id `kg-platform`, task_type, agent = service, HMAC user and conversation, data_classification), `user`, and `routing.data_classification` / `routing.task_type`. It never overwrites a caller's stricter classification.
  5. Sends, times the call and parses usage, then writes a `CallRecord` to the call-log sink (async batch insert into `model_calls`) and updates the metrics.
  6. If the response carries `x-gateway-provider`, records `router_key_is_admin` (L9).
- **`gateway.py` — `ModelGateway`:** `complete()` and `embed()` with the spike's semantics:
  - purpose→alias with a fallback alias, used only on exhausted retryable errors or `unsupported_capability`;
  - `json_schema` validation;
  - E5 prefixes, ≤512-token chunking, mean-pooling, the 384-dim assertion;
  - category-based retry with `Retry-After` precedence;
  - local cost estimate.

  It is built on `httpx.AsyncClient(transport=RouterTransport(...))`. The egress context is set by a context manager `with egress(classification=..., purpose=..., ws=...)`.
- **`errors.py`:** copy of `spikes/gateway/errors.py` (category map; unknown codes are config-terminal).
- **`config.py`:** loads `compat/model-purposes.yaml` and `compat/pricing.yaml` (paths from settings) and validates their schema at startup. A missing `embedding.dim`, or a classification listed both allowed and denied, is a startup error.
- **`health.py`:** `router_health()` → `GET <origin>/health` (outside `/v1`), returning `ok|unreachable|auth_error`. `GET /v1/models` with the service key is used to confirm auth, and a mismatch between the purpose aliases and the listed aliases → `degraded` with the missing alias names.
- **Modes:** `OSO_ROUTER_MODE=mock|live` selects the base URL (§11). Tests use the in-process mock (`httpx.ASGITransport(app=create_app(MockConfig(...)))`) exactly as the spike harness did.
- **Tests (contract):**
  - G-01 to G-04 ported against the in-process mock;
  - **T-MG-01:** a Graphiti-style raw `AsyncOpenAI(http_client=AsyncClient(transport=RouterTransport))` call without an egress context → refused with no network (spy asserts 0 requests);
  - **T-MG-02:** metadata injection happens on raw SDK calls too;
  - **T-MG-03:** an admin-header response logs `router_key_is_admin`;
  - **T-MG-04:** `model_calls` rows hold no content (assert the columns; a fuzz test with sentinel strings in messages checks they never appear in the DB or logs);
  - **T-MG-05:** config validation failures.

### WP7: Graph adapter (`kg_graph`)
- **`connection.py`:** async Neo4j driver (`neo4j` 6.4.0); `ping()` (`RETURN 1`); `ensure_indices()` (calls Graphiti's index build once, swallowing "equivalent index already exists", L12).
- **`group.py`:** `GroupId` value object built only via `GroupId.for_workspace(ctx: WorkspaceContext)` → `"ws-<uuid>"`. It validates `^[A-Za-z0-9_-]+$` and rejects empty values (ADR-015).
- **`factory.py`:** `build_graphiti(settings, transport)` returns a configured but private `Graphiti` instance:
  - `OpenAIGenericClient(LLMConfig(model, small_model from purposes graph_extraction / graph_extraction_small), client=AsyncOpenAI(http_client=AsyncClient(transport=RouterTransport)))`;
  - `E5Embedder(OpenAIEmbedder(embedding_model="oso/embedding", embedding_dim=384))` (from harness.py);
  - `NoCrossEncoder()` (L11).
- **`scoped.py`:** `ScopedGraph(ctx)` is the only public entry point. In P1 it exposes:
  - `ping()`;
  - `node_count()` (scoped Cypher with `$group_id`, for the dashboard);
  - guarded helpers that later phases extend: `search(query, config)`, which always passes `group_ids=[gid]` and copies the recipe (`config.model_copy()`), and `get_node(uuid)`, which fetches and then asserts `node.group_id == gid` or else raises `NotFound`.

  Raw Cypher helpers require `$group_id` in the query text (a static check at call time).
- **Tests (contract, against the real Neo4j in the test stack):** the GR-04 trap tests ported:
  - **T-GR-01:** scoped search never returns another group's nodes;
  - **T-GR-02:** `get_node` with another group's UUID → `NotFound`;
  - **T-GR-03:** `GroupId` cannot be built from a raw string, None or an empty value;
  - **T-GR-04:** a Cypher helper without `$group_id` → error;
  - **T-GR-05:** the factory wiring assertions (generic client, NoCrossEncoder, E5Embedder 384, a RouterTransport in the chain);
  - **T-GR-06:** import-linter forbids `graphiti_core` elsewhere.

### WP8: Worker skeleton (`kg_worker`)
- **Startup:** settings → DB role assertion → Neo4j `ensure_indices()` → router health (logged; *not* fatal in mock or live mode, because P1 does no router work).
- **Loop:**
  - every 10 s, upsert `worker_heartbeats` (system session) and set the `worker_heartbeat_unixtime` metric;
  - graceful shutdown on SIGTERM/SIGINT (finish the beat, mark `status=stopped`).
- A `/metrics` endpoint on a separate port (9101).
- Extension seam for P3: a `JobHandler` protocol and an empty registry. The outbox comes in P3.
- **Tests:** heartbeat row appears and advances; SIGTERM marks it stopped; an unsafe DB role → exit code 3.

### WP9: CLI, seed and demo
- **`kg_cli` (Typer):**
  - `db upgrade|downgrade|current`;
  - `seed demo [--rotate-keys]`;
  - `keys create --workspace <slug> --principal <handle>`, `keys revoke <prefix>`, `keys list --workspace <slug>`;
  - `openapi export`.

  The CLI runs as `kg_owner` only for `db` commands. Everything else uses `kg_app` plus the security-definer functions, or an explicit `system_seed` path that sets `app.workspace_id` per workspace.
- **Seed (idempotent).** Workspace IDs are deterministic (uuid5 of the slug), so tests and demos can refer to them.

| Workspace | Principals (kind, roles) |
|---|---|
| `demo` | `admin@demo` (user, workspace_admin); `reviewer@demo` (user, reviewer); `operator@demo` (user, operator); `conversation_app` (app, app_writer); `agent_research` (agent, agent); `agent_planning` (agent, agent) |
| `other_demo` | `admin@other` (user, workspace_admin); `agent_other` (agent, agent) |

- **Seed keys:**
  - minted only on first creation (or with `--rotate-keys`);
  - written to `.local/dev-credentials.json` (gitignored, file mode 0600) and printed **once**;
  - never logged;
  - `ENV=production` refuses to seed.
- **`scripts/demo_p1.py`** (run by `make demo-p1`) asserts and prints a table:
  1. `agent_planning` → `GET /v1/me` → workspace `demo`;
  2. `GET /v1/workspaces/current` → demo;
  3. `agent_other` → `GET /v1/workspaces/{demo_id}` → 404 with the same body shape as a random UUID;
  4. `agent_other` → `GET /v1/principals` → only other_demo principals;
  5. `agent_planning` → `GET /v1/principals` → 403 (an agent lacks `principal.read`);
  6. `admin@demo` → `GET /v1/principals` → 6 demo principals;
  7. revoke a throwaway demo key → the same key and its JWT get 401;
  8. `GET /health/ready` → postgres, neo4j, router (mock), worker all ok.

  The script exits non-zero on any failure.

### WP10: Admin console (`apps/admin`)
- **Stack:** the same family as the user's router admin, for consistency:
  - Vite + React + TypeScript (strict), React Router, TanStack Query;
  - Tailwind + Radix primitives + lucide icons;
  - `openapi-typescript` (generates `src/api/schema.d.ts` from `contracts/openapi.json`) plus `openapi-fetch` for a typed client;
  - Vitest + Testing Library;
  - one Playwright smoke test.

  Versions are pinned at install and recorded in `p1_additions`.
- **Auth:**
  - the Login page takes an API key (dev) → `POST /v1/auth/token` → the JWT is held in memory and mirrored to `sessionStorage` (never `localStorage`); the key is discarded;
  - a 401 response → back to Login;
  - the top bar shows the workspace name, principal handle and logout.
- **Pages:**
  - **Dashboard:** dependency status from `GET /v1/operations/health` (postgres, neo4j, router mode and reachability, worker heartbeat age), principal counts by kind, and the last 20 audit events (IDs and action names only).
  - **Agents & Access (read-only):** a table of principals (handle, kind, roles, status, key prefixes, created) and a detail drawer. Deep link `/agents/:principalId`. A foreign or unknown ID shows the same "Not found" state.
  - Shared **permission-denied, empty, loading and error** states, used by every page (blueprint UI principle).
  - Navigation shows only the pages that exist. Later pages are added in their phases, not stubbed.
- **Rules:**
  - no secrets in the bundle;
  - no direct Neo4j access;
  - all data through the generated client;
  - render text, never HTML (`dangerouslySetInnerHTML` is forbidden by an ESLint rule).
- **Dev:** `vite` proxies `/v1` and `/health` to `http://localhost:8000`. The Compose `admin` service runs under the `dev` profile.
- **Tests:**
  - Vitest for components and states;
  - **Playwright smoke:** log in as `admin@demo` → Dashboard shows "postgres ok" → Agents lists 6 principals; log in as `admin@other` → Agents lists 2 and none of demo's handles appear anywhere on the page (an ADM-05 precursor).

### WP11: Compose, Docker image, Makefile, docs
- **`deploy/compose/docker-compose.yml`** (project `kg`). Images are pinned by the digests in `versions.lock.yaml`, and every service has a health check.

| Service | Details |
|---|---|
| `postgres` | port `5544` |
| `neo4j` | ports `7475/7688`, APOC, named volume |
| `mock-router` | built from `tools/mock_router`; port `8782` → 8781 internal; `MOCK_ROUTER_MAX_CLASSIFICATION=restricted` |
| `migrate` | one-shot `kg db upgrade` as `kg_owner` |
| `api` | waits for `migrate` to complete successfully; port 8000; `extra_hosts: host.docker.internal:host-gateway` (L15) |
| `worker` | |
| `admin` | profile `dev`, port 5273 |

- **Makefile targets:**
  - `up`, `down`, `reset` (down -v), `seed`, `demo-p1`, `logs`;
  - `test` (unit), `test-int`, `test-security`, `test-contract`;
  - `lint` (ruff + import-linter + eslint), `typecheck` (mypy + tsc), `openapi` (export + TypeScript client generation), `check` (everything);
  - plus `admin-dev`.
- **Docs:**
  - `docs/runbooks/startup.md`: first run, ports, resetting, switching to the live router;
  - `docs/architecture/p1-foundation.md`: request lifecycle sequence diagram, role matrix, RLS pattern, package rules.
- `.github/workflows/ci.yml` mirrors `make check` with service containers. It is inert until a remote exists.

---

## 6. Key Designs

### 6.1 Request lifecycle

```mermaid
sequenceDiagram
  participant C as Client (agent/app/admin)
  participant M as Middleware
  participant A as authn
  participant Z as authz policy
  participant S as tenant_session
  participant DB as Postgres (kg_app, RLS)
  C->>M: request (+x-request-id?)
  M->>M: request_id/correlation_id = UUIDv7 (or valid incoming UUID)
  M->>A: Authorization header
  A->>DB: auth.lookup_credential(prefix) / auth.credential_status(key_id) [SECURITY DEFINER]
  A-->>M: Principal (workspace from key record/claim)
  M->>Z: @requires(action) for this route
  Z-->>M: allow / 403
  M->>S: open connection + BEGIN + set_config(app.workspace_id, true)
  S->>DB: queries with explicit workspace predicate (RLS backstop)
  S-->>C: response + x-request-id + x-correlation-id; COMMIT; pool reset
```

### 6.2 Router traffic

```
ModelGateway.complete/embed ─┐
Graphiti (OpenAIGenericClient,├─► httpx.AsyncClient(transport=RouterTransport) ─► mock-router | live router
          OpenAIEmbedder)    ─┘        egress check (fail closed) · per-service key · x-request-id
                                       metadata/routing injection · CallRecord → model_calls · metrics
```

### 6.3 Roles and actions (P1 matrix; later phases add actions, never widen silently)

| Role | workspace.read | principal.read | audit.read | operations.read | diagnostics.run |
|---|---|---|---|---|---|
| workspace_admin | ✓ | ✓ | ✓ | ✓ | ✓ |
| operator | ✓ | — | — | ✓ | ✓ |
| reviewer | ✓ | — | — | — | — |
| app_writer | ✓ | — | — | — | — |
| agent | ✓ | — | — | — | — |
| reader | ✓ | — | — | — | — |

Scope ACLs (subject, project, agent, session) are stored from P1 and enforced from P7.

---

## 7. API Contract (P1)

### 7.1 Endpoints

| Method | Path | Policy | Behaviour |
|---|---|---|---|
| GET | `/health` | public | Liveness `{status:"ok"}`; no dependency calls |
| GET | `/health/ready` | public | `{status: ok\|degraded\|down, checks:{postgres, neo4j, router, worker}}`. Postgres or Neo4j down → 503. Router unreachable or worker stale → `degraded` (200). No versions or hostnames |
| POST | `/v1/auth/token` | public (key in the `Authorization` header) | Exchanges an API key for a 15-minute JWT `{access_token, token_type, expires_in}`. Audited (`auth.token_issued`) |
| GET | `/v1/me` | authenticated, no action | Principal (ID, handle, kind, roles) and workspace (ID, slug, name) |
| GET | `/v1/workspaces/current` | `workspace.read` | The caller's workspace |
| GET | `/v1/workspaces/{workspace_id}` | `workspace.read` | Own workspace → 200; **any other ID → 404** (indistinguishable from a random UUID) |
| GET | `/v1/principals` | `principal.read` | Cursor-paginated list of the caller's workspace principals with roles and key prefixes (never hashes) |
| GET | `/v1/principals/{principal_id}` | `principal.read` | Own workspace → 200; foreign → 404 |
| GET | `/v1/audit` | `audit.read` | Cursor-paginated recent audit events (metadata only) |
| GET | `/v1/operations/health` | `operations.read` | The dashboard view: readiness details plus worker heartbeat age, router mode, purpose-alias check, counts |
| POST | `/v1/operations/router-check` | `diagnostics.run` | Embeds a constant probe string with classification `internal` through `ModelGateway` (mock in P1). Returns `{ok, alias, dims, latency_ms, router_request_id}` and writes a `model_calls` row. Proves the transport end to end |
| GET | `/metrics` | public on the internal port only (9100 api, 9101 worker) | Prometheus |

**API rules:**
- Cursor pagination: `?limit=` (max 100) and `?cursor=` (opaque base64 of `(created_at, id)`).
- Every response carries `x-request-id` and `x-correlation-id`.
- `Idempotency-Key` is not required yet: P1 has no mutating business endpoints. It is defined in the OpenAPI components for P2 and P3.

### 7.2 Error envelope

```json
{"error": {"code": "not_found", "message": "Resource not found", "request_id": "0199…", "retry_after": null, "details": null}}
```

**Codes in P1:** `unauthenticated` (401), `forbidden` (403), `not_found` (404), `method_not_allowed` (405), `validation_failed` (422, with `details.fields[]` holding paths only), `payload_too_large` (413), `dependency_unavailable` (503), `internal_error` (500).

---

## 8. Test Plan and Gate Mapping

| Gate / requirement | Tests |
|---|---|
| **SEC-01 (start):** zero cross-workspace reads | T-SEC-01: for every route with a path ID, a foreign ID gives 404 with a body identical to a random UUID's. T-SEC-02: list endpoints never contain foreign rows (both workspaces seeded with similar handles). T-SEC-03: JWT `workspace_id` tampering → 401. T-AUTH-HDR. T-DB-02, T-DB-03. T-GR-01/02 |
| Deny by default | T-REG: the route-policy registry has no unpoliced routes. T-AUTHZ: the role matrix table test (each role × action) |
| SEC-03 (start): revocation is immediate | T-AUTH-REVOKE (key and JWT), revoked principal, suspended workspace |
| SEC-02 / no content in telemetry | Redaction test; metrics label audit; T-MG-04 content-sentinel fuzz across logs, `model_calls`, `audit_log` |
| SEC-06 / ING-06 egress | T-MG-01 (no classification → nothing sent), G-02 port (`local_only` → nothing sent) |
| Repeatable clean start | `make reset up seed demo-p1` twice in CI: identical demo table, seed idempotent (no duplicate principals), migrations up/down/up (T-DB-06) |
| Contracts | Endpoint contract tests; OpenAPI drift test; TypeScript client compiles (`tsc --noEmit`) |
| Boundaries | import-linter contracts; `tools.*` not imported by production code |
| Admin | Vitest states; Playwright smoke incl. the other_demo isolation check |

**Test layout:**
- unit tests live in `packages/*/tests` and `apps/*/tests`;
- cross-cutting `tests/integration`, `tests/security` and `tests/contract` run against the Compose test stack (`kg_test` DB, a separate Neo4j database is not possible on Community, so tests use unique `ws-test-<uuid>` groups and clean up by group, ADR-015 §5) and the in-process mock router.

---

## 9. Sequencing

```
WP0 ─► WP1 ─► WP2 ─┬─► WP3 ─► WP4 ─► WP5 ─┬─► WP9 ─► (demo-p1)
                   ├─► WP6 ───────────────┤
                   └─► WP7 (needs WP6) ───┤
                        WP8 (needs WP3, WP7) ┘
WP5 ─► (openapi export) ─► WP10
WP11 grows alongside: compose skeleton after WP1, completed with WP8/WP10
```

Two coding agents can work in parallel after WP2. Track A takes WP3 → WP4 → WP5. Track B takes WP6 → WP7. WP8–WP11 follow.

## 10. Exit Criteria (Definition of Done for P1)

- [x] `make reset up seed demo-p1` passes twice in a row from a clean checkout. All 8 demo assertions pass.
- [x] `make check` is green: ruff, mypy strict, import-linter, eslint, tsc, unit, integration, security and contract tests, Vitest, Playwright smoke, OpenAPI drift.
- [x] The API and worker refuse to start with an RLS-bypassing DB role, and the test proves it.
- [x] The pooled-connection reuse test (T-DB-02) passes. It is a release blocker from now on.
- [x] Every route has an authorization policy; a foreign ID gives the same 404 as a random ID.
- [x] Revocation takes effect on the next request for both API keys and JWTs.
- [x] `/v1/operations/router-check` succeeds against the mock, and the `model_calls` row has no content.
- [x] Admin: log in, Dashboard and Agents & Access work for both workspaces with no cross-workspace data.
- [x] Docs: startup runbook, P1 architecture note, `versions.lock.yaml` `p1_additions`, ADR-019 note; the compatibility report is regenerated (fixed).
- [ ] WP0 (open): `git init` and the initial commit. The plan requires asking the user first; not done.

**Not required for P1:** the live router. `make verify-router` stays the Phase 0 kit. Optional check if the router is up: `OSO_ROUTER_MODE=live make up` then `POST /v1/operations/router-check` from the container (confirms NW-01 against the real process).

## 11. Configuration (`.env.example` additions)

```dotenv
ENV=dev
# Postgres (P1 stack; spike stack keeps 5543)
DATABASE_URL_APP=postgresql+psycopg://kg_app:kg_app_dev@127.0.0.1:5544/kg
DATABASE_URL_OWNER=postgresql+psycopg://kg_owner:kg_owner_dev@127.0.0.1:5544/kg
# Neo4j (P1 stack)
NEO4J_URI=bolt://127.0.0.1:7688
# Router: mock (default) or live
OSO_ROUTER_MODE=mock
OSO_ROUTER_MOCK_URL=http://127.0.0.1:8782/v1          # containers use http://mock-router:8781/v1
OSO_ROUTER_LIVE_URL=http://127.0.0.1:8780/v1          # containers use http://host.docker.internal:8780/v1
OSO_ROUTER_API_KEY_API=mock-key
OSO_ROUTER_API_KEY_WORKER=mock-key
MODEL_PURPOSES_PATH=compat/model-purposes.yaml
PRICING_PATH=compat/pricing.yaml
# Auth (dev values; production must override, enforced by settings validation)
AUTH_KEY_PEPPER=dev-only-pepper-change-me-32-bytes-min
AUTH_JWT_SECRET=dev-only-jwt-secret-change-me-32-bytes
AUTH_JWT_TTL_SECONDS=900
TELEMETRY_HMAC_SECRET=dev-only-telemetry-secret
# Observability
LOG_LEVEL=INFO
OTEL_EXPORTER_OTLP_ENDPOINT=
GRAPHITI_TELEMETRY_ENABLED=false
```

## 12. Risks and Mitigations

| Risk | Mitigation |
|---|---|
| A route added later forgets authorization | Startup registry check plus T-REG fail the build |
| RLS gives false comfort while application code forgets predicates | Repositories always add an explicit `workspace_id` predicate (primary control); RLS is the backstop and tested separately (T-DB-03) |
| Security-definer functions become a bypass | Minimal return columns, pinned `search_path`, `REVOKE ALL FROM PUBLIC`, code review, and T-DB-05 asserts their shape |
| The per-request status check adds latency | One indexed lookup by primary key. Measured in P9; a short in-process cache is *not* allowed because of revocation immediacy |
| The mock router drifts from the real router | Unchanged Phase 0 mitigation: catalog synced from the router seed (`make sync-catalog`), live kit and recorded fixtures |
| Graphiti version drift | Exact pin; Graphiti appears only in `kg_graph`; GR-04 trap tests in CI |
| Port collisions with the spike stack or other local services | Distinct ports (L14); `docs/runbooks/startup.md` lists them |

## 13. Rollback

P1 is greenfield. Rollback means `make reset` (drops volumes) and `kg db downgrade base`. No data migration is involved. Each WP is committed separately (once git is initialised), so a WP can be reverted alone.

## 14. Open Decisions (keep open; don't assume)

1. **Admin login for humans:** dev API-key paste is the P1 mechanism. Whether P1 also needs username and password is open. Recommendation: no; go straight to OIDC later (ADR-020 path).
2. **Admin UI stack:** the plan mirrors the router admin's stack (Tailwind, Radix, lucide, TanStack Query). This is fine to change before WP10 starts.
3. **Seeded credentials file** (`.local/dev-credentials.json`): acceptable for dev demos? The alternative is printing once only.
4. **Router keys:** P1 runs on the mock. Getting the real `kg-api` / `kg-worker` non-admin keys with a higher `maxClassification` (Phase 0 change request 2) only matters for the optional live check.
