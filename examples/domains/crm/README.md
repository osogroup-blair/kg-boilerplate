# Second domain: Project / Customer CRM

```bash
make up seed
make demo-crm     # the world, two printed renewal-prep packages, then the benchmark
make eval-crm     # the benchmark alone
```

Proves principle P-01: the same platform, a different domain, **configuration only**. Walk-through: [docs/guides/add-a-domain.md](../../../docs/guides/add-a-domain.md).

| File | What it is |
|---|---|
| `ontology.yaml` | Contact, Customer, Deal, Product, Project, Ticket; eight relationships with rules; three context profiles (`account_review`, `renewal_prep`, `support_triage`); the renewal profile's `expected_signals` |
| `events/timeline.json` | system-of-record commands, a backdated job change (Marcus Lee leaves Globex for Initech), three conversations, one account plan |
| `mock_rules.py` | the offline model answers, generated from the timeline |
| `benchmark/cases.yaml` | 9 frozen cases: recall, unknowns, temporal correctness (`as_of`), cross-customer leakage, support vs account scope, budget |

Agents: `globex-am` (Globex contacts and the `acct-globex` project), `initech-am` (the Initech equivalents), `support-agent` (every contact, no account project). The account plan is published to `acct-globex`, so support never sees it; Initech's manager asking about Globex gets no Globex contact or memory.

The benchmark has its own small scorer in `run.py` because `eval/context` builds Sarah's world in Python; the thresholds are the same (leakage 0, budget and attribution 100%). A reviewer can check that the cases can fail: add `forbid: ["Dana Reyes"]` to R01 and it does.
