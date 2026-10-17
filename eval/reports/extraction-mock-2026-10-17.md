# Extraction evaluation (mock, 2026-10-17)

Router mode: **mock**. Scenarios: 7. Verdict: **PASS**.

| metric | value | threshold |
|---|---|---|
| fact precision | 1.0 | ≥ 0.8 (proposed) |
| fact recall | 1.0 | ≥ 0.8 (proposed) |
| status correct | 1.0 | |
| valid time correct | 1.0 | |
| identity decisions correct | 1.0 | 1.0 |
| missed reverts | 0 | 0 (strict) |
| cross-workspace links | 0 | 0 (strict) |
| unparseable outputs left un-quarantined | 0 | 0 (strict) |
| chat calls per episode | 3.71 | recorded (DEC-11) |
| tokens per episode | 4145.6 | recorded |
| latency p50 / p95 (s) | 0.81 / 1.32 | recorded |

## E5 cosine of name pairs (ADR-013)

| group | min | mean | max |
|---|---|---|---|
| related | 0.0 | 0.3155 | 0.6667 |
| same_name | 1.0 | 1.0 | 1.0 |
| unrelated | 0.0 | 0.0 | 0.0 |

Suggested `IDENTITY_COS_MIN` (midpoint between the lowest related and the highest unrelated pair): **None**

## Scenarios

| scenario | facts tp/fp/fn | stage | entities | identity | reviews | reverts | calls | seconds |
|---|---|---|---|---|---|---|---|---|
| backdated | 1/0/0 | ok | ok | ok | ok | ok | 4 | 1.03 |
| bad_json | 0/0/0 | ok | ok | ok | ok | ok | 0 | 4.3 |
| cyclist | 1/0/0 | ok | ok | ok | ok | ok | 4 | 1.32 |
| manager | 1/0/0 | ok | ok | ok | ok | ok | 4 | 0.6 |
| s_johnson | 0/0/0 | ok | ok | ok | ok | ok | 4 | 0.58 |
| evt003 | 2/0/0 | ok | ok | ok | ok | ok | 6 | 0.81 |
| newco | 1/0/0 | ok | ok | ok | ok | ok | 4 | 0.63 |
