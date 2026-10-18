# Release checklist (v1.0.0-rc1)

The owner ticks a box only with the linked evidence open. The release candidate is tagged `v1.0.0-rc1` when every box is ticked. `make release-candidate` produces the reports below and `docs/release/rc-report-<date>.md` links them.

## Strict blockers (zero tolerance)
- [ ] **AT-01** idempotent ingestion — release sweep `AT-01`
- [ ] **AT-08** context budget never exceeded — sweep `AT-08`, `eval-context` budget adherence 100 %
- [ ] **AT-10** no cross-agent memory/vector/trace leak — sweep `AT-10`, `eval-authorization`
- [ ] **AT-11** no cross-workspace graph access — sweep `AT-11`
- [ ] **AT-15** prompt injection changes no policy and leaks nothing — sweep `AT-15`, `eval-injection`
- [ ] **AT-16** deletion: immediate read block + verified purge — sweep `AT-16` (residuals 0)

## Acceptance and security ids (release sweep, two API replicas)
Report: `eval/reports/<date>/release-sweep.md`
- [ ] AT-02 · AT-03 · AT-04 · AT-05 · AT-06 · AT-07 · AT-09 · AT-12 · AT-13 · AT-14 · AT-17 — each `pass` in the sweep
- [ ] **AT-18** backup/restore — a passing `make restore-rehearsal` from this candidate's own backups: `eval/reports/restore-<date>.md` (journal re-applied, residuals 0, reconcile clean)
- [ ] SEC-01 · SEC-02 · SEC-03 · SEC-04 · SEC-05 · SEC-06 — each `pass`

## Gates and runs
- [ ] `make check` (ruff, mypy strict, import-linter) green
- [ ] `make test`, `test-int`, `test-security`, `test-contract` green
- [ ] `make test-resilience` — FI-01..23 green (FI-20 and FI-22 are emulated: see `docs/architecture/p9-release.md`)
- [ ] `make test-e2e` and the admin Vitest suite green
- [ ] `make eval-all eval-gate` green: `eval/reports/gate/` (strict metrics exact, proposed metrics inside tolerance)
- [ ] **Live session done and applied** (`make live-session`, review, `make live-session-apply`): `eval/reports/live/<date>/summary.md`; ADR-012/013/020 Accepted; every P4–P8 live threshold confirmed, or listed as a release blocker / accepted exception below
- [ ] `make eval-gate-live` record present for any model-affecting change since the live session (CI `scripts/check_gate_record.py`)
- [ ] `make scan` clean: `eval/reports/<date>/scan.json` (no unexcepted HIGH/CRITICAL, no expired exception); SBOMs in `eval/reports/sbom/`
- [ ] `make obs-test` (alert rules unit-tested)
- [ ] `make prod-smoke` green (TLS, read-only, internal networks, non-root, break-glass)
- [ ] Load baseline recorded: `eval/reports/baseline-<date>.md` (M size, plus the S/M/L curve)

## Owner decisions (docs/release/decisions.md)
- [ ] SLOs set (DEC-11) and the alert thresholds updated to match
- [ ] Retention and backup windows set (DEC-07)
- [ ] DEC-06 (graph tenancy) acknowledged as the open production decision
- [ ] Threat review signed off (`docs/security/threat-model.md`): residual risks §5 accepted
- [ ] Recovery identity and escrow identity held by named people; restore key-recovery test done once

## Sign-off
Signed: ____________________   Date: ____________   Commit: ____________
