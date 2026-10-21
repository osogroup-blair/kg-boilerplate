# Consolidation evaluation (live, 2026-10-21)

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
{"answers": 2, "drops": {"empty": 1}, "escalations_escaped": 0, "merged_distinct_escaped": 0, "ok": true, "valid": 1, "validity_rate": 0.5}

- cycling: {"accepted": false, "reason": "empty", "groups": 1}
- considerations: {"accepted": true, "reason": "", "groups": 0}

verdict: PASS
