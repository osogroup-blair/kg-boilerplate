# Handoff: Phases 9-10 (release hardening, then working examples)

*Written 2026-10-18 for the next agent or engineer. Everything below is in git; nothing important lives only on the previous machine.*

## 1. Where it stands
- Phases P0-P10 are implemented and committed (`git log --oneline`; tag `v0.8.0-offline` marks P0-P8 but was never pushed). Phase 9 ends at `88ddaa9` (load baseline, the memory-search fix, sizing, DEC-11 proposals, ROADMAP status). Phase 10 is the commits after it: the plan (`docs/plans/phase-10-closeout-and-examples.md`), the example kit, the Sarah scenario, the CRM second domain, the agent examples, seven notebooks, docs, `make examples` and the CI `examples` job.
- **Verified green on the previous machine** (see ROADMAP Phase 9): lint, mypy strict, import-linter (22 contracts), 1465 unit, integration 528, security 105, contract 70, admin Vitest 183, `make test-release`, `make prod-smoke`, `make obs-test`. A full `make release-candidate` passed every step through `restore-rehearsal`, then died at `baseline` when that machine's disk filled. The load baseline has since been produced separately (`eval/reports/baseline-2026-10-18.*`).
- **Verified on this machine (2026-10-18/19):** `make lint typecheck` (import-linter 22 contracts); `make test-examples` 26 passed (Sarah 11/11 steps, CRM benchmark, agents 5/5, the seven notebooks); `uv run pytest tools/mock_router/tests` 50 passed.
- **Not re-run since the move:** `make test-int test-security test-contract`, `make test-release`, `make test-resilience`, `make test-e2e`, `make prod-smoke`, `make obs-test`, and the whole `make release-candidate` (it needs a clean, committed tree and drops the dev volumes; it now includes an `examples` step).
- **The GitHub `check` job is red on `main`** (both runs on 2026-10-18) with the same four integration failures: `tests/integration/test_db_p4.py` (`test_constraints_keep_extracted_rows_honest`, `test_the_application_role_cannot_delete_or_rewrite_what_it_must_not`, `test_the_review_gauges_function_returns_aggregates_without_ids`) and `tests/integration/test_p9_reapply.py::test_a_restore_from_before_a_deletion_forgets_again` (`pg_dump failed: service "postgres" is not running`: it expects the compose stack, CI uses service containers). They have not been investigated; they pass locally against the compose stack. The new `examples` job is independent of `check` and **has not run on GitHub yet**: the first push will show whether the runner can do `make up seed mcp-up examples test-examples` (it should; unverified).
- **Known weaknesses to be aware of:** the memory-search fix has only a partial guard (`test_the_scope_predicate_is_in_the_scan_of_the_search_statement_not_a_filter_after_it` asserts the lex/vec merge is a full join; the bad nested-loop plan itself needs the perf seed's statistics to reproduce). `make test-mock` fails on a missing `kg_common` in the spikes environment (it was already broken); run the mock router tests with `uv run pytest tools/mock_router/tests`. Search is the capacity limit (see DEC-11).

## 2. What is left (do these in order)
1. **Fix or explain the four red CI integration tests** (above), push, and confirm the `examples` job is green.
2. **Release candidate:** from a clean, committed tree, `make release-candidate` (about 100 min plus the baseline step; it **drops the dev volumes**; do not run other heavy work, and keep several GB of disk free). It writes `docs/release/rc-report-<date>.md`. Fix whatever it reports, rerun the failed step with `make release-candidate ARGS="--only <step>"`. Commit the report.
3. **Owner actions** (section 4). Search needs an ANN decision before any workspace passes 50,000 memories (exact scan: p95 2.6 to 3.6 s; see DEC-11). The examples can also be run with `--live` against the real router once the live session is applied.

## 3. Setting up a new machine
```bash
# Docker Desktop (about 6 GB memory, 20+ GB free disk), git, Node 20
git clone https://github.com/osogroup-blair/kg-boilerplate.git && cd kg-boilerplate
make tools            # installs uv into .tools/ (the Makefile finds it)
make sync             # Python workspace, exact versions from uv.lock
make admin-install    # console dependencies
make up seed          # postgres, neo4j, mock router, migrations, api, worker; demo workspaces
make lint typecheck test
make examples-install && make examples test-examples   # optional: the Phase 10 examples and notebooks
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

Phase 10 added `examples/` (kit `_kit`, `sarah`, `domains/crm`, `agents`, `notebooks`; index in `examples/README.md`), `tests/examples`, `docs/architecture/overview.md`, `docs/guides/` (extend, add-a-domain), the optional `examples` dependency group, `make examples examples-install demo-sarah demo-crm eval-crm examples-agents notebooks test-examples help`, a scripted `_message` (tool calls) response in the mock router, and the `examples` job and release-candidate step.

## 6. Gotchas that cost time before
- **Examples against the strict mock:** every structured request the pipeline can make for an ingested event needs a rule (`ExtractedEntities`, `ExtractedEdges`, `MemoryCandidates`, and once the workspace has nodes `NodeResolutions` and `SummarizedEntities`; an entity type with attributes beyond its name also asks one request named after the type). A gap does not fail the example: the job retries and the run only times out waiting. `Runner.settle()` waits for the outbox and names the stuck stage; it ignores `memory_expire` (scheduled 24 h out on purpose).
- `make examples` runs `make mcp-up` first, which recreates the api and mock router: never during a test run. The examples create a workspace per run (`ex-<name>-<time>`); `make reset` clears them; there is no governed cleanup yet.
- Generated notebooks are committed without outputs; edit `examples/notebooks/src/*.py` and run `python -m examples.notebooks.build` (a test fails when they differ).
- `model_calls.outcome` is `ok` for success (the router's error code otherwise) and the embedding operation is `embeddings`.
- Production Neo4j runs as uid 7474 and is the one documented exception to read-only containers; production secret files are mode 0444 inside a 0700 directory (the services are non-root).
- Tests that patch one app's internals are auto-skipped under `KG_REPLICAS=2` (the sweep); they still run in `make test-int`.
- `ops.expire_keys_on_pepper` is platform-wide; the rotation test restores the keys it expires in other workspaces.
- Never run `make down`/`make reset` mid-test; the suites share one database and one graph.
- A full host disk wedges Docker Desktop (the daemon stops answering); keep several GB free.
