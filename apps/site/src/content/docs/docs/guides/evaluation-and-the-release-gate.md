---
title: Evaluation and the release gate
description: The frozen datasets, the strict metrics, the mock and live modes, and how a change is gated.
sidebar:
  order: 7
---

The platform's claims (no leakage, budgets respected, every item cited) are checked by an evaluation harness against **frozen, hash-locked datasets**, and a release gate compares the result with a recorded baseline.

## The suites

| Suite | Command | What it checks | Strict metrics (must be exact) |
|---|---|---|---|
| Extraction | `make eval-extraction` | Entities, facts, identity and valid time against a scenario gold set | cross-workspace links 0, unquarantined unparseable 0 |
| Memory | `make eval-memory` | Candidate precision and recall, recall@3, scopes | escalation violations 0, unmapped-subject leaks 0, procedural auto-activations 0 |
| Context | `make eval-context` | 16 frozen cases: leakage, budget, attribution, temporal correctness | leakage 0, budget adherence 100%, attribution 100% |
| Consolidation | `make eval-consolidation` | Reinforce clustering and the synthesis validator | every strict counter 0 |
| Conflicts | `make eval-conflicts` | 151 identity and conflict cases through the pure policy | mismatches 0 |
| Authorization | `make eval-authorization` | About 900 generated probes: every route × every caller class, plus frozen and deleted subjects | policy mismatches 0, leakage 0, server errors 0 |
| Injection | `make eval-injection` | The frozen injection corpus through every ingress | failed checks 0 |

`make eval-all` runs every suite against the **strict mock router** (offline and deterministic) and writes `eval/reports/<date>/all.json`. `make eval-gate` compares it with `eval/baselines/release.json`: strict metrics must be exact, the others may not regress past a tolerance.

## Freezing

Datasets are hash-locked (`eval/datasets/<suite>/v1/manifest.json`). If a file changes after freezing, `eval-all` reports it ("files changed since the freeze") instead of silently passing. Re-freezing is an owner act: `make eval-freeze`, then `python -m eval.gate --write-baseline`.

## Mock vs live

The mock run proves the **platform** logic deterministically. It says nothing about model quality, so each suite has a `-live` variant (`make eval-context-live`, ...) that runs the same cases through the real router and costs money. A model-affecting change (an alias, a price, a prompt, the ranker or packer version, the Graphiti pin) needs a recorded live gate: `make eval-gate-live`. CI requires the record.

## The wider release sweep

- `make test-release`: the leak suites, route walkers, injection, egress, revocation and trigger gates, run against **two API replicas**, with a report keyed to the security and acceptance ids.
- `make test-resilience`: failure injection (crashes, SIGKILL, lease expiry, Postgres and Neo4j restarts, router outage and latency, clock skew).
- `make perf-baseline`: steady, burst, search and long-document load with a size curve, freshness and cost.
- `make release-candidate`: every gate in order from a clean tree (it drops the dev volumes) and writes `docs/release/rc-report-<date>.md`.

## Writing a case for your domain

The CRM example has its own small scorer (`examples/domains/crm/benchmark/cases.yaml`): frozen cases with `expect`, `forbid`, `unknowns`, `leak` and a token budget, with the same thresholds. Copy it for your domain. A reviewer can check that your cases can fail: add a `forbid` that should trip and watch it.
