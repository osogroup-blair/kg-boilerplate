# Consolidation evaluation (mock, 2026-10-19)

## Reinforce
merged_distinct=0 crossed_scope=0 missed_duplicates=0

- paraphrases fold: ok got=[['a', 'b', 'c']] want=[['a', 'b', 'c']]
- distinct claims stay: ok got=[] want=[]
- same words different vectors: ok got=[] want=[]
- different scope sets never mix: ok got=[] want=[]
- different classification never mixes: ok got=[] want=[]
- conflicting assertions never fold: ok got=[] want=[]
- two independent clusters: ok got=[['a', 'b'], ['c', 'd']] want=[['a', 'b'], ['c', 'd']]

## Synthesis
{"bad_accepted": 0, "good_refused": 0, "ok": true, "wrong_reason": 0}

- cycling: {"good_ok": true, "good_reason": "", "bad": [{"label": "invented number", "expected": "invented_detail", "got": "invented_detail", "ok": true}, {"label": "invented name", "expected": "invented_detail", "got": "invented_detail", "ok": true}, {"label": "missing citation", "expected": "missing_citation", "got": "missing_citation", "ok": true}, {"label": "foreign citation", "expected": "foreign_citation", "got": "foreign_citation", "ok": true}, {"label": "one source only", "expected": "bad_sources", "got": "bad_sources", "ok": true}, {"label": "merges a ride with a club", "expected": "merged_distinct_claims", "got": "merged_distinct_claims", "ok": true}, {"label": "echoes an instruction", "expected": "echoed_instruction", "got": "echoed_instruction", "ok": true}]}
- considerations: {"good_ok": true, "good_reason": "", "bad": [{"label": "a consideration becomes a decision", "expected": "escalated_claim", "got": "escalated_claim", "ok": true}, {"label": "a hedge becomes a plan", "expected": "escalated_claim", "got": "escalated_claim", "ok": true}]}

verdict: PASS
