# Handoff: Phase 9 (evaluation, recoverability, release hardening)

*Written 2026-10-18 for the next agent or engineer. Everything below is in git; nothing important lives only on the previous machine.*

## 1. Where it stands
- Phases P0-P9 are implemented and committed (`git log --oneline`; tag `v0.8.0-offline` marks P0-P8). Phase 9 is commits `85d1093`, `d2d9a82`, `1a4f123`, `00a604e`, `8dd2bfd` and the handoff commit.
- **Verified green** on the current pins: lint, mypy strict, import-linter (22 contracts), 1465 unit tests, integration 528, security 105, contract 70, admin Vitest 183, `make test-release` (the SEC/AT sweep on two API replicas: every id passes), `make prod-smoke`, `make obs-test`.
- One full `make release-candidate` run passed every step up to and including `restore-rehearsal` (RTO about 95 s), then **died at the `baseline` step because the previous machine's disk filled**. So there is no load-baseline report and no `docs/release/rc-report-*.md` yet.

## 2. What is left (do these in order)
1. **Set up the machine** (section 3) and run `make up seed` plus a quick `make lint typecheck test` to confirm the checkout is healthy.
2. **Load baseline:** `make perf-down perf-up perf-seed perf-baseline` (about 30 min, needs roughly 3 GB of Docker disk; runs in its own `kg-perf` compose project beside the dev stack). Output: `eval/reports/baseline-<date>.{json,md,svg}`. Do not run other heavy work while it runs, it measures latency. The size curve waits for the outbox to drain; if a search in the curve times out, the stack was still busy.
3. **VM sizing:** replace the `@@SIZING@@` line in `docs/runbooks/deploy-production.md` with a sizing table derived from the baseline (CPU, RAM, disk, Neo4j heap and page cache), and put the proposed SLOs from the report into `docs/release/decisions.md` as *proposals* (the owner sets DEC-11).
4. **ROADMAP:** add the Phase 9 status paragraph under "Phase 9" in `ROADMAP.md` (same shape as the Phase 8 status: verified, built, details links, surprises). The facts are in `docs/architecture/p9-release.md`.
5. **Release candidate:** from a clean tree, `make release-candidate` (about 90 min; it **drops the dev volumes**). It writes `docs/release/rc-report-<date>.md`. Fix whatever it reports, rerun the failed step with `make release-candidate ARGS="--only <step>"`.
6. **Commit** the results as "Phase 9 (6/n)". Update `docs/release/handoff.md`.

## 3. Setting up a new machine
```bash
# Docker Desktop (about 6 GB memory, 20+ GB free disk), git, Node 20
git clone https://github.com/osogroup-blair/kg-boilerplate.git && cd kg-boilerplate
make tools            # installs uv into .tools/ (the Makefile finds it)
make sync             # Python workspace, exact versions from uv.lock
make admin-install    # console dependencies
make up seed          # postgres, neo4j, mock router, migrations, api, worker; demo workspaces
make lint typecheck test
```
Full test order with the stack up: `make test-int test-security test-contract` (about 17 min), then `make test-release` (about 8 min), `make test-resilience`, `make test-e2e`.

Not in git, regenerate as needed: `.local/` (dev credentials from `make seed`; backup keys from `make backup-init`; perf credentials from `make perf-seed`), `.env`, `deploy/compose/secrets/` (`make prod-secrets`).

## 4. Owner actions (a person must do these; do not fake them)
- `make live-session` against the real model router (needs `OSO_ROUTER_MODE=live`, `OSO_ROUTER_BASE_URL`, a non-admin `OSO_ROUTER_API_KEY`; it prints a cost estimate and asks first), read `eval/reports/live/<date>/proposal.diff`, then `make live-session-apply`.
- Set the SLOs (DEC-11) and retention/backup windows (DEC-07) in `docs/release/decisions.md`.
- Sign `docs/release/checklist.md` and the threat review (`docs/security/threat-model.md` section 5), then `git tag v1.0.0-rc1`.
- The open production decision DEC-06 (graph tenancy) is recorded, not resolved.

## 5. Map of what Phase 9 added
Evaluation `eval/` (`make eval-all eval-gate`) · load `eval/load` (`make perf-*`) · backups `tools/backup` (`make backup restore-rehearsal`) · reconcile and reindex `packages/reconcile` · secrets and rotation `packages/common/.../secrets.py`, `docs/runbooks/rotate-secrets.md` · scanners `scripts/scan.py` (`make scan`) · release sweep `scripts/test_release.py` (`make test-release`) · production `deploy/compose/docker-compose.prod.yml`, `make prod-smoke` · observability `deploy/observability` (`make obs-test`) · canary `apps/worker/.../canary.py`, `scripts/canary_setup.py` · live session `tools/live_session` · runbooks `docs/runbooks/` · ADRs 048-053.

## 6. Gotchas that cost time before
- `model_calls.outcome` is `ok` for success (the router's error code otherwise) and the embedding operation is `embeddings`.
- Production Neo4j runs as uid 7474 and is the one documented exception to read-only containers; production secret files are mode 0444 inside a 0700 directory (the services are non-root).
- Tests that patch one app's internals are auto-skipped under `KG_REPLICAS=2` (the sweep); they still run in `make test-int`.
- `ops.expire_keys_on_pepper` is platform-wide; the rotation test restores the keys it expires in other workspaces.
- Never run `make down`/`make reset` mid-test; the suites share one database and one graph.
- A full host disk wedges Docker Desktop (the daemon stops answering); keep several GB free.
