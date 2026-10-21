# Memory evaluation (live, 2026-10-21)

Router mode: **live**. Scenarios: 8. Verdict: **FAIL**.

| metric | value | threshold |
|---|---|---|
| candidate precision | 0.25 | ≥ 0.8 (proposed) |
| candidate recall | 0.2 | recorded |
| escalation violations | 0 | 0 (strict) |
| unmapped-subject leaks | 0 | 0 (strict) |
| procedural auto-activations | 0 | 0 (strict) |
| content-chosen scopes | 0 | 0 (strict) |
| gold memory recall@3 | 1.0 | ≥ 0.9 (proposed) |
| queries with a distractor in the top 3 | 1 | 0 (proposed) |
| model calls per event | 1 | recorded (DEC-11) |
| tokens per event | 790.2 | recorded |
| latency p50 / p95 (s) | 42.47 / 55.82 | recorded |

## Query cosine distribution (ADR-013)

| group | n | min | mean | max |
|---|---|---|---|---|
| gold | 4 | 0.8174 | 0.8473 | 0.8701 |
| distractor | 4 | 0.7215 | 0.7388 | 0.7554 |
| filler | 28 | 0.6907 | 0.7378 | 0.7847 |

Suggested `MEMORY_VEC_MIN_COSINE` (midpoint between the lowest gold and the highest non-gold cosine): **0.801**

## Queries

| query | gold rank | distractor rank | top 3 |
|---|---|---|---|
| starting a business | 1 | 5 | Michael encourages Sarah Johnson's entre, Sarah Johnson prefers options laid out s, Sarah books conference travel through th |
| entrepreneurship | 1 | 4 | Michael encourages Sarah Johnson's entre, Sarah Johnson prefers options laid out s, Sarah books conference travel through th |
| weekend cycling club ride | 1 | 3 | Sarah Johnson is a cycling club member w, Sarah's team ships a quarterly release o, Michael encourages Sarah Johnson's entre |
| how should options be compared when advising | 1 | 4 | Sarah Johnson prefers options laid out s, Michael encourages Sarah Johnson's entre, Sarah books conference travel through th |

## Scenarios

| scenario | memories tp/fp/fn | stage | drops | reviews | escalations | leaks | procedural active | calls | seconds |
|---|---|---|---|---|---|---|---|---|---|
| mem_bad_json | 0/0/0 | WRONG | ok | ok | 0 | 0 | 0 | 1 | 55.82 |
| mem_cyclist | 0/0/1 | ok | ok | ok | 0 | 0 | 0 | 1 | 53.62 |
| mem_escalation | 0/1/1 | ok | WRONG | ok | 0 | 0 | 0 | 1 | 34.32 |
| mem_evt003 | 0/1/1 | ok | ok | ok | 0 | 0 | 0 | 1 | 50.62 |
| mem_evt004 | 1/0/0 | ok | ok | ok | 0 | 0 | 0 | 1 | 17.55 |
| mem_injection | 0/0/1 | ok | ok | WRONG | 0 | 0 | 0 | 1 | 23.8 |
| mem_trivial | 0/0/0 | ok | WRONG | ok | 0 | 0 | 0 | 1 | 14.05 |
| mem_unmapped | 0/1/0 | ok | WRONG | ok | 0 | 0 | 0 | 1 | 75.59 |

## Problems

- candidate_precision = 0.25 (needs 0.8)
- distractor_in_top3 = 1 (must be 0)
