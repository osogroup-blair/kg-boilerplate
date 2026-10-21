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
| queries with a distractor in the top 3 | 0 | 0 (proposed) |
| model calls per event | 1 | recorded (DEC-11) |
| tokens per event | 769.4 | recorded |
| latency p50 / p95 (s) | 39.2 / 47.93 | recorded |

## Query cosine distribution (ADR-013)

| group | n | min | mean | max |
|---|---|---|---|---|
| gold | 3 | 0.7965 | 0.8337 | 0.8715 |
| distractor | 1 | 0.7134 | 0.7134 | 0.7134 |
| filler | 32 | 0.6907 | 0.7473 | 0.8861 |

Suggested `MEMORY_VEC_MIN_COSINE` (midpoint between the lowest gold and the highest non-gold cosine): **None**

## Queries

| query | gold rank | distractor rank | top 3 |
|---|---|---|---|
| starting a business | 2 | None | Sarah Johnson is considering entrepreneu, Michael encourages Sarah Johnson's entre, Sarah Johnson wants decision options com |
| entrepreneurship | 2 | None | Sarah Johnson is considering entrepreneu, Michael encourages Sarah Johnson's entre, Sarah Johnson wants decision options com |
| weekend cycling club ride | None | 6 | Sarah's team ships a quarterly release o, Sarah books conference travel through th, Sarah Johnson is considering entrepreneu |
| how should options be compared when advising | 1 | None | Sarah Johnson wants decision options com, Sarah Johnson is considering entrepreneu, Michael encourages Sarah Johnson's entre |

## Scenarios

| scenario | memories tp/fp/fn | stage | drops | reviews | escalations | leaks | procedural active | calls | seconds |
|---|---|---|---|---|---|---|---|---|---|
| mem_bad_json | 0/0/0 | WRONG | ok | ok | 0 | 0 | 0 | 1 | 38.92 |
| mem_cyclist | 0/0/1 | ok | ok | ok | 0 | 0 | 0 | 1 | 47.93 |
| mem_escalation | 1/0/0 | ok | WRONG | ok | 0 | 0 | 0 | 1 | 17.65 |
| mem_evt003 | 0/1/1 | ok | ok | ok | 0 | 0 | 0 | 1 | 40.47 |
| mem_evt004 | 1/0/0 | ok | ok | ok | 0 | 0 | 0 | 1 | 160.03 |
| mem_injection | 0/0/1 | ok | ok | WRONG | 0 | 0 | 0 | 1 | 37.38 |
| mem_trivial | 0/0/0 | ok | WRONG | ok | 0 | 0 | 0 | 1 | 16.51 |
| mem_unmapped | 0/0/0 | ok | WRONG | ok | 0 | 0 | 0 | 1 | 39.49 |

## Problems

- candidate_precision = 0.6667 (needs 0.8)
- recall_at_3 = 0.75 (needs 0.9)
