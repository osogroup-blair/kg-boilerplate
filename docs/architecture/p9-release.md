# Phase 9: evaluate, recover, harden, release

Decisions: [ADR-048](../adr/ADR-048-workspace-rls-not-per-principal.md) (workspace RLS, no per-principal RLS), [ADR-049](../adr/ADR-049-oidc-for-people-keys-for-machines.md) (OIDC), [ADR-050](../adr/ADR-050-shared-limiter-and-notify-invalidation.md) (shared limiter, NOTIFY invalidation), [ADR-051](../adr/ADR-051-flow-f-parked-namespaces.md) (Flow F), [ADR-052](../adr/ADR-052-secret-rings.md) (secret rings), [ADR-053](../adr/ADR-053-production-reference-deployment.md) (production deployment). Plan: [phase-9-release-hardening.md](../plans/phase-9-release-hardening.md). Register: [decisions.md](../release/decisions.md). Checklist: [checklist.md](../release/checklist.md). Threat review: [threat-model.md](../security/threat-model.md).

Run it: `make release-candidate` (everything, in order, from a clean tree) or the pieces: `make eval-all eval-gate`, `make test-release`, `make test-resilience`, `make scan`, `make backup restore-rehearsal`, `make perf-up perf-seed perf-baseline`, `make prod-smoke`, `make obs-test`. The owner actions: `make live-session` (+ `live-session-apply`) and the decisions in the register.

## What was built, by work package
| WP | Delivered | Where |
|---|---|---|
| WP1 live session | preflight, cost estimate, steps (a)-(h), calibration (token margin, cosine floors, semaphore, latency profile), proposed diff, `apply` | `tools/live_session`, `make live-session` |
| WP2 evaluation | hash-locked datasets and manifests, the conflicts / authorization / injection suites, `eval-all`, `eval-gate` (strict = exact; proposed = tolerance), the CI live-gate record check | `eval/`, `scripts/check_gate_record.py` |
| WP3 reconcile | one drift scan over graph, memory, ledger and deletions; ids-only output; safe repairs; nightly check-only scan | `packages/reconcile`, `kg reconcile` |
| WP4 load | seeded S/M/L generator, open-loop driver, lognormal latency model in the mock router, the size curve, SLO proposals | `eval/load`, `make perf-*` |
| WP5 backup/restore | age-encrypted sets, key escrow, offline Neo4j dump, `kg deletions reapply`, the restore rehearsal with RTO/RPO | `tools/backup`, `packages/governance/.../deletion/reapply.py` |
| WP6 security | secret rings and rotation, body limits, security headers, `*_FILE` secrets, production CORS, scanners and SBOM, threat review, release sweep on two replicas | `packages/common/secrets.py`, `scripts/scan.py`, `scripts/test_release.py` |
| WP7 shared state, OIDC | Postgres limiter, NOTIFY invalidation, OIDC verification/login/mappings, break-glass flag | `packages/governance/{limiter,notify,authn/oidc}.py` |
| WP8 Flow F | rebuild in a parked namespace, one-transaction cutover, rollback | `packages/reconcile/reindex.py`, `packages/graph/reindex.py` |
| WP9 operations | cost / latency / freshness / backups / live-eval / alerts read models, console panels, OIDC login page, graph rebuild page, Prometheus rules (unit-tested), Alertmanager, Grafana, the canary and its leakage probe | `apps/api/.../operations_release.py`, `apps/admin`, `deploy/observability`, `apps/worker/.../canary.py` |
| WP10 deployment | production compose, Caddy, edge image, secrets generator, `kg workspaces create`, systemd timers, smoke test | `deploy/`, `scripts/prod_*.py` |
| WP11 docs | runbooks, checklist, decision register, ADRs, this page | `docs/` |
| WP12 | `make release-candidate` | `scripts/release_candidate.py` |

## Request path with replicas
```
client → Caddy (TLS) → api replica A | B      (least connections, /health checked)
                         │  shared limiter ──► Postgres (UNLOGGED counters, database clock)
                         │  LISTEN kg_invalidate ◄── triggers on settings, principals, grants, api_keys, ontology, namespaces, oidc mappings
                         └─ RLS (kg_app, forced) ──► Postgres      group_id ──► Neo4j
worker: outbox (SKIP LOCKED polling) · reconcile scan (nightly, check-only) · reindex builds · gauges
canary: public API every 5 min (context build + cross-agent leak probe) → metrics
```

## Deviations from the plan, and limits (read these before trusting a number)
1. **Datasets v1** freeze the existing P4-P8 sets plus the new generated suites. The extra synthetic cases the plan lists (about twenty more extraction conversations, new context cases) are deferred to a v2 with the live session's evidence.
2. **Flow F uses parked uuids**, not shared uuids across two groups (ADR-051): Neo4j's global uniqueness constraints and Graphiti's merge-by-uuid make the plan's wording impossible.
3. **FI-20 (router latency) and FI-22 are emulated** (a delayed mock, a crash at a chosen point), not produced by a real network fault. **The mock cannot reproduce JWT clock skew**: NTP is a deployment prerequisite.
4. **The latency model is provisional** until the live session captures real percentiles (step g); every baseline report says so on its first line. The SLOs in it are **proposals**.
5. **Communities (V12)** cannot be measured offline; the decision is to stay disabled.
6. **Journal text-residual exceptions** are not re-raised on reapply (the journal carries no names): F-08.
7. **Backups run from the host** (the Neo4j Community dump is offline), not as a compose sidecar.
8. **Model calls from background stages belong to no principal**, so the console's "top consumers" ranks principals by context builds; spend is by purpose and alias.
9. **The release sweep runs the suites with `KG_REPLICAS=2`** (two in-process apps with their own pools, caches and listeners behind a round-robin client, one shared database), not two containers; `make prod-smoke` is the container-level proof.
10. **Graph tenancy (DEC-06) remains an open production decision.**

## Surprises worth knowing
- `model_calls.outcome` is `ok` for success; the router's error code otherwise. Two cost estimates used `success` and always read zero until this phase.
- A kept restore environment leaks volumes: `fresh_target` tears down first.
- Never recreate the compose stack in the middle of a test run (the suites share one database and one Neo4j).
- A context rate limit also applies to measurements: the baseline waits out `retry_after` rather than recording a 429 as a latency.
