# Memory evaluation (live, 2026-10-21)

Router mode: **live**. Scenarios: 8. Verdict: **FAIL**.

| metric | value | threshold |
|---|---|---|
| candidate precision | 0.6667 | ≥ 0.8 (proposed) |
| candidate recall | 0.4 | recorded |
| escalation violations | 0 | 0 (strict) |
| unmapped-subject leaks | 0 | 0 (strict) |
| procedural auto-activations | 0 | 0 (strict) |
| content-chosen scopes | 0 | 0 (strict) |
| gold memory recall@3 | 1.0 | ≥ 0.9 (proposed) |
| queries with a distractor in the top 3 | 0 | 0 (proposed) |
| model calls per event | 1 | recorded (DEC-11) |
| tokens per event | 781.5 | recorded |
| latency p50 / p95 (s) | 46.53 / 81.62 | recorded |

## Query cosine distribution (ADR-013)

| group | n | min | mean | max |
|---|---|---|---|---|
| gold | 4 | 0.8052 | 0.8379 | 0.8612 |
| distractor | 4 | 0.7065 | 0.7273 | 0.7421 |
| filler | 32 | 0.675 | 0.7376 | 0.8244 |

Suggested `MEMORY_VEC_MIN_COSINE` (midpoint between the lowest gold and the highest non-gold cosine): **None**

## Queries

| query | gold rank | distractor rank | top 3 |
|---|---|---|---|
| starting a business | 1 | 7 | Michael actively encourages Sarah Johnso, Sarah Johnson prefers side-by-side optio, Sarah books conference travel through th |
| entrepreneurship | 1 | 7 | Michael actively encourages Sarah Johnso, Sarah Johnson prefers side-by-side optio, Sarah books conference travel through th |
| weekend cycling club ride | 2 | 6 | Sarah Johnson is a member of the cycling, Sarah Johnson completed a 40 km cycling , Sarah's team ships a quarterly release o |
| how should options be compared when advising | 1 | 6 | Sarah Johnson prefers side-by-side optio, Michael actively encourages Sarah Johnso, Sarah books conference travel through th |

## Scenarios

| scenario | memories tp/fp/fn | stage | drops | reviews | escalations | leaks | procedural active | calls | seconds |
|---|---|---|---|---|---|---|---|---|---|
| mem_bad_json | 0/0/0 | WRONG | ok | ok | 0 | 0 | 0 | 1 | 16.89 |
| mem_cyclist | 0/0/1 | ok | ok | ok | 0 | 0 | 0 | 1 | 81.62 |
| mem_escalation | 1/0/0 | ok | WRONG | ok | 0 | 0 | 0 | 1 | 27.69 |
| mem_evt003 | 0/1/1 | ok | ok | ok | 0 | 0 | 0 | 1 | 60.42 |
| mem_evt004 | 1/0/0 | ok | ok | ok | 0 | 0 | 0 | 1 | 65.63 |
| mem_injection | 0/0/1 | ok | ok | WRONG | 0 | 0 | 0 | 1 | 32.63 |
| mem_trivial | 0/0/0 | ok | WRONG | ok | 0 | 0 | 0 | 1 | 29.94 |
| mem_unmapped | 0/0/0 | ok | WRONG | ok | 0 | 0 | 0 | 1 | 93.65 |

## Problems

- candidate_precision = 0.6667 (needs 0.8)
