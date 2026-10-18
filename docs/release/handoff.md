# Handoff: Phase 9 (evaluation, recoverability, release hardening)

*Written 2026-10-18 for the next agent or engineer. Everything below is in git; nothing important lives only on the previous machine.*

## 1. Where it stands
- Phases P0-P9 are implemented and committed (`git log --oneline`; tag `v0.8.0-offline` marks P0-P8 but was never pushed). Phase 9 is commits `85d1093`, `d2d9a82`, `1a4f123`, `00a604e`, `8dd2bfd`, `cb11966` and the handoff commit; the 2026-10-18 follow-up (below) is **not committed yet**.
- **Verified green on the previous machine** (see ROADMAP Phase 9): lint, mypy strict, import-linter (22 contracts), 1465 unit, integration 528, security 105, contract 70, admin Vitest 183, `make test-release`, `make prod-smoke`, `make obs-test`. A full `make release-candidate` passed every step through `restore-rehearsal`, then died at `baseline` when that machine's disk filled.
- **Done on 2026-10-18 on a new machine** (uncommitted, in the working tree): the machine is set up; lint, mypy, 1467 unit, 183 Vitest, 135 memory and context integration tests and `make eval-memory` pass; the load baseline completed (`eval/reports/baseline-2026-10-18.{json,md,svg}`); sizing is in `docs/runbooks/deploy-production.md`; DEC-11 proposals are in `docs/release/decisions.md`; the ROADMAP has its Phase 9 status. To get a baseline at all, five defects had to be fixed:
  - **A real search bug** (`packages/memory/src/kg_memory/search.py`): when a query term matched most of a workspace the planner used a nested loop (2.5 billion comparisons at 50,000 memories, minutes per search). The `lex`/`vec` joins are now one `FULL JOIN` (`hits`). `make eval-memory` is unchanged. **There is no regression test**: a synthetic one passed on the old SQL too (the bad plan needs the perf seed's statistics). The baseline size curve is the only guard; add an `EXPLAIN` test if you can reproduce the plan.
  - **Four load-tool defects** (`eval/load/`): the load agent lacked a subject grant (every context build was a fast `denied` counted as a success; a denied build now fails the run), proposals lacked evidence, the raised rate limits never reached the container (`compose.perf.yml`), and scenarios queued behind each other's backlog (the outbox is drained between them).
- Not yet re-run on the new machine: `make test-int test-security test-contract`, `make test-release`, `make test-resilience`, `make test-e2e`, `make prod-smoke`, `make obs-test`, and `make release-candidate` (it needs a clean, committed tree and drops the dev volumes).

## 2. What is left (do these in order)
1. **Commit** the 2026-10-18 follow-up (search fix, load-tool fixes, baseline reports, sizing, DEC-11 proposals, ROADMAP status) as "Phase 9 (6/n)". `release-candidate` refuses a dirty tree.
2. **Release candidate:** from that clean tree, `make release-candidate` (about 90 min, plus the baseline step again; it **drops the dev volumes**; do not run other heavy work). It writes `docs/release/rc-report-<date>.md`. Fix whatever it reports, rerun the failed step with `make release-candidate ARGS="--only <step>"`. Commit the report.
3. **Owner actions** (section 4). Search needs an ANN decision before any workspace passes 50,000 memories (exact scan: p95 2.6 to 3.6 s; see DEC-11).

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
