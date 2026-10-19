# Runbook: deploying the production reference stack

One Linux VM, Docker Compose, Caddy in front ([ADR-053](../adr/ADR-053-production-reference-deployment.md)). Everything below is what `docker-compose.prod.yml` assumes; `make prod-smoke` proves its claims on any Docker host before you rely on it.

## 1. Size the VM
Derived from `eval/reports/baseline-2026-10-18.md` (`make perf-baseline`). Read the limits at the end before relying on a number.

**What was measured** (M dataset: one workspace of 50,000 memories, 310,000 memories in the database, container peaks sampled every few seconds during the scenarios):

| Component | CPU peak | Memory peak | Under which load |
|---|---|---|---|
| Postgres | 4.9 cores | 0.47 GiB | steady: 4 requests/s, 70 % context builds |
| Postgres | 5.6 cores | 0.48 GiB | burst: 40 requests/s (most context builds refused, see below) |
| Postgres | **20 cores** | 0.78 GiB | search only, 8/s (p50 0.45 s, p95 3.6 s) |
| Neo4j | 2.7 cores | 1.3 GiB (heap 640 MB, page cache 192 MB: the dev settings) | every scenario; the store is 0.5 GB |
| API replica | 0.5 to 0.8 cores | 0.28 GiB | steady and burst |
| Worker | 0.3 to 0.4 cores | 0.23 GiB | steady and burst |

Postgres dominates, and **search dominates Postgres**: exact cosine over every authorised candidate (ADR-019, no ANN index) costs about **2.5 cores per search per second** at 50,000 memories. The size curve says the same (search p95 0.75 s at 10,000 memories, 2.9 s at 50,000, 2.6 s at 200,000; context p95 0.35 / 1.65 / 1.70 s). An ANN index would change this table; the baseline's trigger ("ANN needed before L") is already true.

**Recommended VM**

| | Small | Reference |
|---|---|---|
| Workload it is sized for | up to 10,000 memories per workspace, 1 search/s, 3 context builds/s, 0.3 ingest events/s | 50,000 memories per workspace, the same rates; one workspace at 200,000 is **not** covered (search p95 2.6 s, add ANN first) |
| vCPU | 8 | 16 |
| RAM | 16 GB | 32 GB |
| Disk | 100 GB | 250 GB |
| Neo4j heap / page cache | 2 g / 2 g (the compose defaults) | 2 g / 2 g; raise the page cache to 1.2 times the store when the store passes 1.5 GB |
| Postgres `shared_buffers` / `effective_cache_size` | 2 GB / 8 GB | 4 GB / 16 GB |
| Postgres `work_mem` | 8 MB | 16 MB (untested suggestion: the 50,000-row search plan spilled its candidate set to temp files at the 4 MB default) |
| Worker | `WORKER_CONCURRENCY=4`, `SEMAPHORE_LIMIT=4` | the same, or double both if freshness matters more than router spend |

**The arithmetic** (so you can redo it with your own numbers)
- **CPU, Reference:** Postgres 4.9 (mixed load) + 2.5 (one search/s) + Neo4j 2.7 + 2 API replicas at 0.8 + worker 0.4 + Caddy and the OS 0.5, about 12 cores at peak. The 16 vCPU leaves a third free for a burst. **Small** assumes the Postgres part scales with the candidate rows (search p50 at 10,000 memories is 0.37 times the one at 50,000): 2 + 1 + 2.7 + 1.6 + 0.4 + 0.5, about 8.
- **RAM, Reference:** Postgres 4 GB shared buffers plus about 2 GB of backends; Neo4j 2 g + 2 g + about 0.5 GB of overhead; API replicas (limit 1 g each, see the compose file) 2 GB; worker 1 GB (compose limit 2 g); operating system and page cache the rest. The hot set (the whole database, 1.06 GB for 310,000 memories, about 3.4 KB per memory with a 384-dimension embedding) fits in the page cache many times over. **Small:** the same parts with Postgres at 2 GB, about 11 GB, rounded to 16.
- **Disk, Reference:** operating system and images 15 GB; Postgres 1 GB plus the planning figure of **80 MB per 1,000 ingested events** (the measured values were 19, 29 and 78 MB, so the planning figure is the worst one) times 100,000 events a month times 12 months, about 97 GB; Neo4j 1 GB; backups at `BACKUP_RETENTION_DAYS=30`, a daily set of about 1.6 GB (Postgres dump plus Neo4j dump at this data size), about 48 GB. That is 165 GB, times 1.5 headroom, 250 GB. **Small:** 20,000 events a month gives 19 GB of Postgres growth a year and 12 GB of backups, 50 GB with the base, times 1.5 headroom plus rounding, 100 GB.
- **Ingest capacity, both:** the worker settles about **0.25 jobs per second per stage** at its defaults (four concurrent jobs, four concurrent model calls) with the provisional router latency, so sustained ingest above about 0.3 events/s builds a backlog: at 7 events/s for 30 s, 81 of 227 events were terminal after 300 s. At 0.6 events/s the freshness p95 was 147 s. This is bounded by router latency and the model-call semaphore, not by CPU (the worker used 0.4 cores), so raise `WORKER_CONCURRENCY` and `SEMAPHORE_LIMIT` together and watch router spend and rate limits.
- **Context builds:** a workspace is capped at 6 builds in flight (`CONTEXT_WORKSPACE_CONCURRENCY`, protects the connection pool), which at 1.1 s per build at 50,000 memories is about **5 builds/s per workspace**; beyond that the API answers 429 `rate_limited` by design. In the burst scenario 674 of 849 builds at 27/s were refused and the rest kept the same p95.

**Limits of these numbers**
- The router was a *modelled* one (lognormal latencies, `mock-router`), so worker throughput, freshness, context-build latency and cost per event move with the real router. Re-run `make perf-baseline` after `make live-session-apply`.
- Measured on a 28-core shared host (other containers idle, below 5 % of one core), one API replica, Neo4j at the dev heap. Peaks are sampled, not integrated.
- The per-principal rate limits (60 context builds/min, 1,200 requests/min) are raised on the perf stack only, because one load agent stands in for many.
- Nothing here covers a workspace above 200,000 memories, Neo4j stores above 0.5 GB, or two busy workspaces at once.

## 2. Prepare the host
- A DNS name for the platform (`KG_PUBLIC_HOST`) pointing at the VM, ports 80 and 443 open (80 only redirects and answers the ACME challenge). Nothing else is published.
- Docker Engine with Compose v2; NTP running (JWT validation allows 60 s of skew).
- A user `kg-ops` that owns `/opt/kg` (a checkout of the tagged release) and may use Docker.
- A **journal volume** for the deletion journal that no database backup includes (the compose `journal` volume, bound to a dedicated path; `chattr +a deletions.jsonl` on it where the filesystem supports it).
- The model router reachable from the VM at `ROUTER_URL`, which is `https://models.oso.group/v1` (outbound HTTPS to Cloudflare; the router is a standalone service and nothing connects to a router port). Model calls and their data cross the public internet over TLS: the egress policy decides what may be sent (threat model F-07).

## 3. First boot checklist
1. `python scripts/prod_secrets.py` creates `deploy/compose/secrets/*` (a 0700 directory of 0444 files). Put the **real router key** (a non-admin key) into `oso_router_api_key_api` and `oso_router_api_key_worker`. Copy the whole directory into the password manager; losing `payload_keys` makes stored content unreadable forever ([payload-key-rotation.md](payload-key-rotation.md)).
2. `cp deploy/compose/prod.env.example .env.prod` and set `KG_PUBLIC_HOST`, `KG_TLS` (the certificate contact e-mail, or `internal`), `ROUTER_URL`, `NEO4J_HEAP`/`NEO4J_PAGECACHE` (from the sizing above), and the OIDC block if SSO is ready ([oidc-setup.md](oidc-setup.md)).
3. `docker compose -f deploy/compose/docker-compose.prod.yml --env-file .env.prod -p kg-prod up -d --build`. The platform **refuses to start** with a development secret, a placeholder router key, a wildcard CORS entry, or without `DELETION_JOURNAL_DIR`: a crash loop on first boot is that guard, and the log names the setting.
4. `docker compose -p kg-prod exec worker kg workspaces create --slug acme --name "Acme"` prints the first administrator's break-glass key once. Store it. (`ALLOW_BREAKGLASS_KEYS=false` is the default: set it `true` for the first sign-in if SSO is not ready, then back.)
5. Publish the ontology (console → Ontology, or `--ontology examples/ontologies/…yaml` on the create command).
6. Verify from outside: `curl https://<host>/health` → 200; the console loads; `/docs` and `/openapi.json` are not served; a request without a key is 401.

## 4. Backups, rehearsal, monitoring
- **Backups:** install `deploy/systemd/kg-backup.{service,timer}` (edit `/etc/kg/backup.env`: `BACKUP_RECIPIENTS`, `BACKUP_ESCROW_RECIPIENTS`, `BACKUP_DIR`, `BACKUP_ESCROW_DIR`, `BACKUP_RETENTION_DAYS`). The recovery identity is **not** on this host. Run one by hand (`systemctl start kg-backup`) and confirm the console's Operations → Backups shows it.
- **Rehearsal:** on a recovery host install `kg-rehearsal.*` and keep the recovery identity there ([backup-restore.md](backup-restore.md)). A release needs a passing rehearsal.
- **Metrics and alerts:** run Prometheus and Alertmanager on the `kg-prod_internal` network with `deploy/observability/{prometheus.example.yml,rules.yml,alertmanager.example.yml}`; import `grafana-dashboard.json`. Route `severity=critical` to a pager. Set `ALERTMANAGER_URL` so the console's alert panel shows what is firing.
- **Canary:** `kg workspaces create --slug canary --name Canary`, then `python scripts/canary_setup.py --api https://<host> --admin-key-file <file holding the printed admin key>` registers a `canary-reader` agent (its key is written to `deploy/compose/secrets/canary_key`) and a `canary-private` agent that saves one private memory holding a unique marker. Put the printed `CANARY_LEAK_MARKER` in `.env.prod` and `docker compose … --profile canary up -d canary`. Two failed builds in 15 minutes page; the marker ever appearing for the reader pages immediately (`leakage_check_failed`).

## 5. Upgrades
Pull the tagged release, `docker compose … build`, `up -d` (the `migrate` service runs first; API replicas roll one at a time because Caddy health-checks them). Take a backup first. Dependencies: [upgrade-pin-dependencies.md](upgrade-pin-dependencies.md).

## 6. When it goes wrong
[startup.md](startup.md) · [graph-outage.md](graph-outage.md) · [llm-outage.md](llm-outage.md) · [stuck-job-replay.md](stuck-job-replay.md) · [backup-restore.md](backup-restore.md) · [agent-revocation.md](agent-revocation.md).
