# Extraction evaluation (live, 2026-10-21)

Router mode: **live**. Scenarios: 7. Verdict: **FAIL**.

| metric | value | threshold |
|---|---|---|
| fact precision | 0.6667 | ≥ 0.8 (proposed) |
| fact recall | 0.6667 | ≥ 0.8 (proposed) |
| status correct | 1.0 | |
| valid time correct | 0.5 | |
| identity decisions correct | 1.0 | 1.0 |
| missed reverts | 0 | 0 (strict) |
| cross-workspace links | 0 | 0 (strict) |
| unparseable outputs left un-quarantined | 1 | 0 (strict) |
| chat calls per episode | 4.71 | recorded (DEC-11) |
| tokens per episode | 8219.4 | recorded |
| latency p50 / p95 (s) | 105.28 / 161.12 | recorded |

## E5 cosine of name pairs (ADR-013)

| group | min | mean | max |
|---|---|---|---|
| related | 0.8931 | 0.9338 | 0.9628 |
| same_name | 1.0 | 1.0 | 1.0 |
| unrelated | 0.7617 | 0.7922 | 0.8106 |

Suggested `IDENTITY_COS_MIN` (midpoint between the lowest related and the highest unrelated pair): **0.8518**

## Scenarios

| scenario | facts tp/fp/fn | stage | entities | identity | reviews | reverts | calls | seconds |
|---|---|---|---|---|---|---|---|---|
| backdated | 1/0/0 | ok | ok | ok | ok | ok | 5 | 108.64 |
| bad_json | 0/0/0 | WRONG | ok | ok | ok | ok | 3 | 71.62 |
| cyclist | 1/1/0 | ok | WRONG | ok | WRONG | ok | 6 | 210.01 |
| manager | 0/1/1 | ok | ok | ok | ok | ok | 4 | 105.28 |
| s_johnson | 0/0/0 | ok | ok | ok | ok | ok | 4 | 56.1 |
| evt003 | 1/0/1 | ok | ok | ok | ok | ok | 5 | 161.12 |
| newco | 1/0/0 | ok | ok | ok | ok | ok | 6 | 68.22 |

## Problems

- unquarantined_unparseable = 1 (strict: must be 0)
- fact_precision = 0.6667 (needs 0.8)
- fact_recall = 0.6667 (needs 0.8)
