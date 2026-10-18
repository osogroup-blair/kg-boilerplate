# Memory evaluation (mock, 2026-10-18)

Router mode: **mock**. Scenarios: 8. Verdict: **PASS**.

| metric | value | threshold |
|---|---|---|
| candidate precision | 1.0 | ≥ 0.8 (proposed) |
| candidate recall | 1.0 | recorded |
| escalation violations | 0 | 0 (strict) |
| unmapped-subject leaks | 0 | 0 (strict) |
| procedural auto-activations | 0 | 0 (strict) |
| content-chosen scopes | 0 | 0 (strict) |
| gold memory recall@3 | 1.0 | ≥ 0.9 (proposed) |
| queries with a distractor in the top 3 | 0 | 0 (proposed) |
| model calls per event | 0.88 | recorded (DEC-11) |
| tokens per event | 512.9 | recorded |
| latency p50 / p95 (s) | 0.38 / 1.11 | recorded |

## Query cosine distribution (ADR-013)

| group | n | min | mean | max |
|---|---|---|---|---|
| gold | 4 | 0.1487 | 0.2564 | 0.375 |
| distractor | 4 | -0.1376 | -0.0205 | 0.0555 |
| filler | 28 | -0.165 | -0.0209 | 0.0894 |

Suggested `MEMORY_VEC_MIN_COSINE` (midpoint between the lowest gold and the highest non-gold cosine): **0.1191**

## Queries

| query | gold rank | distractor rank | top 3 |
|---|---|---|---|
| starting a business | 1 | None | Sarah is considering entrepreneurship: a |
| entrepreneurship | 1 | None | Sarah is considering entrepreneurship: a |
| weekend cycling club ride | 1 | None | Sarah went on a 40 km cycling club ride  |
| how should options be compared when advising | 1 | None | Present options with risks and trade-off |

## Scenarios

| scenario | memories tp/fp/fn | stage | drops | reviews | escalations | leaks | procedural active | calls | seconds |
|---|---|---|---|---|---|---|---|---|---|
| mem_bad_json | 0/0/0 | ok | ok | ok | 0 | 0 | 0 | 0 | 1.11 |
| mem_cyclist | 1/0/0 | ok | ok | ok | 0 | 0 | 0 | 1 | 1.22 |
| mem_escalation | 1/0/0 | ok | ok | ok | 0 | 0 | 0 | 1 | 0.37 |
| mem_evt003 | 1/0/0 | ok | ok | ok | 0 | 0 | 0 | 1 | 0.93 |
| mem_evt004 | 1/0/0 | ok | ok | ok | 0 | 0 | 0 | 1 | 0.39 |
| mem_injection | 1/0/0 | ok | ok | ok | 0 | 0 | 0 | 1 | 0.35 |
| mem_trivial | 0/0/0 | ok | ok | ok | 0 | 0 | 0 | 1 | 0.35 |
| mem_unmapped | 0/0/0 | ok | ok | ok | 0 | 0 | 0 | 1 | 0.35 |
