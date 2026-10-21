# Extraction evaluation (live, 2026-10-21)

Router mode: **live**. Scenarios: 7. Verdict: **FAIL**.

| metric | value | threshold |
|---|---|---|
| fact precision | 0.8571 | ≥ 0.8 (proposed) |
| fact recall | 1.0 | ≥ 0.8 (proposed) |
| status correct | 1.0 | |
| valid time correct | 0.3333 | |
| identity decisions correct | 1.0 | 1.0 |
| missed reverts | 0 | 0 (strict) |
| cross-workspace links | 0 | 0 (strict) |
| unparseable outputs left un-quarantined | 1 | 0 (strict) |
| chat calls per episode | 4.29 | recorded (DEC-11) |
| tokens per episode | 6263.7 | recorded |
| latency p50 / p95 (s) | 29.38 / 30.06 | recorded |

## E5 cosine of name pairs (ADR-013)

| group | min | mean | max |
|---|---|---|---|
| related | 0.8931 | 0.9338 | 0.9628 |
| same_name | 1.0 | 1.0 | 1.0 |
| unrelated | 0.7617 | 0.7922 | 0.8107 |

Suggested `IDENTITY_COS_MIN` (midpoint between the lowest related and the highest unrelated pair): **0.8519**

## Scenarios

| scenario | facts tp/fp/fn | stage | entities | identity | reviews | reverts | calls | seconds |
|---|---|---|---|---|---|---|---|---|
| backdated | 1/0/0 | ok | ok | ok | ok | ok | 5 | 30.06 |
| bad_json | 0/0/0 | WRONG | ok | ok | ok | ok | 3 | 17.32 |
| cyclist | 1/0/0 | ok | WRONG | ok | WRONG | ok | 4 | 42.04 |
| manager | 1/0/0 | ok | ok | ok | ok | ok | 4 | 29.47 |
| s_johnson | 0/0/0 | ok | ok | ok | ok | ok | 4 | 20.34 |
| evt003 | 2/1/0 | ok | ok | ok | ok | ok | 5 | 29.38 |
| newco | 1/0/0 | ok | ok | ok | ok | ok | 5 | 28.4 |

## Problems

- unquarantined_unparseable = 1 (strict: must be 0)
