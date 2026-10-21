# Memory evaluation (live, 2026-10-21)

Router mode: **live**. Scenarios: 8. Verdict: **FAIL**.

| metric | value | threshold |
|---|---|---|
| candidate precision | 0.1429 | ≥ 0.8 (proposed) |
| candidate recall | 0.2 | recorded |
| escalation violations | 0 | 0 (strict) |
| unmapped-subject leaks | 0 | 0 (strict) |
| procedural auto-activations | 0 | 0 (strict) |
| content-chosen scopes | 0 | 0 (strict) |
| gold memory recall@3 | 1.0 | ≥ 0.9 (proposed) |
| queries with a distractor in the top 3 | 0 | 0 (proposed) |
| model calls per event | 1 | recorded (DEC-11) |
| tokens per event | 1007 | recorded |
| latency p50 / p95 (s) | 45.47 / 86.8 | recorded |

## Query cosine distribution (ADR-013)

| group | n | min | mean | max |
|---|---|---|---|---|
| gold | 4 | 0.7991 | 0.8246 | 0.8602 |
| distractor | 4 | 0.6908 | 0.7162 | 0.7311 |
| filler | 32 | 0.675 | 0.7377 | 0.8244 |

Suggested `MEMORY_VEC_MIN_COSINE` (midpoint between the lowest gold and the highest non-gold cosine): **None**

## Queries

| query | gold rank | distractor rank | top 3 |
|---|---|---|---|
| starting a business | 1 | 8 | Michael encourages Sarah Johnson's entre, Sarah Johnson wants decisions presented , Sarah books conference travel through th |
| entrepreneurship | 1 | 6 | Michael encourages Sarah Johnson's entre, Sarah Johnson wants decisions presented , Sarah books conference travel through th |
| weekend cycling club ride | 2 | 7 | Sarah Johnson is a member of the cycling, Sarah Johnson rode 40 km with Tom on Sat, Sarah's team ships a quarterly release o |
| how should options be compared when advising | 1 | 7 | Sarah Johnson wants decisions presented , Michael encourages Sarah Johnson's entre, Sarah books conference travel through th |

## Scenarios

| scenario | memories tp/fp/fn | stage | drops | reviews | escalations | leaks | procedural active | calls | seconds |
|---|---|---|---|---|---|---|---|---|---|
| mem_bad_json | 0/0/0 | WRONG | ok | ok | 0 | 0 | 0 | 1 | 86.8 |
| mem_cyclist | 0/2/1 | ok | ok | ok | 0 | 0 | 0 | 1 | 227.26 |
| mem_escalation | 0/1/1 | ok | WRONG | ok | 0 | 0 | 0 | 1 | 47.69 |
| mem_evt003 | 0/2/1 | ok | ok | ok | 0 | 0 | 0 | 1 | 43.26 |
| mem_evt004 | 1/0/0 | ok | ok | ok | 0 | 0 | 0 | 1 | 30.85 |
| mem_injection | 0/0/1 | ok | ok | WRONG | 0 | 0 | 0 | 1 | 26.79 |
| mem_trivial | 0/0/0 | ok | WRONG | ok | 0 | 0 | 0 | 1 | 8.33 |
| mem_unmapped | 0/1/0 | ok | WRONG | ok | 0 | 0 | 0 | 1 | 70.05 |

## Problems

- candidate_precision = 0.1429 (needs 0.8)
