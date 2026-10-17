# Context evaluation (mock, 2026-10-15)

Router mode: **mock**. Cases: 16. Ranker: `ctx-rank.v1`. Tokenizer: `tiktoken:max(o200k_base,cl100k_base)+margin0.1`. Verdict: **PASS**.

| metric | value | threshold |
|---|---|---|
| authorisation leakage | 0 | 0 (strict) |
| budget adherence | 1.0 | 100% (strict) |
| attribution | 1.0 | 100% (strict) |
| evidence recall (cases 1-8) | 1.0 | ≥ 0.9 (proposed) |
| irrelevant inclusion | 0.0 | ≤ 0.1 (proposed) |
| forbidden items packed | 0 | 0 |
| temporal correctness | 1.0 | 100% (proposed) |
| summarised fallback | None | recorded |
| tokens per call | 238.3 | recorded (DEC-11) |
| latency p50 / p95 (s) | 0.039 / 0.069 | recorded (DEC-11) |

## Cases

| case | status | recall | irrelevant | temporal | attribution | leaks | budget | tokens |
|---|---|---|---|---|---|---|---|---|
| C01_current_employer | complete | 1.0 | 0.0 | ok | 1.0 | 0 | ok | 149/300 |
| C02_historical_employer | complete | 1.0 | 0.0 | ok | 1.0 | 0 | ok | 98/300 |
| C03_aspiration_and_conflicting_evidence | complete | 1.0 | 0.0 | ok | 1.0 | 0 | ok | 438/600 |
| C04_shared_agent_finding | complete | 1.0 | 0.0 | ok | 1.0 | 0 | ok | 378/600 |
| C05_unrelated_preference_distractor | complete | 1.0 | 0.0 | ok | 1.0 | 0 | ok | 77/300 |
| C06_private_memory_not_allowed | complete | 1.0 | 1.0 | ok | 1.0 | 0 | ok | 378/600 |
| C07_insufficient_evidence | insufficient_evidence | 1.0 | 0.0 | ok | 1.0 | 0 | ok | 77/600 |
| C08_backdated_update | insufficient_evidence | 1.0 | 0.0 | ok | 1.0 | 0 | ok | 54/300 |
| C09_budget_250_vs_1200 | complete | 1.0 | 0.0 | ok | 1.0 | 0 | ok | 438/1200 |
| C10_graph_backend_missing | partial | 1.0 | 0.0 | ok | 1.0 | 0 | ok | 438/600 |
| C11_vector_backend_missing | partial | 1.0 | 0.0 | ok | 1.0 | 0 | ok | 438/600 |
| C12_foreign_workspace | denied | 1.0 | 0.0 | ok | 1.0 | 0 | ok | 0/None |
| C13_injection_in_a_memory | complete | 1.0 | 1.0 | ok | 1.0 | 0 | ok | 537/600 |
| C14_summarized_faithfulness | complete | 1.0 | 1.0 | ok | 1.0 | 0 | ok | 207/1200 |
| C15_hidden_person_is_denied | denied | 1.0 | 0.0 | ok | 1.0 | 0 | ok | 0/None |
| C16_hidden_relationship_is_absent | complete | 1.0 | 0.0 | ok | 1.0 | 0 | ok | 75/300 |
