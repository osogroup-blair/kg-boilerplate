# Context evaluation (live, 2026-10-21)

Router mode: **live**. Cases: 16. Ranker: `ctx-rank.v1`. Tokenizer: `tiktoken:max(o200k_base,cl100k_base)+margin0.1`. Verdict: **FAIL**.

| metric | value | threshold |
|---|---|---|
| authorisation leakage | 0 | 0 (strict) |
| budget adherence | 1.0 | 100% (strict) |
| attribution | 1.0 | 100% (strict) |
| evidence recall (cases 1-8) | 1.0 | ≥ 0.9 (proposed) |
| irrelevant inclusion | 0.4991 | ≤ 0.1 (proposed) |
| forbidden items packed | 5 | 0 |
| temporal correctness | 0.875 | 100% (proposed) |
| summarised fallback | envelope_broken | recorded |
| tokens per call | 317.8 | recorded (DEC-11) |
| latency p50 / p95 (s) | 0.257 / 0.376 | recorded (DEC-11) |

## Cases

| case | status | recall | irrelevant | temporal | attribution | leaks | budget | tokens |
|---|---|---|---|---|---|---|---|---|
| C01_current_employer | complete | 1.0 | 0.75 | ok | 1.0 | 0 | ok | 285/300 |
| C02_historical_employer | complete | 1.0 | 0.0 | ok | 1.0 | 0 | ok | 98/300 |
| C03_aspiration_and_conflicting_evidence | complete | 1.0 | 0.2222 | ok | 1.0 | 0 | ok | 533/600 |
| C04_shared_agent_finding | complete | 1.0 | 0.2222 | ok | 1.0 | 0 | ok | 533/600 |
| C05_unrelated_preference_distractor | complete | 1.0 | 0.8 | WRONG | 1.0 | 0 | ok | 261/300 |
| C06_private_memory_not_allowed | complete | 1.0 | 1.0 | ok | 1.0 | 0 | ok | 533/600 |
| C07_insufficient_evidence | complete (WRONG) | 1.0 | 1.0 | WRONG | 1.0 | 0 | ok | 98/600 |
| C08_backdated_update | complete (WRONG) | 1.0 | 1.0 | ok | 1.0 | 0 | ok | 261/300 |
| C09_budget_250_vs_1200 | complete | 1.0 | 0.2222 | ok | 1.0 | 0 | ok | 533/1200 |
| C10_graph_backend_missing | partial | 1.0 | 0.2222 | ok | 1.0 | 0 | ok | 533/600 |
| C11_vector_backend_missing | partial | 1.0 | 0.0 | ok | 1.0 | 0 | ok | 444/600 |
| C12_foreign_workspace | denied | 1.0 | 0.0 | ok | 1.0 | 0 | ok | 0/None |
| C13_injection_in_a_memory | complete | 1.0 | 1.0 | ok | 1.0 | 0 | ok | 583/600 |
| C14_summarized_faithfulness | complete | 1.0 | 1.0 | ok | 1.0 | 0 | ok | 383/1200 |
| C15_hidden_person_is_denied | denied | 1.0 | 0.0 | ok | 1.0 | 0 | ok | 0/None |
| C16_hidden_relationship_is_absent | complete | 1.0 | 0.0 | ok | 1.0 | 0 | ok | 75/300 |

## Misses

- **C01_current_employer**: missing ids [], irrelevant ['Sarah Johnson from the cycling club rode 40 km with Tom on Saturday.', 'Sarah discussed a possible software business with Michael; she had not decided to leave Ac', 'Sarah Johnson reports that Michael, whom she has known for years, keeps encouraging her to'], missing text [], forbidden text [], forbidden ids packed [], LEAKED []
- **C03_aspiration_and_conflicting_evidence**: missing ids [], irrelevant ['Sarah Johnson from the cycling club rode 40 km with Tom on Saturday.', 'Sarah Johnson reports that Michael, whom she has known for years, keeps encouraging her to'], missing text [], forbidden text [], forbidden ids packed ['01a15f51-bdde-70b5-9a26-d5d7ce716cf7'], LEAKED []
- **C04_shared_agent_finding**: missing ids [], irrelevant ['Sarah Johnson from the cycling club rode 40 km with Tom on Saturday.', 'Sarah Johnson reports that Michael, whom she has known for years, keeps encouraging her to'], missing text [], forbidden text [], forbidden ids packed [], LEAKED []
- **C05_unrelated_preference_distractor**: missing ids [], irrelevant ['Sarah prefers decision comparisons that show risks and trade-offs.', 'Sarah discussed a possible software business with Michael; she had not decided to leave Ac', 'Michael has been encouraging Sarah Johnson in her consideration of entrepreneurship, servi', 'Sarah Johnson reports that Michael, whom she has known for years, keeps encouraging her to'], missing text [], forbidden text ['software business'], forbidden ids packed ['01a15f50-cce1-7037-b65b-0a1f19e37215', '01a15f51-bda3-70ae-918c-ab1b9600780f'], LEAKED []
- **C07_insufficient_evidence**: missing ids [], irrelevant (unscored), missing text [], forbidden text ['Acme'], forbidden ids packed ['01a15f50-1219-711a-8604-dd07d3fe6129'], LEAKED []
- **C08_backdated_update**: missing ids [], irrelevant ['Sarah Johnson from the cycling club rode 40 km with Tom on Saturday.', 'Sarah prefers decision comparisons that show risks and trade-offs.', 'Sarah discussed a possible software business with Michael; she had not decided to leave Ac', 'Michael has been encouraging Sarah Johnson in her consideration of entrepreneurship, servi', 'Sarah Johnson reports that Michael, whom she has known for years, keeps encouraging her to'], missing text [], forbidden text [], forbidden ids packed [], LEAKED []
- **C09_budget_250_vs_1200**: missing ids [], irrelevant (unscored), missing text [], forbidden text [], forbidden ids packed ['01a15f51-bdde-70b5-9a26-d5d7ce716cf7'], LEAKED []

## Problems

- a case returned a status its expectation does not allow (denied / insufficient_evidence must behave as specified)
- 5 item(s) a case forbids were packed (a relevance error)
- irrelevant inclusion = 0.4991 (needs ≤ 0.1)
- temporal correctness = 0.875 (needs 100%)
