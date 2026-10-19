# Add a domain (no platform code)

The CRM example (`examples/domains/crm/`) is the worked case. A new domain is **configuration and data only**; `tests/examples/test_crm_is_config_only.py` fails if platform source ever mentions the CRM names.

## The files, in the order you write them
1. **`ontology.yaml`**: entity types (identity, `access: subject_scoped` for people whose facts need a grant), relationship types with a `fact_template` (command facts render through it), rules from the fixed catalog (`require_source`, `preserve_valid_time`, `succession`, `max_active_per_source`, `require_review_if_inferred`, ...), memory policy, and `context_profiles`. Validate before publishing: `make` has no target for it; use `kg ontology validate` or `POST /v1/ontologies/versions/{id}/validate`. A profile name the platform does not know starts from `quick_answer` and overrides field by field; `expected_signals` make the engine report **unknowns** deterministically (CRM: no `renewal_date` attribute on a customer means "No verified renewal date for X").
2. **`events/timeline.json`**: what the system of record writes by command (entities, relationships, a backdated end-of-employment) and the conversations that go through extraction.
3. **`mock_rules.py`** (offline only): the model answers for each conversation. Every structured request the pipeline can make must be covered, or the strict mock fails loudly: `ExtractedEntities`, `ExtractedEdges`, `MemoryCandidates`, and, once the workspace already has nodes, `NodeResolutions` and `SummarizedEntities`; an entity type with attributes beyond the name also makes one request named after the type. Graphiti numbers entity types alphabetically from 1.
4. **`benchmark/cases.yaml`**: frozen cases: expected text, forbidden text, expected unknowns, text that must be **nowhere** in the response (authorisation), and a token budget. Same thresholds as Sarah: leakage 0, budget 100%, attribution 100%.
5. **`run.py`**: fresh workspace → publish → agents with different grants → ingest → build → score.

## What you never touch
`apps/`, `packages/`, `sdk/`, the migrations, the OpenAPI contract, the admin console. If a valid ontology is refused or a profile cannot express what you need, that is a platform bug or a missing feature: fix it generically, in its own commit with its own test.

## Gotchas found building the CRM
- Contacts are `subject_scoped`, so an agent needs a grant on each contact; a fact is visible only when **both** endpoints are. That is what keeps one customer's people out of another's context.
- Evidence for a proposed memory must be an interaction **the proposing agent can read**; have the agent submit its own note rather than citing one a different principal ingested.
- `as_of` must carry a timezone. Ending an employment edits `valid_to` in place, so `known_at` does not "un-know" it (see the Sarah README).
- One memory scope per team (`acct-globex`, `acct-initech`) is how account plans stay private from support.
