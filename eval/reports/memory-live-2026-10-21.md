# Memory evaluation (live, 2026-10-21)

Router mode: **live**. Scenarios: 8. Verdict: **FAIL**.

| metric | value | threshold |
|---|---|---|
| candidate precision | 0.3333 | ≥ 0.8 (proposed) |
| candidate recall | 0.2 | recorded |
| escalation violations | 0 | 0 (strict) |
| unmapped-subject leaks | 0 | 0 (strict) |
| procedural auto-activations | 0 | 0 (strict) |
| content-chosen scopes | 0 | 0 (strict) |
| gold memory recall@3 | 1.0 | ≥ 0.9 (proposed) |
| queries with a distractor in the top 3 | 0 | 0 (proposed) |
| model calls per event | 1 | recorded (DEC-11) |
| tokens per event | 748.8 | recorded |
| latency p50 / p95 (s) | 96.66 / 145.74 | recorded |

## Query cosine distribution (ADR-013)

| group | n | min | mean | max |
|---|---|---|---|---|
| gold | 4 | 0.8017 | 0.8386 | 0.8681 |
| distractor | 4 | 0.7209 | 0.7303 | 0.7362 |
| filler | 36 | 0.6764 | 0.7456 | 0.8716 |

Suggested `MEMORY_VEC_MIN_COSINE` (midpoint between the lowest gold and the highest non-gold cosine): **None**

## Queries

| query | gold rank | distractor rank | top 3 |
|---|---|---|---|
| starting a business | 1 | 8 | Michael encourages Sarah Johnson toward , Sarah Johnson is considering entrepreneu, Sarah Johnson wants options compared sid |
| entrepreneurship | 2 | 7 | Sarah Johnson is considering entrepreneu, Michael encourages Sarah Johnson toward , Sarah Johnson wants options compared sid |
| weekend cycling club ride | 1 | 4 | Sarah Johnson completed a 40 km cycling , Sarah Johnson is a member of a cycling c, Sarah's team ships a quarterly release o |
| how should options be compared when advising | 1 | 4 | Sarah Johnson wants options compared sid, Michael encourages Sarah Johnson toward , Sarah Johnson is considering entrepreneu |

## Scenarios

| scenario | memories tp/fp/fn | stage | drops | reviews | escalations | leaks | procedural active | calls | seconds |
|---|---|---|---|---|---|---|---|---|---|
| mem_bad_json | 0/0/0 | WRONG | ok | ok | 0 | 0 | 0 | 1 | 117.29 |
| mem_cyclist | 0/0/1 | ok | ok | ok | 0 | 0 | 0 | 1 | 154.01 |
| mem_escalation | 0/1/1 | ok | WRONG | ok | 0 | 0 | 0 | 1 | 79.52 |
| mem_evt003 | 0/1/1 | ok | ok | ok | 0 | 0 | 0 | 1 | 78.93 |
| mem_evt004 | 1/0/0 | ok | ok | ok | 0 | 0 | 0 | 1 | 59.31 |
| mem_injection | 0/0/1 | ok | ok | WRONG | 0 | 0 | 0 | 1 | 86.86 |
| mem_trivial | 0/0/0 | ok | WRONG | ok | 0 | 0 | 0 | 1 | 145.74 |
| mem_unmapped | 0/0/0 | ok | WRONG | ok | 0 | 0 | 0 | 1 | 106.45 |

## Problems

- candidate_precision = 0.3333 (needs 0.8)
