# The public sandbox

A throwaway, public instance of the platform on a **mock router**: a visitor gets a private, pre-seeded workspace (the Sarah scenario through the 2027 job change) for two hours, with four API keys, the admin console and a guided tour. Then it is deleted. It exists so a developer can try the real API before cloning anything.

This directory is everything needed to run one. **Deploying it is an owner action**: it is a public service. Nothing here deploys automatically.

## What a visitor gets, and what they can do

| Key | Principal | Power |
|---|---|---|
| `admin` | the workspace administrator of their **own throwaway workspace** | everything inside that workspace, including the console |
| `planning_agent` | agent with `read` on Sarah and the `newco-plan` project, profile `planning` | build contexts, search memory |
| `research_agent` | agent that may `propose`/`publish` findings to the project, profile `research` | write a sourced finding |
| `ungranted_agent` | agent with no grants | see the denial |

The plan called for a "reviewer" key instead of an administrator, but only agents and apps can be registered through the API (people come from the CLI or OIDC), and the console needs a human role. The administrator of a workspace that holds nothing but the visitor's own sandbox data and is deleted after two hours is the honest choice; the limits below are what contain it.

### Route allowlist (what the edge lets through)

| Path | Allowed |
|---|---|
| `/v1/*`, `/health` | yes, to the API: every route the API itself authorises for the caller's role |
| `/v1/operations/health`, `GET /v1/operations/backlog` | yes (the console's dashboard needs them) |
| `/v1/operations/*` (anything else: router check, jobs, backups, reconcile) | **404** |
| `/sandbox/*` | the broker (create, read, end a session; send a scripted event; list scripts) |
| `/sandbox/metrics`, `/metrics*`, `/__mock*` | **404** |
| `/mcp*` | the MCP server |
| `/` and the rest | the admin console |

### What contains an administrator

- Per-principal request limit (`PRINCIPAL_REQUESTS_PER_MINUTE`, 120) and per-principal context rate (`CONTEXT_RATE_PER_MINUTE`, 20); the API's own per-workspace concurrency cap.
- At most `SANDBOX_MAX_ACTIVE` (40) live sessions and `SANDBOX_IP_PER_HOUR` (3) new sessions per address per hour; optional Cloudflare Turnstile on "Start a sandbox".
- Request bodies capped at 256 KB (64 KB for the broker) at the edge.
- The model router is a mock on a network with no route out: nothing a visitor types reaches a model provider or the internet.
- Workspaces are deleted by the governed deletion request after `SANDBOX_TTL_HOURS` (2), by the reaper every minute; a nightly timer wipes and rebuilds everything as the backstop.

## What the sandbox is not

It is not a live model. The mock router answers from prepared fixtures in **strict** mode, so a visitor cannot send free text through extraction: they can send **scripted events** (prepared conversations) that go through the real pipeline. Everything deterministic (commands, corrections, deletions, grants, searches, context builds) is real and writable.

## Isolation

Graph tenancy is application-enforced today (DEC-06: one Neo4j database, `group_id` per workspace). The sandbox is therefore isolated **by host**: its own Postgres and Neo4j, its own volumes, nothing else on the machine. Do not run it next to anything that holds real data.

## Deploy

```bash
deploy/sandbox/setup.sh                 # secret files; a random key shared by the platform and the mock router; .env.sandbox
$EDITOR .env.sandbox                    # KG_PUBLIC_HOST, KG_TLS, SANDBOX_SITE_ORIGIN
# optional: put the Cloudflare Turnstile secret key in deploy/compose/secrets/turnstile_secret
docker compose -f deploy/compose/docker-compose.prod.yml -f deploy/sandbox/docker-compose.sandbox.yml \
  --env-file .env.sandbox -p kg-sandbox up -d --build
sudo cp deploy/sandbox/systemd/kg-sandbox-wipe.* /etc/systemd/system/ && sudo systemctl enable --now kg-sandbox-wipe.timer
```

Then build the static site with `PUBLIC_SANDBOX_URL=https://<KG_PUBLIC_HOST>` (and `PUBLIC_TURNSTILE_SITE_KEY` if Turnstile is on) so the "Start a sandbox" panel is live. Load `rules.sandbox.yml` next to `deploy/observability/rules.yml` and scrape the broker's `/sandbox/metrics` on its internal port.

## Before it is announced (the owner reviews each)

1. `make sandbox-smoke`: broker tests, the merged compose files, and the real Caddy answering 404 for the denied paths. Passes locally without a deployment.
2. `make sandbox-test`: the broker against a real stack (seeds a workspace, uses the keys, sends both scripted events, ends the session).
3. **The leak suites against the sandbox deployment.** From a checkout, point the release sweep at it with the sandbox settings, then record the result in `docs/release/sandbox-<date>.md`.
4. `python deploy/sandbox/verify_edge.py https://<host> --site-origin https://<site>`: the denials hold from the internet, CORS is exact, a session works end to end.
5. `python deploy/sandbox/load/soak.py https://<host> --visitors 40 --minutes 30`: no 5xx, context p95 inside the proposed SLO (lift the per-address limit for the run only).
6. The reaper: end a session early, wait, and prove its key is refused (`verify_edge.py --expire-wait 130`); let one expire naturally and check the workspace is gone.

## Files

| File | What |
|---|---|
| `docker-compose.sandbox.yml` | the overlay: mock router, broker, throwaway-instance settings |
| `Caddyfile.sandbox` | the edge: the allowlist above |
| `broker.Dockerfile` | the broker image (the platform's Python image + `apps/sandbox` + `examples`) |
| `sandbox.env.example`, `setup.sh` | configuration and one-time setup |
| `rules.sandbox.yml` | alerts: near the cap, pool empty, reaper stalled, deletions stuck, disk |
| `systemd/` | the nightly wipe |
| `verify_edge.py`, `load/soak.py` | pre-launch checks against the deployed host |
