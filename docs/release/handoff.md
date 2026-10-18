# Handoff: where Phase 9 stands (2026-10-18)

**Committed (git log, newest first):** `8dd2bfd` 5/n dependency refresh · `00a604e` 4/n production deployment, operations v2, OIDC console, canary, live-session tooling, release sweep, runbooks · `1a4f123` 3/n eval, load tooling, rotation, hardening, scanning · earlier 1-2/n and P5-P8. Tag `v0.8.0-offline` marks P0-P8.

**Verified green on the refreshed pins:** lint, mypy strict, import-linter, 1465 unit tests, integration 528, security 105, contract 70, admin Vitest 183, `make test-release` (two replicas; every SEC-01..06 and AT-01..18 passes), `make prod-smoke`, `make obs-test`. In the one `make release-candidate` run, the steps through `restore-rehearsal` passed (RTO about 95 s); it died at the `baseline` step when the host disk filled, so there is no `docs/release/rc-report-*.md`.

**Not done**
1. The load baseline: `make perf-down perf-up perf-seed perf-baseline` (about 30 min; needs about 3 GB of Docker disk; the size curve now waits for the outbox to drain). It writes `eval/reports/baseline-<date>.md`.
2. Replace `@@SIZING@@` in `docs/runbooks/deploy-production.md` with VM sizing from that report.
3. Add the Phase 9 status to `ROADMAP.md` (the section exists, the status paragraph does not).
4. A clean `make release-candidate` (it resets the dev stack; the tree must be clean), then commit as 6/n.
5. Owner actions: `make live-session` then `make live-session-apply`; set SLOs (DEC-11) and retention (DEC-07) in `docs/release/decisions.md`; sign `docs/release/checklist.md`; `git tag v1.0.0-rc1`.

**On the new machine:** Docker Desktop (about 6 GB), `make tools` (installs uv), `make sync`, `make admin-install`, `make up seed`. Secrets, backup keys and perf credentials live in the git-ignored `.local/` and are not in git; regenerate them (`make backup-init`, `make perf-seed`).

**Gotchas:** `model_calls.outcome` is `ok` and the embedding operation is `embeddings`; the prod Neo4j runs as uid 7474 (the one read-only exception); prod secret files are 0444 inside a 0700 directory; tests that patch one app are auto-skipped under `KG_REPLICAS=2`; never run `make down`/`reset` in the middle of a test run.
