# Runbook: upgrading pinned dependencies (ADR-011)

Everything is pinned exactly: Python packages in `uv.lock`, admin packages in `package-lock.json`, images by digest in the compose files, tools in `compat/versions.lock.yaml`. Nothing is upgraded by a floating tag.

## Cadence
- **Security fixes:** as soon as `make scan` blocks (an unexpired exception in `docs/security/scan-exceptions.yaml` is a dated promise to do this).
- **Routine refresh:** quarterly, in one change, with the evidence below. `compat/versions.lock.yaml` records `refreshed_on`.

## Procedure
1. Branch. Change pins one group at a time: (a) Python libraries, (b) admin libraries, (c) base images, (d) Neo4j/Postgres, (e) **Graphiti last and alone**.
2. Python: edit `pyproject.toml`, `uv lock`, `uv sync --frozen`. Admin: `npm install <pkg>@<exact>` in `apps/admin`, `npm run generate:api` if the API changed.
3. Prove it, in this order, and stop at the first red:
   `make check` (ruff, mypy strict, import-linter) → `make test` → `make up` + `make test-int test-security test-contract` → `make test-release` → `make eval-all eval-gate` → `make scan`.
   Admin: `npm run typecheck lint test` and `make test-e2e`.
4. **Graphiti** additionally: `make spike-offline`, the P4 and P8 graph suites (`tests/integration/test_p4_*`, `test_p8_graph_deletion.py`), and `make reconcile` on a restored backup. ADR-011 says why: minor versions change APIs the adapter (`packages/graph`) depends on. A Graphiti change is **model-affecting**: CI requires a live-gate record (`make eval-gate-live`, `scripts/check_gate_record.py`).
5. Images: update the digest in the compose file *and* `compat/versions.lock.yaml`, `make scan-third-party` for vendor findings, `make prod-smoke`.
6. Record: `compat/versions.lock.yaml` (`refreshed_on`, the changed pins), a line in `docs/release/decisions.md` if a major version moved, and the scan report.
7. If anything cannot be upgraded (a major that breaks e2e, a vendor lag), record a **dated deferral** (what, why, retry date) instead of leaving it implicit.

## Rollback
Pins are in git: revert the commit, `uv sync --frozen`, rebuild images. A Neo4j or Postgres major upgrade is not reversible in place: take a backup first ([backup-restore.md](backup-restore.md)) and rehearse the restore on the new version before cutting production over.
