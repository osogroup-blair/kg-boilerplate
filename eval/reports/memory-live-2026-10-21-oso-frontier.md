# Memory evaluation (live, 2026-10-21)

Router mode: **live**. Scenarios: 8. Verdict: **FAIL**.

| metric | value | threshold |
|---|---|---|
| candidate precision | 0.4 | ≥ 0.8 (proposed) |
| candidate recall | 0.4 | recorded |
| escalation violations | 0 | 0 (strict) |
| unmapped-subject leaks | 0 | 0 (strict) |
| procedural auto-activations | 0 | 0 (strict) |
| content-chosen scopes | 0 | 0 (strict) |
| gold memory recall@3 | 1.0 | ≥ 0.9 (proposed) |
| queries with a distractor in the top 3 | 0 | 0 (proposed) |
| model calls per event | 1 | recorded (DEC-11) |
| tokens per event | 732.4 | recorded |
| latency p50 / p95 (s) | 37.52 / 87.26 | recorded |

## Query cosine distribution (ADR-013)

| group | n | min | mean | max |
|---|---|---|---|---|
| gold | 4 | 0.7908 | 0.8302 | 0.873 |
| distractor | 4 | 0.6764 | 0.7124 | 0.7352 |
| filler | 28 | 0.6907 | 0.7384 | 0.7928 |

Suggested `MEMORY_VEC_MIN_COSINE` (midpoint between the lowest gold and the highest non-gold cosine): **None**

## Queries

| query | gold rank | distractor rank | top 3 |
|---|---|---|---|
| starting a business | 2 | 8 | Sarah Johnson wants options compared sid, Michael encourages Sarah Johnson's entre, Sarah books conference travel through th |
| entrepreneurship | 1 | 6 | Michael encourages Sarah Johnson's entre, Sarah Johnson wants options compared sid, Sarah books conference travel through th |
| weekend cycling club ride | 1 | 5 | Sarah Johnson is a member of a cycling c, Sarah's team ships a quarterly release o, Sarah Johnson wants options compared sid |
| how should options be compared when advising | 1 | 8 | Sarah Johnson wants options compared sid, Michael encourages Sarah Johnson's entre, Sarah books conference travel through th |

## Scenarios

| scenario | memories tp/fp/fn | stage | drops | reviews | escalations | leaks | procedural active | calls | seconds |
|---|---|---|---|---|---|---|---|---|---|
| mem_bad_json | 0/0/0 | WRONG | ok | ok | 0 | 0 | 0 | 1 | 33.0 |
| mem_cyclist | 1/1/0 | ok | ok | ok | 0 | 0 | 0 | 1 | 77.56 |
| mem_escalation | 0/1/1 | ok | WRONG | ok | 0 | 0 | 0 | 1 | 17.87 |
| mem_evt003 | 0/1/1 | ok | ok | ok | 0 | 0 | 0 | 1 | 26.03 |
| mem_evt004 | 1/0/0 | ok | ok | ok | 0 | 0 | 0 | 1 | 16.91 |
| mem_injection | 0/0/1 | ok | ok | WRONG | 0 | 0 | 0 | 1 | 42.05 |
| mem_trivial | 0/0/0 | ok | WRONG | ok | 0 | 0 | 0 | 1 | 87.26 |
| mem_unmapped | 0/0/0 | ok | WRONG | ok | 0 | 0 | 0 | 1 | 91.34 |

## Problems

- candidate_precision = 0.4 (needs 0.8)
