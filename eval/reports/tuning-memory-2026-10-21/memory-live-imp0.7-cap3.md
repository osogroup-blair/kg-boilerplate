# Memory evaluation (live, 2026-10-21)

Router mode: **live**. Scenarios: 8. Verdict: **FAIL**.

| metric | value | threshold |
|---|---|---|
| candidate precision | 0.2 | ≥ 0.8 (proposed) |
| candidate recall | 0.2 | recorded |
| escalation violations | 0 | 0 (strict) |
| unmapped-subject leaks | 0 | 0 (strict) |
| procedural auto-activations | 0 | 0 (strict) |
| content-chosen scopes | 0 | 0 (strict) |
| gold memory recall@3 | 0.75 | ≥ 0.9 (proposed) |
| queries with a distractor in the top 3 | 1 | 0 (proposed) |
| model calls per event | 1 | recorded (DEC-11) |
| tokens per event | 743 | recorded |
| latency p50 / p95 (s) | 41.84 / 59.08 | recorded |

## Query cosine distribution (ADR-013)

| group | n | min | mean | max |
|---|---|---|---|---|
| gold | 3 | 0.82 | 0.8469 | 0.8686 |
| distractor | 1 | 0.739 | 0.739 | 0.739 |
| filler | 28 | 0.6907 | 0.7387 | 0.7873 |

Suggested `MEMORY_VEC_MIN_COSINE` (midpoint between the lowest gold and the highest non-gold cosine): **0.8037**

## Queries

| query | gold rank | distractor rank | top 3 |
|---|---|---|---|
| starting a business | 1 | None | Michael encourages Sarah Johnson to purs, Sarah Johnson prefers a structured decis, Sarah books conference travel through th |
| entrepreneurship | 1 | None | Michael encourages Sarah Johnson to purs, Sarah Johnson prefers a structured decis, Sarah books conference travel through th |
| weekend cycling club ride | None | 1 | Michael encourages Sarah Johnson to purs, Sarah's team ships a quarterly release o, Sarah Johnson prefers a structured decis |
| how should options be compared when advising | 1 | None | Sarah Johnson prefers a structured decis, Michael encourages Sarah Johnson to purs, Sarah books conference travel through th |

## Scenarios

| scenario | memories tp/fp/fn | stage | drops | reviews | escalations | leaks | procedural active | calls | seconds |
|---|---|---|---|---|---|---|---|---|---|
| mem_bad_json | 0/0/0 | WRONG | ok | ok | 0 | 0 | 0 | 1 | 16.13 |
| mem_cyclist | 0/2/1 | ok | ok | ok | 0 | 0 | 0 | 1 | 42.81 |
| mem_escalation | 0/1/1 | ok | WRONG | ok | 0 | 0 | 0 | 1 | 29.68 |
| mem_evt003 | 0/1/1 | ok | ok | ok | 0 | 0 | 0 | 1 | 41.67 |
| mem_evt004 | 1/0/0 | ok | ok | ok | 0 | 0 | 0 | 1 | 17.51 |
| mem_injection | 0/0/1 | ok | ok | WRONG | 0 | 0 | 0 | 1 | 59.08 |
| mem_trivial | 0/0/0 | ok | WRONG | ok | 0 | 0 | 0 | 1 | 42.01 |
| mem_unmapped | 0/0/0 | ok | WRONG | ok | 0 | 0 | 0 | 1 | 86.49 |

## Problems

- candidate_precision = 0.2 (needs 0.8)
- recall_at_3 = 0.75 (needs 0.9)
- distractor_in_top3 = 1 (must be 0)
