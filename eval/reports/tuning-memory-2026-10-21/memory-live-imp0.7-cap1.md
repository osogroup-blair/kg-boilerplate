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
| gold memory recall@3 | 0.75 | ≥ 0.9 (proposed) |
| queries with a distractor in the top 3 | 1 | 0 (proposed) |
| model calls per event | 1 | recorded (DEC-11) |
| tokens per event | 706.9 | recorded |
| latency p50 / p95 (s) | 42.75 / 59.07 | recorded |

## Query cosine distribution (ADR-013)

| group | n | min | mean | max |
|---|---|---|---|---|
| gold | 3 | 0.8277 | 0.8495 | 0.8763 |
| distractor | 1 | 0.7195 | 0.7195 | 0.7195 |
| filler | 28 | 0.6907 | 0.7383 | 0.7824 |

Suggested `MEMORY_VEC_MIN_COSINE` (midpoint between the lowest gold and the highest non-gold cosine): **0.805**

## Queries

| query | gold rank | distractor rank | top 3 |
|---|---|---|---|
| starting a business | 1 | None | Michael consistently encourages Sarah Jo, Sarah Johnson prefers decision support t, Sarah books conference travel through th |
| entrepreneurship | 1 | None | Michael consistently encourages Sarah Jo, Sarah Johnson prefers decision support t, Sarah books conference travel through th |
| weekend cycling club ride | None | 3 | Sarah's team ships a quarterly release o, Sarah Johnson prefers decision support t, Michael consistently encourages Sarah Jo |
| how should options be compared when advising | 1 | None | Sarah Johnson prefers decision support t, Michael consistently encourages Sarah Jo, Sarah books conference travel through th |

## Scenarios

| scenario | memories tp/fp/fn | stage | drops | reviews | escalations | leaks | procedural active | calls | seconds |
|---|---|---|---|---|---|---|---|---|---|
| mem_bad_json | 0/0/0 | WRONG | ok | ok | 0 | 0 | 0 | 1 | 42.46 |
| mem_cyclist | 0/0/1 | ok | ok | ok | 0 | 0 | 0 | 1 | 84.81 |
| mem_escalation | 1/0/0 | ok | WRONG | ok | 0 | 0 | 0 | 1 | 22.91 |
| mem_evt003 | 0/1/1 | ok | ok | ok | 0 | 0 | 0 | 1 | 43.03 |
| mem_evt004 | 1/0/0 | ok | ok | ok | 0 | 0 | 0 | 1 | 19.12 |
| mem_injection | 0/0/1 | ok | ok | WRONG | 0 | 0 | 0 | 1 | 59.07 |
| mem_trivial | 0/0/0 | ok | WRONG | ok | 0 | 0 | 0 | 1 | 8.18 |
| mem_unmapped | 0/0/0 | ok | WRONG | ok | 0 | 0 | 0 | 1 | 55.98 |

## Problems

- candidate_precision = 0.6667 (needs 0.8)
- recall_at_3 = 0.75 (needs 0.9)
- distractor_in_top3 = 1 (must be 0)
