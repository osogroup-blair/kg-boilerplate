# Memory evaluation (live, 2026-10-21)

Router mode: **live**. Scenarios: 16. Verdict: **FAIL**.

| metric | value | threshold |
|---|---|---|
| candidate precision | 0.4167 | ≥ 0.8 (proposed) |
| candidate recall | 0.5 | recorded |
| escalation violations | 0 | 0 (strict) |
| unmapped-subject leaks | 0 | 0 (strict) |
| procedural auto-activations | 0 | 0 (strict) |
| content-chosen scopes | 0 | 0 (strict) |
| gold memory recall@3 | 0.75 | ≥ 0.9 (proposed) |
| queries with a distractor in the top 3 | 0 | 0 (proposed) |
| model calls per event | 1 | recorded (DEC-11) |
| tokens per event | 901.8 | recorded |
| latency p50 / p95 (s) | 30.56 / 55.72 | recorded |

## Query cosine distribution (ADR-013)

| group | n | min | mean | max |
|---|---|---|---|---|
| gold | 3 | 0.8109 | 0.8435 | 0.8752 |
| distractor | 1 | 0.7177 | 0.7177 | 0.7177 |
| filler | 32 | 0.6907 | 0.7437 | 0.8584 |

Suggested `MEMORY_VEC_MIN_COSINE` (midpoint between the lowest gold and the highest non-gold cosine): **None**

## Queries

| query | gold rank | distractor rank | top 3 |
|---|---|---|---|
| starting a business | 1 | None | Michael encourages Sarah Johnson toward , Sarah Johnson is considering entrepreneu, Sarah Johnson prefers side-by-side optio |
| entrepreneurship | 2 | None | Sarah Johnson is considering entrepreneu, Michael encourages Sarah Johnson toward , Sarah Johnson prefers side-by-side optio |
| weekend cycling club ride | None | 6 | Sarah's team ships a quarterly release o, Sarah Johnson is considering entrepreneu, Sarah books conference travel through th |
| how should options be compared when advising | 1 | None | Sarah Johnson prefers side-by-side optio, Michael encourages Sarah Johnson toward , Sarah Johnson is considering entrepreneu |

## Scenarios

| scenario | memories tp/fp/fn | stage | drops | reviews | escalations | leaks | procedural active | calls | seconds |
|---|---|---|---|---|---|---|---|---|---|
| mem_bad_json | 0/0/0 | WRONG | ok | ok | 0 | 0 | 0 | 1 | 13.87 |
| mem_cyclist | 1/1/0 | ok | ok | ok | 0 | 0 | 0 | 1 | 55.72 |
| mem_escalation | 1/0/0 | ok | WRONG | ok | 0 | 0 | 0 | 1 | 31.33 |
| mem_evt003 | 0/2/1 | ok | ok | ok | 0 | 0 | 0 | 1 | 40.59 |
| mem_evt004 | 1/0/0 | ok | ok | ok | 0 | 0 | 0 | 1 | 23.76 |
| mem_injection | 0/0/1 | ok | ok | WRONG | 0 | 0 | 0 | 1 | 31.43 |
| mem_trivial | 0/0/0 | ok | WRONG | ok | 0 | 0 | 0 | 1 | 8.07 |
| mem_unmapped | 0/0/0 | ok | WRONG | ok | 0 | 0 | 0 | 1 | 43.64 |
| mem_bad_json | 0/0/0 | WRONG | ok | ok | 0 | 0 | 0 | 1 | 16.68 |
| mem_cyclist | 0/2/1 | ok | ok | ok | 0 | 0 | 0 | 1 | 63.06 |
| mem_escalation | 1/0/0 | ok | WRONG | ok | 0 | 0 | 0 | 1 | 29.79 |
| mem_evt003 | 0/2/1 | ok | ok | ok | 0 | 0 | 0 | 1 | 37.99 |
| mem_evt004 | 1/0/0 | ok | ok | ok | 0 | 0 | 0 | 1 | 32.21 |
| mem_injection | 0/0/1 | ok | ok | WRONG | 0 | 0 | 0 | 1 | 25.24 |
| mem_trivial | 0/0/0 | ok | WRONG | ok | 0 | 0 | 0 | 1 | 7.84 |
| mem_unmapped | 0/0/0 | ok | WRONG | ok | 0 | 0 | 0 | 1 | 29.71 |

## Repeated runs (2; semantic match ON)

Precision mean 0.4166 (range 0.3333 to 0.5); recall mean 0.5 (range 0.4 to 0.6); strict counters all zero in every run: True.

| run | tp | fp | fn | precision | recall |
|---|---|---|---|---|---|
| 1 | 3 | 3 | 2 | 0.5 | 0.6 |
| 2 | 2 | 4 | 3 | 0.3333 | 0.4 |

| scenario | tp per run | fp per run | fn per run | seconds mean (min-max) |
|---|---|---|---|---|
| mem_bad_json | [0, 0] | [0, 0] | [0, 0] | 15.275 (13.87-16.68) |
| mem_cyclist | [1, 0] | [1, 2] | [0, 1] | 59.39 (55.72-63.06) |
| mem_escalation | [1, 1] | [0, 0] | [0, 0] | 30.56 (29.79-31.33) |
| mem_evt003 | [0, 0] | [2, 2] | [1, 1] | 39.29 (37.99-40.59) |
| mem_evt004 | [1, 1] | [0, 0] | [0, 0] | 27.985 (23.76-32.21) |
| mem_injection | [0, 0] | [0, 0] | [1, 1] | 28.335 (25.24-31.43) |
| mem_trivial | [0, 0] | [0, 0] | [0, 0] | 7.955 (7.84-8.07) |
| mem_unmapped | [0, 0] | [0, 0] | [0, 0] | 36.675 (29.71-43.64) |

## Problems

- candidate_precision = 0.4167 (needs 0.8)
- recall_at_3 = 0.75 (needs 0.9)
