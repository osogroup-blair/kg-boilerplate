# Runbook: debugging a context

The question is always "why did the agent get (or not get) X?". Start with the trace, never with the database.

## 1. Find the build
`kg context trace <context_id> --workspace demo` (or the admin console: Context Lab → a build, or `/context/<id>`). The agent's response carries `context_id` and `retrieval_trace_id`. To list builds: `GET /v1/context` (admins see every build's metadata).

To reproduce a build exactly as the agent would have: Context Lab → **Build as** the agent, same task/subject/profile/budget/`as_of`; or `kg context build "<task>" --as agent_planning --subject <entity id> --profile planning --budget 600`. The Lab uses the same endpoint and the agent's own authorisation, so a difference between the Lab and the agent is a bug.

## 2. Read the trace top-down
| Look at | It tells you |
|---|---|
| `status` and `unavailable` | `partial` names the source and the reason (`timeout`, `unavailable`, `embedding_unavailable`, `vector_unavailable`, `egress_policy`, `stale`). Graph down is *not* wrong current facts: those come from Postgres. |
| `plan` | the profile, whether history was on and why (`as_of`, `task`, `profile`), the sections and memory types asked for |
| `counts.candidates_by_source` | how many AUTHORISED candidates each source produced. `memory: 0` for an agent that should see memory means a **grant** problem (`kg scopes list`), not a ranking problem. |
| `items` (included) | position = rank order; `tier`, `score`, and the `features` that produced it |
| `items` (excluded) | the reason: `below_threshold` (no relevance), `unsupported` (the profile requires evidence), `stale_default_state` (not true at `as_of`), `duplicate_of_fact` / `near_duplicate` (`duplicate_of` names the winner), `budget` / `max_items` (it did not fit), `not_included` (the profile does not include that section) |
| `used_tokens` / `required_tokens` / `max_tokens` | `insufficient_budget` means the required tier alone needed more than `max_tokens`: ask for `required_tokens` |
| `latencies.per_source` | which source is slow; a source over `CONTEXT_SOURCE_TIMEOUT_MS` becomes `unavailable: timeout` |
| `watermarks` | `graph_ingest` / `memory_propose` backlog age and dead jobs: a fact the agent "should" have is probably still in the pipeline (see [stuck-job-replay](stuck-job-replay.md)) |

An item that is **not in the trace at all** was never an authorised candidate: the memory's scopes, its classification, its status (only `active`; `as_of` admits history), or its type is not in the profile. Check `kg memories show <id> --actor <agent>`: a 404 there is the answer.

## 3. Symptom → cause
- *Right fact, wrong answer from the agent*: read `evidence_status` and the heading it sits under; unverified/reported items are labelled, and the data envelope says to verify. Check the agent places `context_text` as quoted data (see [agent-integration](../agent-integration.md)).
- *A memory is missing*: excluded `below_threshold` → its words/vector do not match the task (check `MEMORY_VEC_MIN_COSINE`, `kg memories search`), or it is `standing`-eligible only if it is the subject's approved procedural memory or the named `project`'s.
- *A caveat is missing*: unknowns/conflicts are required-tier; if absent in `relevant` mode they were judged not material to the task (`below_threshold` in the trace); use a `planning`/`review` profile (`current_facts: always`) or add the signal to the profile.
- *`partial: stale`*: `freshness_requirement.max_lag_seconds` is smaller than the pipeline lag or a job is dead. Replay it, or relax the requirement.
- *Over budget*: `context_budget_violations_total` must be 0. If it is not, stop and file it: the counter was changed (`CONTEXT_TOKEN_MARGIN`) or the packer regressed; `tests/contract/test_context_budget.py` reproduces it.
- *Debug snapshot*: `build` with `debug_snapshot=true` as an admin, then `GET /v1/context/{id}/debug` for the task and rendered text (encrypted at rest, gone after `CONTEXT_DEBUG_TTL_HOURS`; audited).

## 4. Retention and switches
`kg context purge-traces [--older-than-days N]` runs the retention functions now. `CONTEXT_API_ENABLED=false` makes the build answer 503 `feature_disabled` (rollback without a deploy). `kg db downgrade 0005` drops the trace tables.
