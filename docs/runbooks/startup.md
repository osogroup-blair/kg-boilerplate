# Runbook: starting, resetting and operating the stack (Phase 1 + Phase 2)

## Prerequisites
- Docker Desktop (about 4 GiB is enough; Neo4j is capped at a 768 MB heap).
- `uv` (if it is not on `PATH`, `make tools` installs it into `.tools/uvenv`, which the Makefile uses automatically).
- Node 20 for the admin app (`make admin-install`).

## First run
```bash
make sync          # Python workspace, exact versions from uv.lock
make up            # postgres, neo4j, mock router, migrations, api, worker   (about 2 minutes the first time)
make seed          # demo + other_demo workspaces; prints each new API key ONCE and writes .local/dev-credentials.json (mode 0600)
make demo-p1       # 8 assertions: isolation, role gating, immediate revocation, readiness
make demo-p2       # 13 assertions: ontology publish/rollback, deterministic commands, zero-LLM graph projection (re-runnable)
make admin-install admin-dev   # admin console on http://localhost:5273
```
Sign in to the console by pasting a key from `.local/dev-credentials.json`, for example `demo/admin@demo` (full dashboard and Agents & Access) or `other_demo/admin@other` (a different workspace; it never sees `demo` data).

## Ports
| Port | What | Note |
|---|---|---|
| 8000 | API (`/docs` in dev) | metrics on 9100 |
| 9101 | worker metrics | |
| 5544 | Postgres (pgvector) | the spike stack uses 5543; 5432-5437 belong to other projects |
| 7475 / 7688 | Neo4j browser / bolt | the spike stack uses 7474 / 7687 |
| 8782 | mock router | the spike mock uses 8781 |
| 5273 | admin dev server | 5173 and 5174 are taken on this machine |

## Two database roles (do not merge them)
- `kg_owner` owns the schema, runs migrations and has `BYPASSRLS` (it is never used at runtime).
- `kg_app` is what the API and worker connect as: `NOSUPERUSER NOBYPASSRLS`. Both processes **refuse to start** if their role could bypass row-level security (the worker exits with code 3).
- `key_hash` is not selectable by `kg_app`; authentication goes through the `SECURITY DEFINER` function `auth.lookup_credential()`.

## Resetting
```bash
make reset         # docker compose down -v (drops the Postgres and Neo4j volumes) and deletes .local/dev-credentials.json
make up seed demo-p1
```
`make reset up seed demo-p1` is repeatable: seeding is idempotent and keys are minted only when a principal has no active key (`kg seed demo --rotate-keys` revokes and re-mints).

## API keys
```bash
uv run kg keys create --workspace demo --principal agent_planning   # prints the key once
uv run kg keys list   --workspace demo                              # prefixes only, never secrets
uv run kg keys revoke <prefix>                                      # takes effect on the very next request, for the key AND its JWTs
```

## Router: mock or live
Default is the mock router (`OSO_ROUTER_MODE=mock`). To use the real Oso router (needs `./start-local.sh` in `oso_model_router` and a **non-admin** key per service):
```bash
export OSO_ROUTER_MODE=live OSO_ROUTER_API_KEY_API=... OSO_ROUTER_API_KEY_WORKER=...
make up
# then, as a user with the operator or admin role:
curl -s -X POST localhost:8000/v1/operations/router-check -H "authorization: Bearer $KEY"
```
Containers reach a loopback-only router through `host.docker.internal:8780` (compose sets `extra_hosts`). An admin router key is detected from provider headers and logged as `router_key_is_admin` (an error under `ENV=production`). Phase 0's `make verify-router` remains the live verification kit.

## Tests
```bash
make stack-up                # postgres + neo4j + mock router only (what host-run tests need)
make test                    # unit + offline contract tests (no stack)
make test-int test-security test-contract
make check                   # lint, types, all of the above, admin Vitest
make test-e2e                # Playwright (P1 smoke + P2 ADM-01 and entity console); runs demo-p2 first; needs `make up seed`
```
Integration suites fail loudly (not skip) when the stack is missing and `KG_REQUIRE_STACK=1` (the `make` targets set it).

## Troubleshooting
- **`api` unhealthy:** `make logs`. The usual causes are a role that can bypass RLS (`UnsafeDatabaseRole`) or a failed migration (check the `migrate` container).
- **Readiness `degraded`:** the router is unreachable or no worker heartbeat is fresh (`/health/ready` reports which). Postgres or Neo4j down returns 503.
- **Port already in use:** another project is holding it; see the table above and change the host side of the mapping in `deploy/compose/docker-compose.yml`.
- **Docker memory:** if Neo4j is killed, raise Docker Desktop memory or lower `NEO4J_server_memory_heap_max__size`.

## Phase 2: ontology, commands and the graph projection
- The demo workspace starts with **no ontology**: commands answer `409 no_active_ontology` until an admin publishes one. `make demo-p2` does it through the API; `uv run kg seed demo --ontology examples/ontologies/personal-knowledge.yaml` does it directly (idempotent).
- Entities and facts live in Postgres. Each applied command enqueues a projection job in the **outbox**, which the worker's dispatcher runs (the P2 sweeper is off). A command request waits up to `PROJECTION_REQUEST_BUDGET_SECONDS` (5) for its own projection, then answers `202` with a status URL. See [projection-repair.md](projection-repair.md) and [stuck-job-replay.md](stuck-job-replay.md).
- Events: `POST /v1/interactions` (encrypted at rest, idempotent). `make demo-p3` is the tour; the runbooks cover replay, dead letters and [payload key rotation](payload-key-rotation.md). The dev stack ships a development keyring; `ENV=production` refuses it (`PAYLOAD_KEYS`, `PAYLOAD_ACTIVE_KID`, `CONTENT_HMAC_SECRET`).
- `make up` creates `.local/faults/`, which the worker container reads for development-only failure injection. `make test-resilience` (FI-01..08) crashes workers and restarts Postgres and Neo4j in the compose stack.
- Writes need an `Idempotency-Key` header on every mutation; entity edits and ontology draft edits need `If-Match` (take it from the `ETag` of the GET).
- `make demo-p2` can be repeated any number of times. After `make test-e2e` the active ontology may be a newer version; the demo re-activates v1.0.0 as its last ontology step.
- The integration tests share Neo4j with this stack. Each test uses its own random workspace, so they do not touch the demo data; the one exception is the demo workspace's graph node count, which a test reads from the graph instead of assuming zero.
