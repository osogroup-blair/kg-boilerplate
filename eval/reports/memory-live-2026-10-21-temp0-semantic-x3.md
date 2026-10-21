# Memory evaluation (live, 2026-10-21)

Router mode: **live**. Scenarios: 24. Verdict: **FAIL**.

| metric | value | threshold |
|---|---|---|
| candidate precision | 0.5 | ≥ 0.8 (proposed) |
| candidate recall | 0.4 | recorded |
| escalation violations | 0 | 0 (strict) |
| unmapped-subject leaks | 0 | 0 (strict) |
| procedural auto-activations | 0 | 0 (strict) |
| content-chosen scopes | 0 | 0 (strict) |
| gold memory recall@3 | 1.0 | ≥ 0.9 (proposed) |
| queries with a distractor in the top 3 | 0 | 0 (proposed) |
| model calls per event | 1 | recorded (DEC-11) |
| tokens per event | 810.9 | recorded |
| latency p50 / p95 (s) | 22.56 / 47.81 | recorded |

## Query cosine distribution (ADR-013)

| group | n | min | mean | max |
|---|---|---|---|---|
| gold | 4 | 0.8017 | 0.8343 | 0.8715 |
| distractor | 4 | 0.7105 | 0.7273 | 0.7354 |
| filler | 36 | 0.675 | 0.7442 | 0.8644 |

Suggested `MEMORY_VEC_MIN_COSINE` (midpoint between the lowest gold and the highest non-gold cosine): **None**

## Queries

| query | gold rank | distractor rank | top 3 |
|---|---|---|---|
| starting a business | 2 | 9 | Sarah Johnson is considering entrepreneu, Michael encourages Sarah Johnson toward , Sarah Johnson prefers options presented  |
| entrepreneurship | 2 | 8 | Sarah Johnson is considering entrepreneu, Michael encourages Sarah Johnson toward , Sarah Johnson prefers options presented  |
| weekend cycling club ride | 2 | 6 | Sarah Johnson is a member of the cycling, Sarah Johnson completed a 40 km ride on , Sarah's team ships a quarterly release o |
| how should options be compared when advising | 1 | 6 | Sarah Johnson prefers options presented , Sarah Johnson is considering entrepreneu, Michael encourages Sarah Johnson toward  |

## Scenarios

| scenario | memories tp/fp/fn | stage | drops | reviews | escalations | leaks | procedural active | calls | seconds |
|---|---|---|---|---|---|---|---|---|---|
| mem_bad_json | 0/0/0 | WRONG | ok | ok | 0 | 0 | 0 | 1 | 16.83 |
| mem_cyclist | 0/2/1 | ok | ok | ok | 0 | 0 | 0 | 1 | 54.96 |
| mem_escalation | 1/0/0 | ok | WRONG | ok | 0 | 0 | 0 | 1 | 27.88 |
| mem_evt003 | 0/1/1 | ok | ok | ok | 0 | 0 | 0 | 1 | 101.17 |
| mem_evt004 | 1/0/0 | ok | ok | ok | 0 | 0 | 0 | 1 | 17.14 |
| mem_injection | 0/0/1 | ok | ok | WRONG | 0 | 0 | 0 | 1 | 17.72 |
| mem_trivial | 0/0/0 | ok | WRONG | ok | 0 | 0 | 0 | 1 | 7.51 |
| mem_unmapped | 0/0/0 | ok | WRONG | ok | 0 | 0 | 0 | 1 | 41.87 |
| mem_bad_json | 0/0/0 | WRONG | ok | ok | 0 | 0 | 0 | 1 | 14.18 |
| mem_cyclist | 0/0/1 | ok | ok | ok | 0 | 0 | 0 | 1 | 45.78 |
| mem_escalation | 1/0/0 | ok | WRONG | ok | 0 | 0 | 0 | 1 | 21.7 |
| mem_evt003 | 0/2/1 | ok | ok | ok | 0 | 0 | 0 | 1 | 36.03 |
| mem_evt004 | 1/0/0 | ok | ok | ok | 0 | 0 | 0 | 1 | 16.6 |
| mem_injection | 0/0/1 | ok | ok | WRONG | 0 | 0 | 0 | 1 | 18.49 |
| mem_trivial | 0/0/0 | ok | WRONG | ok | 0 | 0 | 0 | 1 | 8.13 |
| mem_unmapped | 0/0/0 | ok | WRONG | ok | 0 | 0 | 0 | 1 | 38.68 |
| mem_bad_json | 0/0/0 | WRONG | ok | ok | 0 | 0 | 0 | 1 | 14.83 |
| mem_cyclist | 0/0/1 | ok | ok | ok | 0 | 0 | 0 | 1 | 25.47 |
| mem_escalation | 1/0/0 | ok | WRONG | ok | 0 | 0 | 0 | 1 | 24.51 |
| mem_evt003 | 0/1/1 | ok | ok | ok | 0 | 0 | 0 | 1 | 37.59 |
| mem_evt004 | 1/0/0 | ok | ok | ok | 0 | 0 | 0 | 1 | 19.02 |
| mem_injection | 0/0/1 | ok | ok | WRONG | 0 | 0 | 0 | 1 | 23.43 |
| mem_trivial | 0/0/0 | ok | WRONG | ok | 0 | 0 | 0 | 1 | 9.79 |
| mem_unmapped | 0/0/0 | ok | WRONG | ok | 0 | 0 | 0 | 1 | 47.81 |

## Repeated runs (3; semantic match ON)

Precision mean 0.5222 (range 0.4 to 0.6667); recall mean 0.4 (range 0.4 to 0.4); strict counters all zero in every run: True.

| run | tp | fp | fn | precision | recall |
|---|---|---|---|---|---|
| 1 | 2 | 3 | 3 | 0.4 | 0.4 |
| 2 | 2 | 2 | 3 | 0.5 | 0.4 |
| 3 | 2 | 1 | 3 | 0.6667 | 0.4 |

| scenario | tp per run | fp per run | fn per run | seconds mean (min-max) |
|---|---|---|---|---|
| mem_bad_json | [0, 0, 0] | [0, 0, 0] | [0, 0, 0] | 15.28 (14.18-16.83) |
| mem_cyclist | [0, 0, 0] | [2, 0, 0] | [1, 1, 1] | 42.07 (25.47-54.96) |
| mem_escalation | [1, 1, 1] | [0, 0, 0] | [0, 0, 0] | 24.6967 (21.7-27.88) |
| mem_evt003 | [0, 0, 0] | [1, 2, 1] | [1, 1, 1] | 58.2633 (36.03-101.17) |
| mem_evt004 | [1, 1, 1] | [0, 0, 0] | [0, 0, 0] | 17.5867 (16.6-19.02) |
| mem_injection | [0, 0, 0] | [0, 0, 0] | [1, 1, 1] | 19.88 (17.72-23.43) |
| mem_trivial | [0, 0, 0] | [0, 0, 0] | [0, 0, 0] | 8.4767 (7.51-9.79) |
| mem_unmapped | [0, 0, 0] | [0, 0, 0] | [0, 0, 0] | 42.7867 (38.68-47.81) |

## Problems

- candidate_precision = 0.5 (needs 0.8)
