# Memory evaluation (live, 2026-10-21)

Router mode: **live**. Scenarios: 16. Verdict: **FAIL**.

| metric | value | threshold |
|---|---|---|
| candidate precision | 0.4286 | ≥ 0.8 (proposed) |
| candidate recall | 0.3 | recorded |
| escalation violations | 0 | 0 (strict) |
| unmapped-subject leaks | 0 | 0 (strict) |
| procedural auto-activations | 0 | 0 (strict) |
| content-chosen scopes | 0 | 0 (strict) |
| gold memory recall@3 | 1.0 | ≥ 0.9 (proposed) |
| queries with a distractor in the top 3 | 0 | 0 (proposed) |
| model calls per event | 1 | recorded (DEC-11) |
| tokens per event | 744.8 | recorded |
| latency p50 / p95 (s) | 30.84 / 122.89 | recorded |

## Query cosine distribution (ADR-013)

| group | n | min | mean | max |
|---|---|---|---|---|
| gold | 4 | 0.8019 | 0.8318 | 0.859 |
| distractor | 4 | 0.6764 | 0.7106 | 0.7352 |
| filler | 28 | 0.6907 | 0.7386 | 0.7927 |

Suggested `MEMORY_VEC_MIN_COSINE` (midpoint between the lowest gold and the highest non-gold cosine): **0.7973**

## Queries

| query | gold rank | distractor rank | top 3 |
|---|---|---|---|
| starting a business | 1 | 8 | Michael encourages Sarah Johnson in her , Sarah Johnson prefers options laid out s, Sarah books conference travel through th |
| entrepreneurship | 1 | 6 | Michael encourages Sarah Johnson in her , Sarah Johnson prefers options laid out s, Sarah books conference travel through th |
| weekend cycling club ride | 1 | 6 | Sarah Johnson is a member of a cycling c, Sarah's team ships a quarterly release o, Sarah books conference travel through th |
| how should options be compared when advising | 1 | 8 | Sarah Johnson prefers options laid out s, Michael encourages Sarah Johnson in her , Sarah books conference travel through th |

## Scenarios

| scenario | memories tp/fp/fn | stage | drops | reviews | escalations | leaks | procedural active | calls | seconds |
|---|---|---|---|---|---|---|---|---|---|
| mem_bad_json | 0/0/0 | WRONG | ok | ok | 0 | 0 | 0 | 1 | 18.11 |
| mem_cyclist | 0/1/1 | ok | ok | ok | 0 | 0 | 0 | 1 | 186.21 |
| mem_escalation | 0/1/1 | ok | WRONG | ok | 0 | 0 | 0 | 1 | 30.32 |
| mem_evt003 | 0/1/1 | ok | ok | ok | 0 | 0 | 0 | 1 | 40.6 |
| mem_evt004 | 1/0/0 | ok | ok | ok | 0 | 0 | 0 | 1 | 21.17 |
| mem_injection | 0/0/1 | ok | ok | WRONG | 0 | 0 | 0 | 1 | 36.27 |
| mem_trivial | 0/0/0 | ok | WRONG | ok | 0 | 0 | 0 | 1 | 9.16 |
| mem_unmapped | 0/0/0 | ok | WRONG | ok | 0 | 0 | 0 | 1 | 58.65 |
| mem_bad_json | 0/0/0 | WRONG | ok | ok | 0 | 0 | 0 | 1 | 17.5 |
| mem_cyclist | 0/0/1 | ok | ok | ok | 0 | 0 | 0 | 1 | 122.89 |
| mem_escalation | 1/0/0 | ok | WRONG | ok | 0 | 0 | 0 | 1 | 31.36 |
| mem_evt003 | 0/1/1 | ok | ok | ok | 0 | 0 | 0 | 1 | 49.91 |
| mem_evt004 | 1/0/0 | ok | ok | ok | 0 | 0 | 0 | 1 | 18.62 |
| mem_injection | 0/0/1 | ok | ok | WRONG | 0 | 0 | 0 | 1 | 20.03 |
| mem_trivial | 0/0/0 | ok | WRONG | ok | 0 | 0 | 0 | 1 | 14.99 |
| mem_unmapped | 0/0/0 | ok | WRONG | ok | 0 | 0 | 0 | 1 | 49.96 |

## Repeated runs (2; semantic match ON)

Precision mean 0.4583 (range 0.25 to 0.6667); recall mean 0.3 (range 0.2 to 0.4); strict counters all zero in every run: True.

| run | tp | fp | fn | precision | recall |
|---|---|---|---|---|---|
| 1 | 1 | 3 | 4 | 0.25 | 0.2 |
| 2 | 2 | 1 | 3 | 0.6667 | 0.4 |

| scenario | tp per run | fp per run | fn per run | seconds mean (min-max) |
|---|---|---|---|---|
| mem_bad_json | [0, 0] | [0, 0] | [0, 0] | 17.805 (17.5-18.11) |
| mem_cyclist | [0, 0] | [1, 0] | [1, 1] | 154.55 (122.89-186.21) |
| mem_escalation | [0, 1] | [1, 0] | [1, 0] | 30.84 (30.32-31.36) |
| mem_evt003 | [0, 0] | [1, 1] | [1, 1] | 45.255 (40.6-49.91) |
| mem_evt004 | [1, 1] | [0, 0] | [0, 0] | 19.895 (18.62-21.17) |
| mem_injection | [0, 0] | [0, 0] | [1, 1] | 28.15 (20.03-36.27) |
| mem_trivial | [0, 0] | [0, 0] | [0, 0] | 12.075 (9.16-14.99) |
| mem_unmapped | [0, 0] | [0, 0] | [0, 0] | 54.305 (49.96-58.65) |

## Problems

- candidate_precision = 0.4286 (needs 0.8)
