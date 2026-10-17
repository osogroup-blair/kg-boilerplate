# Runbook: the memory stage, quarantine and review

`memory_propose` runs after `graph_ingest` and is the only memory stage that calls a generative model. Its state is on the interaction (`GET /v1/interactions/{id}`, Admin console → Interactions → stage timeline).

| `last_error_code` / status | Meaning | What the platform does | What you do |
|---|---|---|---|
| `skipped_by_policy` · `consent_denied`, `egress_denied`, `classification_excluded`, `extraction_disabled` | the data may not leave, or the ontology switched extraction off | no model call, event stays `indexed` | nothing; change consent/policy only through the owner of the data |
| `memory_extraction_unparseable` | the model answered something the schema rejects | one retry, then **dead letter**, event `partial`; no memory is written | fix the cause, then **Replay** the stage (`kg interactions replay <id> --stage memory_propose --reason model_fixed …`) |
| `extraction_timeout` | the call exceeded `MEMORY_PROPOSE_TIMEOUT_SECONDS` | retried with backoff | check router latency |
| `router_*`, `budget_exceeded` | router refused or paused | see [stuck-job-replay](stuck-job-replay.md) | |
| stage `pending` with a dead `graph_ingest` | memory waits for the graph stage | the event is `partial` | replay `graph_ingest` first; `memory_propose` then runs by itself |

**Events accepted before P5** have `memory_propose` as `skipped_disabled`. To give them memory: `uv run kg interactions replay <id> --upgrade-pipeline --reason enable_memory --actor <you> --workspace <ws>` (audited as `interaction.pipeline_upgraded`; a replay re-asks the model and the keyed content hash prevents duplicate memories).

## Reviewing what the stage proposed
Procedural memory, conflicts with a higher-authority memory and every agent proposal land in **Review Queue** (`memory_proposal`, `memory_conflict`). The page shows the memory, its scopes **as you may see them**, its evidence ids (read the source through the audited *View evidence* action) and the competing memory. Decisions: `approve`, `reject`, `approve_with_edit` (a new active memory with your wording; the proposal is kept, superseded, with lineage); for conflicts `keep_existing`, `supersede_existing` (needs your authority ≥ the existing memory's), `mark_disputed`.

## Drop reasons
The drops are reported per event in the stage result (`drops`, and the `memory_extraction_candidates_total` metric): `below_importance`, `unmapped_subject`, `escalated_claim`, `foreign_fact`, `no_subject`, `too_long`, `empty`, `working_type`, `duplicate_candidate`, `over_limit`. A rising `unmapped_subject` or `escalated_claim` rate means the model is inventing names or decisions: check the prompt version (`mem.v1`) and the model alias before loosening anything. Memory content never appears in logs; use `correlation_id` (`memory-propose:{interaction}:g{generation}` as a UUID5) to find the model calls.
