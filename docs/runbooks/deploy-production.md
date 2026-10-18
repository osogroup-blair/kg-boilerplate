# Runbook: deploying the production reference stack

One Linux VM, Docker Compose, Caddy in front ([ADR-053](../adr/ADR-053-production-reference-deployment.md)). Everything below is what `docker-compose.prod.yml` assumes; `make prod-smoke` proves its claims on any Docker host before you rely on it.

## 1. Size the VM
@@SIZING@@

## 2. Prepare the host
- A DNS name for the platform (`KG_PUBLIC_HOST`) pointing at the VM, ports 80 and 443 open (80 only redirects and answers the ACME challenge). Nothing else is published.
- Docker Engine with Compose v2; NTP running (JWT validation allows 60 s of skew).
- A user `kg-ops` that owns `/opt/kg` (a checkout of the tagged release) and may use Docker.
- A **journal volume** for the deletion journal that no database backup includes (the compose `journal` volume, bound to a dedicated path; `chattr +a deletions.jsonl` on it where the filesystem supports it).
- The model router reachable from the VM at `ROUTER_URL` (its host or VPN address). The reference compose speaks plain HTTP to it: put it on a private network or a VPN (threat model F-07).

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
