# Phase 10: Close-out and Fully Working Examples — Detailed Implementation Plan

**Roadmap:** [ROADMAP.md](../../ROADMAP.md), Phase 10 ("Fully Working Examples"), plus the Phase 9 items still open in [docs/release/handoff.md](../release/handoff.md).

**Blueprint inputs:**
- `05-delivery/02`: the 10-step end-to-end reference scenario, the AT-01..AT-18 matrix, the retrieval evaluation method.
- `06-reference/01`: the Sarah walkthrough (`evt_001`..`evt_006`, the 2027 change, the mistaken "Michael manages Sarah", the `other_demo` negative test, the acceptance summary).
- `06-reference/02`: example config and payloads (ontology section A, event envelope, context request/response).
- `00-overview`: principle P-01 (reusable across domains with configuration only).

**Binding inputs from P0–P9:**
- Every ADR (011–053) and every "Deviations" section in `docs/architecture/p1..p9-*.md`.
- `docs/agent-integration.md` (how an agent asks for context, places it in a prompt, writes back).
- The existing demo drivers `scripts/demo_p1.py` .. `demo_p8.py` (the `Demo` class each one carries), the Sarah world builder `tests/support/p6.py::sarah_world`, and the mock-router scenarios in `tools/mock_router/fixtures/scenarios/` (`sarah_evt003`, `sarah_newco`, `michael_manager`, `memory_evt003`, `memory_evt004`, `memory_research`, ...).
- `sdk/python` (`KGClient`, `KGClientSync`) and `apps/mcp` (stateless Streamable HTTP, forwards the caller's key).

**State at the time of writing (2026-10-18):**
- P0–P9 are committed. Phase 9 is verified green except for three things: the load baseline, the release-candidate report, and the owner actions.
- **A load baseline is running right now in another session** (`make perf-down perf-up perf-seed perf-baseline`, compose project `kg-perf`). It produced an uncommitted fix in `packages/memory/src/kg_memory/search.py`: a `FULL JOIN` CTE (`hits`) replaces two `LEFT JOIN`s that the planner turned into a nested loop with 2.5 billion comparisons on a 50 000-memory workspace. There is also an untracked report, `eval/reports/memory-mock-2026-10-18.{json,md}`.
- `origin` (GitHub) exists and `main` is in sync with it. The header of `.github/workflows/ci.yml` still says "Inert until the repository has a remote".

**Update 2026-10-18 (later):** another session finished the baseline and committed WP-A0/A1 as `88ddaa9` (baseline report, search fix, sizing, DEC-11 proposals, ROADMAP P9 status, handoff). What remains of Part A: the plan-shape test (added to `tests/integration/test_db_p5.py`), the loose ends in WP-A2, and the commit. The baseline also found that search, not the worker, is the capacity limit (ANN trigger already true), so examples should keep datasets small.

**Status:** ready for a coding agent. Part A finishes Phase 9; Part B is Phase 10. Owner actions are marked **(owner)**: an agent prepares them but never fakes them.

---

## 1. Objective

1. **Button up Phase 9.** Land the baseline and the search fix, size the VM, write the SLO proposals and the ROADMAP status, fix the small loose ends, and leave the tree ready for one final release-candidate run.
2. **Show the knowledge graph working**, end to end, in ways a new reader can run and believe:
   - **Sarah scenario** (`make demo-sarah`): all 10 reference steps, the 2027 change, the correction, the deletion and a model-outage variant, with a pass/fail report and the printed context packages.
   - **Second domain** (`make demo-crm`): a Project / Customer CRM ontology with its own events, profiles and benchmark, and **no platform code changes**.
   - **Real agent integrations** (`make examples-agents`): a Research agent and a Planning agent through the SDK, the same flow through MCP, and an unauthorized-agent example that shows every denial.
   - **Seven Jupyter notebooks** that run top to bottom against the local stack.
   - **Docs:** quickstart, architecture overview, "extend this boilerplate" guide.
3. **Gate:** from a fresh clone, `make tools sync admin-install up seed examples test-examples` passes. CI runs it on every push. The final `make release-candidate` includes the examples step and writes `docs/release/rc-report-<date>.md`.

---

## 2. Lessons that bind this phase (X1–X14)

| # | Lesson (source) | How P10 applies it |
|---|---|---|
| X1 | Demos share one workspace, so a mock rule matched by `contains: [marker]` leaks into every later prompt with that marker (P8) | Every example creates **its own workspace** and uses markers unique to that example; it removes its fixtures in a `finally` |
| X2 | Strict mock mode makes a missing fixture fail loudly (P4) | Examples run the mock in strict mode for their duration and restore the previous mode afterwards |
| X3 | Real extraction in a workspace that already has nodes also needs `NodeResolutions` and `SummarizedEntities` rules (P8) | The fixture packs in WP-B1/B2 include them; the kit has a helper that checks a pack covers every schema the pipeline will ask for |
| X4 | Command facts carry no `fact_text`; they render through the ontology's template (P6) | The CRM ontology defines a template for every edge type; WP-B2's test fails if one is missing |
| X5 | A hidden id returns the same 404 as a missing one; a fact is visible only when both endpoints are (P7) | The unauthorized-agent example compares the response bodies **byte for byte** (apart from `request_id`) |
| X6 | A rejected or queued command is recorded under its `Idempotency-Key`; a corrected retry needs a new key (P2) | The kit generates one key per logical write; examples never reuse a key for a different body |
| X7 | `make mcp-up` recreates api and mock-router (P7) | The MCP examples need it, so `make examples` runs `mcp-up` **once, before** anything else, never in the middle of a run |
| X8 | The egress setting is cached for 3 s in dev (P8) | Examples that change settings wait out `WORKSPACE_EGRESS_TTL_SECONDS` explicitly, so they do not depend on timing luck |
| X9 | A context is never stored, only explained; re-materialisation marks changed items `withdrawn` (P6) | Notebook 04 and the deletion step show `withdrawn` items on purpose |
| X10 | Never `make down`/`reset` mid-run; `release-candidate` drops the dev volumes (P9) | `test-examples` never resets; the RC run is last and is announced before it starts |
| X11 | Tests that patch internals are skipped under `KG_REPLICAS=2` (P9) | Examples are strictly black-box (HTTP, SDK, MCP, CLI), so they also run against two replicas |
| X12 | The act-as header works only on `POST /v1/context/build` (P6) | The notebooks use the Context Lab's act-as only for context builds; everything else uses the agent's own key |
| X13 | Model-affecting changes need a recorded live gate (CLAUDE.md §5) | Examples add no prompts, aliases or ranker changes. Mock fixtures are dev tooling. A `--live` flag exists, but running it live is an owner action |
| X14 | A full disk wedges Docker (P9) | The plan never runs the perf stack, the dev stack and the RC at the same time; WP-A1 runs `docker system df` before the RC |

---

## 3. Scope

**In scope:** everything in §1. Small platform fixes that the examples uncover go in their own commit with their own regression test. The CRM no-code-change rule (WP-B2) holds even so: a fix may make the platform accept a valid ontology it wrongly refused, but it may never add CRM-specific code.

**Out of scope:** new platform features; DEC-06 (graph tenancy) stays open; any live-router run (owner); production deployment beyond the existing reference.

---

## 4. Layout

```
examples/
  README.md                     index: what each example shows, how to run it, expected output
  _kit/                         shared helpers (WP-B0); depends on kg_platform_sdk + httpx only
    __init__.py  env.py  workspace.py  admin.py  mock.py  waits.py  show.py  report.py
  ontologies/                   (exists) personal-knowledge*.yaml
  sarah/                        WP-B1
    run.py  steps.py  fixtures/  expected/  README.md
  domains/crm/                  WP-B2
    ontology.yaml  profiles.yaml?  events/  mock/  benchmark/  run.py  README.md
  agents/                       WP-B3
    common.py  research_agent.py  planning_agent.py  unauthorized_agent.py
    mcp/  mcp_flow.py  claude-code.mcp.json  claude-desktop.json  README.md
  notebooks/                    WP-B4
    01_ingest_and_ledger.ipynb … 07_second_domain.ipynb  README.md
tests/examples/                 WP-B6: pytest wrappers that run every example and notebook
docs/architecture/overview.md   WP-B5
docs/guides/add-a-domain.md, docs/guides/extend.md   WP-B5
```

Whether CRM profiles live inside `ontology.yaml` or in a separate file follows how profiles are loaded today (the typed `ContextProfile` arrived with ontology v1.3.0). WP-B2 checks this first and uses whatever the personal-knowledge ontology does.

---

## 5. Work Packages

### Part A: Close out Phase 9

#### WP-A0: Land the in-flight baseline work (depends on the running session)
- **Wait for the other session's `perf-baseline` to finish.** Do not start heavy work while it measures latency. Read its log and the report it writes (`eval/reports/baseline-<date>.{json,md,svg}`).
- **Search fix (`kg_memory/search.py`):**
  - add a regression test that pins the plan shape: an `EXPLAIN` on a seeded workspace where the query term matches most rows must show no nested loop between `lex`/`vec` and `candidates`. Put it in the integration suite next to the existing memory-search perf test;
  - rerun `make test` and the memory integration tests, `make eval-memory` (results must not move: rank order and ties are unchanged by construction) and the memory-search perf script, then update `eval/reports/memory-search-perf.json`;
  - this changes retrieval SQL, not ranking weights, so no live gate is needed (X13). Record that reasoning in the commit message.
- **Untracked report:** commit `eval/reports/memory-mock-2026-10-18.*` with the search fix (it is the evidence that the fix left the scores alone).
- **Done when:** the fix has a test, the reports are refreshed, and the baseline report exists. If the baseline fails, triage it with the handoff's notes (a timed-out search in the size curve means the outbox was still draining) and rerun only `perf-baseline`.

#### WP-A1: Sizing, SLO proposals, ROADMAP status
- `docs/runbooks/deploy-production.md`: replace the `@@SIZING@@` line with a table derived from the baseline for sizes S/M/L: vCPU, RAM, disk, Neo4j heap and page cache, Postgres `shared_buffers`/`work_mem`, worker concurrency. Include the arithmetic (measured peak × headroom) so the owner can redo it.
- `docs/release/decisions.md` DEC-11: paste the measured p50/p95 per endpoint and the freshness lag, plus **proposed** SLOs marked "Proposal; owner sets". Point the alert thresholds in `deploy/observability` at the proposals, still marked as proposals.
- `ROADMAP.md` Phase 9: add the status paragraph in the Phase 8 shape (Verified / Built / Details / Surprises). The facts come from `docs/architecture/p9-release.md` and the green runs listed in the handoff.
- **Done when:** no `@@SIZING@@` placeholder is left (`grep -rn '@@' docs` is empty) and the ROADMAP reads like the other phases.

#### WP-A2: Loose ends
| Item | Fix |
|---|---|
| `README.md` says `make help`, but the Makefile has no `help` target | Add `help:` that prints every `target: ## description` line (the targets already carry `##` comments), and add it to `.PHONY` |
| `.github/workflows/ci.yml` header says "Inert until the repository has a remote" | A remote now exists. Check the latest Actions run with `gh run list`. If CI is red or never ran, fix it or record why (for example missing secrets). Update the header comment |
| ROADMAP "How We'll Use This Roadmap" still names `/Users/blairboylan/...` | Replace it with the repository-relative path |
| ROADMAP P5–P8 statuses say "Nothing … is committed yet" and "waiting for `git init`" | Add a one-line "(since committed: `f638e32` and earlier)" note rather than rewriting history |
| `docs/release/handoff.md` | Rewrite at the end of Part A, and again at the end of Part B |
| Disk hygiene | Before the RC: `docker system df`, prune dangling images and build cache only (never volumes the dev stack uses), confirm 20+ GB free (X14) |

#### WP-A3: Commit "Phase 9 (6/n)" (owner approves the commit)
One commit: the search fix and its test, the reports, sizing, DEC-11 proposals, the ROADMAP status, the loose ends and the updated handoff. **The release-candidate run moves to WP-B7**, so that it covers the examples as well and runs once (see §7, decision D2).

### Part B: Phase 10 examples

#### WP-B0: Example kit (`examples/_kit/`)
The pieces every example needs, taken from the `Demo` classes in `scripts/demo_p*.py`. Leave the demos alone (they are release evidence); the kit is a clean copy for outside readers.
- `env.py`: reads `KG_BASE_URL` (default `http://localhost:8000`), the MCP URL, the mock-router URL, and `.local/dev-credentials.json`. It refuses to run against a non-local host unless `--live` is passed, and in `--live` mode it refuses the mock-only helpers.
- `workspace.py`: `fresh_workspace(example: str) -> Workspace` runs `kg workspaces create` (the CLI added in P9 for first boot) with the slug `ex-<example>-<UTC yyyymmddHHMMSS>` and returns the first admin's key. **Decision D5:** one workspace per run; a reset of the dev stack cleans them up; `make examples-list` shows them. Before building, check whether `kg workspaces` has a delete or offboard command. If it does, add `make examples-clean` that uses the governed path (P8 workspace deletion with cooling-off); never delete rows directly.
- `admin.py`: thin `httpx` calls for admin-only actions the SDK deliberately does not have: publish an ontology from YAML (draft → validate → publish), register an agent or app with scopes, allowed profiles and quotas, grant subject access, revoke an agent, open and resolve review items, create a deletion request.
- `mock.py`: a context manager that turns strict mode on, loads a fixture pack via `/__mock/fixtures`, and on exit deletes the fixtures and restores the mode (X1, X2). It also has `coverage_check(pack, schemas)` (X3).
- `waits.py`: wait until an interaction reaches a stage, the outbox drains, or a deletion receipt is issued. Every wait has a timeout and reports the last state it saw.
- `show.py`: prints a context package as a readable block (current facts, memories with provenance, unknowns, conflicts, citations, token count against the budget), and a trace summary. Rich text in a terminal; plain markdown in notebooks.
- `report.py`: collects `Step(id, title, at_ids, passed, evidence)` and writes `report.json` plus `report.md`.
- **Import contract (import-linter):** `examples` may import only `kg_platform_sdk`, `httpx`, the stdlib and its own `_kit`. This proves the examples use the platform from the outside. The kit calls the CLI through `subprocess` and never imports `kg_cli`.
- **Tests:** unit tests for `report.py` and `show.py` (golden output), and a test for the import contract.

#### WP-B1: Sarah two-agent scenario (`make demo-sarah`)
One run in a fresh workspace, in blueprint order. Every step records its AT ids and its evidence: response ids, trace ids, counts, and **no raw content in the report** apart from the context packages the step prints on purpose.

| Step | What happens | Assertions | AT / ids |
|---|---|---|---|
| 1 | The admin publishes Person, Organization, Topic, WORKS_AT, KNOWS, CONSIDERING and policies (`examples/ontologies/personal-knowledge-governance.yaml`, the latest of the existing files) | The published version has a content hash; validation is clean | ONT-01/02 |
| 2 | A trusted app ingests `evt_001`/`evt_002` as commands, and the Sarah–Michael friendship history | Projection complete; **no model call** recorded for these writes; resubmitting with the same key gives one logical event | AT-01, ONT-06 |
| 3 | `evt_003` conversation: Sarah tells Michael she is considering a software company | Interaction reaches `graph_ingest` and `memory_propose` done | ING-01 |
| 4 | The episode yields a CONSIDERING fact (statement, not decision) and a curated memory; current employment is still Acme | Graph query `as_of=2026-10-11` shows Acme / Engineering Manager current; memory `mem_003`-equivalent is active, with evidence that links back to `evt_003` | AT-02..05 |
| 4b | `evt_004` preference (risks and trade-offs) | Published preference memory | MEM |
| 5 | The Research agent submits the `evt_005` finding with evidence; policy or review publishes it | Label `agent_generated`; it becomes visible to the Planning agent only after publish | AT-10, AGT |
| 6 | The Planning agent asks for `planning` context on `person_sarah` with `max_tokens=600` | Contains Acme / EM as current facts, the memory, the preference, the finding, the unknown "runway", and citations; excludes the cycling distractor; rendered size ≤ 600 tokens. **Prints the package** | AT-06..09, AT-12 |
| 7 | `evt_006` (2027): Acme records that Sarah left, effective 2027-02-01 | `as_of=2026-12-01` → Acme current; `as_of=2027-03-01` → Acme historical; `known_at` before the event recorded vs after gives different answers; the entrepreneurship memory is unchanged. **Prints both packages** | AT-02/03, DEC-12 |
| 8 | An `other_demo` agent, and an agent of this workspace without a subject grant, ask for Sarah | `other_demo` gets 404, identical to a missing id, with no names, counts or ids in the body or the trace; the ungranted agent gets no restricted memory | AT-11, SEC-01, AT-14 |
| 9 | The model wrongly extracts "Michael manages Sarah" (`michael_manager` scenario); a reviewer rejects it | The next build does not present it as verified; the original episode is retained | AT-13, MEM-06 |
| 10 | A deletion request for a subject | Reads blocked immediately (the next build marks items `withdrawn`); the purge completes and a receipt is issued; verification is clean | AT-16, AT-17 |
| V | **Model-outage variant:** inject a router fault (`/__mock/faults`) during a new conversation, then build context | Ingestion is queued and backs off (not lost); context returns `partial` with source watermarks when `allow_partial`, and `ContextIncomplete` otherwise; after the fault clears, the event completes | FI matrix, AT-08 |

- **R2 settled (2026-10-18):** `known_at` filters on the platform's own `recorded_at`, which the database sets (`kg_knowledge/reads.py`); the source's claim "learned on 2027-02-05" is stored as `source_recorded_at` (informational, KGR-04). So step 7 sends `source_recorded_at=2027-02-05` and demonstrates `known_at` with real timestamps taken just before and just after the ingest. The README states this. (Original question, kept for context:) step 7 needs a record time of 2027-02-05 for a fixture "from the future". ADR-017/023 say Graphiti stamps wall-clock `created_at` and the platform keeps its own record time. Check how `known_at` is evaluated: against the ledger's `recorded_at` or a source-declared time. If the platform cannot express "learned on 2027-02-05" today, the step demonstrates `known_at` with the real record times (before/after the step-7 ingest) and the README says why. Do not add a backdoor for fake record times.
- **Fixtures:** reuse the scenarios in `tools/mock_router/fixtures/scenarios/` through the kit's fixture pack, adding markers unique to this example (X1). `--live` skips the mock and runs against the real router (owner action, X13).
- **Outputs:** `examples/sarah/reports/sarah-<date>.{json,md}`; the latest is committed as a sample. Exit code 0 only if every step passes.
- **Re-runnable:** a second run in a new workspace gives the same step results and the same package shapes (ids differ).

#### WP-B2: Second domain: Project / Customer CRM (`make demo-crm`, `make eval-crm`)
**Rule:** this work package changes **only** `examples/`, `eval/datasets/crm/` (or wherever `eval/context` reads cases from) and docs. `tests/examples/test_crm_is_config_only.py` checks that the WP-B2 diff touches nothing under `apps/` or `packages/`. If the domain exposes a platform bug, fix it in a separate commit with its own test, and record it as a finding in `docs/guides/add-a-domain.md`.

- **Ontology (`ontology.yaml`, same schema as `06-reference/02` section A):**
  - entities: `Customer` (organization-like), `Contact` (person-like, `access: subject_scoped`), `Project`, `Deal`, `Ticket`, `Product`, `Topic`;
  - edges: `WORKS_FOR` (Contact→Customer, temporal), `ACCOUNT_OWNER` (Contact→Customer, cardinality one current), `SPONSORS` (Contact→Project), `FOR_CUSTOMER` (Project/Deal→Customer), `USES_PRODUCT` (Customer→Product, temporal), `BLOCKED_BY` (Project→Ticket), `ESCALATED_TO`;
  - rules from the declarative catalog only (cardinality, temporal, authority, validation);
  - a render template for every edge (X4); avoid Graphiti's reserved `name` field (ADR-014).
- **Profiles:** `account_review`, `renewal_prep` (with `expected_signals`: renewal date, budget owner, open escalations, so missing ones show as unknowns), `support_triage`. Each narrows a base profile; agent overrides can only narrow further.
- **Events (`events/`):** CRM-system commands (customer created, contact joined/left, deal stage changes, products adopted), meeting notes and support-ticket conversations as interactions, and one backdated authoritative correction.
- **Mock fixtures (`mock/`):** extraction, `NodeResolutions`, `SummarizedEntities` and memory responses for each conversation, loaded at run time (X1–X3). Nothing goes into `tools/mock_router/fixtures`.
- **Benchmark (`benchmark/`):** about 10 frozen cases in the `eval/context` format: recall, precision, temporal correctness (a contact who changed employer), attribution, leakage (a second customer's contact must never appear), budget. `make eval-crm` runs the existing harness on these cases; thresholds are the same as Sarah's (leakage 0, budget 100%, attribution 100%).
- **Run (`run.py`):** fresh workspace → publish → register a "Account Manager" agent and a "Support" agent with different grants → ingest → build `renewal_prep` for one customer (print it) → show the support agent cannot see the account-only memory → run the benchmark.
- **Built (2026-10-18):** the benchmark runs with its own scorer in `examples/domains/crm/run.py`, not `eval/context`, whose runner builds Sarah's world in Python (a deviation from "runs the existing harness"; same thresholds). Findings while building it: Graphiti makes an extra request named after any entity type with attributes beyond the name, and `NodeResolutions` / `SummarizedEntities` are needed once the workspace has nodes; evidence for a proposed memory must be readable by the proposing agent.
- **Docs:** `docs/guides/add-a-domain.md` walks through exactly these files in order, using the CRM as the example, with the checklist "what you never need to touch".

#### WP-B3: Real agent integrations (`make examples-agents`)
- **Decision D1 (agent framework):** the platform decision is that **all model calls go through the router** (OpenAI-compatible). The default agents therefore use a small, dependency-light tool-calling loop on the OpenAI-compatible chat API pointed at `OSO_ROUTER_BASE_URL`, with the `openai` client in the `examples` dependency group only (import-linter keeps it out of platform packages). A Claude Agent SDK variant would call Anthropic directly and bypass the router. It is offered as an optional appendix only if the owner wants it (and only if the router can front it). Claude Code / Claude Desktop are shown through MCP config instead, which needs no code.
- **`common.py`:** the loop. It sends a system prompt, the user task and the tool definitions; executes tool calls through the SDK; and puts retrieved context into the prompt as quoted data inside `<retrieved_context>` (never as instructions, per `docs/agent-integration.md` §2). Each run is capped at N turns. It logs tool names and ids, never content.
- **Tools exposed to the model** (all SDK calls with the agent's own key): `build_context`, `search_memory`, `get_evidence`, `propose_memory`, `save_working_memory`, `ingest_interaction`, `get_interaction_status`.
- **`research_agent.py`:** given "research the planned industry for Sarah", it searches, reads evidence, saves private working notes, and proposes a finding with evidence (`evidence_required`).
- **`planning_agent.py`:** builds `planning` context (600 tokens), answers the task citing item ids, and shows that the Research agent's working notes are absent while the published finding is present.
- **Offline determinism:** in mock mode, the router's chat responses (including `tool_calls`, which the mock's response model supports) come from a fixture pack per agent, so the transcript is reproducible and strict mode catches drift. Verify early that fixture rules can return `tool_calls`. If they cannot, extend the mock router (dev tooling; its own test) before anything else in this work package.
- **MCP (`agents/mcp/`):**
  - `mcp_flow.py` runs the same Research → Planning flow with the official `mcp` Python client against `apps/mcp`, and asserts **parity** with the SDK run: same item ids in the context package, same denials;
  - `claude-code.mcp.json` and `claude-desktop.json`: ready-to-copy configs that point at `make mcp-up`'s endpoint with the agent key in a header taken from an environment variable (never inline);
  - the README explains that MCP is a transport over the same authorized API, not a privileged path.
- **`unauthorized_agent.py`:** prints a table of denials and checks each one:

| Case | Expected |
|---|---|
| `other_demo` agent asks for Sarah | 404, body identical to a random id (X5); trace has no candidate ids |
| Agent without a subject grant | Facts per the ontology's access mode, no subject-scoped memory |
| Agent asks for a profile outside `allowed_profiles` | 403 `Forbidden` |
| Agent proposes with evidence it cannot see | `InvalidEvidence` |
| Revoked (retired) agent | 401 `AuthError` on the next call |
| Compromised revoke | 401, and its published findings move to `source_compromised` review |
| Quota exhausted (small quota set by the kit) | `QuotaExceeded` with `retry_after` |
| Prompt-injection text inside an ingested note | Shows up neutralised as data in the package, never as an instruction (SEC-04 corpus sample) |

- **Built (2026-10-18):** the loop talks to the router with plain `httpx` (no `openai` dependency, so D1 holds with less); the mock router gained a scripted `_message` response so a rule can return `tool_calls` (test in `tools/mock_router/tests/test_contract.py`); `make test-mock` was already broken before this work (the spikes environment lacks `kg_common`), run the mock suite with `uv run pytest tools/mock_router/tests` instead. Two denial cases differ from the table above: a profile outside `allowed_profiles` is `422 profile_not_allowed`, and a request quota is `QuotaExceeded`. MCP parity is asserted on the cited item handles, not on prose.

#### WP-B4: Jupyter notebooks (`examples/notebooks/`)
- **Dependencies:** a `uv` dependency group `examples` with `jupyterlab`, `ipykernel`, `nbclient`, `nbformat`, `networkx`, `matplotlib`, `openai` (WP-B3). `make examples-install` installs it; the platform's lock stays authoritative.
- **Rules for every notebook:** the first cell calls `_kit.notebook_env("<name>")`, which gets a fresh workspace and starts the mock context. Markdown explains each step in plain terms. Each code cell **asserts** what it shows, so `test-examples` fails for a real reason. Outputs are stripped in git (decision D4), with a check in `make lint` that fails on committed outputs. Runtime is under 3 minutes each in mock mode.

| Notebook | Shows |
|---|---|
| `01_ingest_and_ledger` | The event envelope, `Idempotency-Key`, same key twice gives one event, a different body is 409, stage timeline, encryption at rest (the payload column is ciphertext), audited replay |
| `02_temporal_graph_as_of` | Commands vs extracted facts; `as_of` and `known_at` queries on Sarah's employment before and after `evt_006`; a networkx/matplotlib drawing of the subgraph at two points in time; a provenance walk from a fact back to its episode |
| `03_curated_memory` | Proposal → policy → publish; scopes and subjects; hybrid search (lexical + vector, `mem-rrf.v1`) with the score parts shown; why the cycling distractor ranks low |
| `04_context_build_and_budget` | The same task at 300, 600 and 1200 tokens: what is cut, in what tier order, `insufficient_budget`; unknowns from `expected_signals`; the trace; re-materialisation after a change marks an item `withdrawn` (X9) |
| `05_multi_agent_sharing` | Two agents, grants, working notes vs published findings, `agent_generated` vs `verified`, revocation; the same calls via SDK and via MCP |
| `06_corrections_and_deletion` | Impact preview → correction → next build reflects it; identity split; deletion freeze → plan → purge → verify → receipt; legal hold blocking a purge |
| `07_second_domain` | Loads the CRM from `examples/domains/crm/`, runs `renewal_prep`, and diffs it against Sarah's `planning` profile to show that only config changed |

#### WP-B5: Documentation
- **`README.md`** becomes a real quickstart: what it is (3 lines), prerequisites, `make tools sync admin-install up seed`, then `make examples` and what you will see, links to the examples index, admin console URL and login, and "where to go next". The "start here if picking up the work" table moves below.
- **`examples/README.md`:** a table of examples with command, runtime, what it proves, and blueprint ids; a section on running live (owner, cost warning).
- **`docs/architecture/overview.md`:** one page. A component diagram (API, worker, Postgres ledger/memory, Neo4j/Graphiti projection, router, admin, SDK/MCP), the write path (command or interaction → ledger → outbox → projection/extraction → memory) and the read path (context build: plan → authorize → retrieve → merge → reconcile time → rank → pack → trace), each step linked to its ADR and blueprint section.
- **`docs/guides/extend.md`:** add a domain (links to add-a-domain), add or narrow a context profile, onboard an agent, add an ingestion source, swap the router or embedding model (re-embed runbook), production deployment (runbook), and where the hard rules live (RLS, `SubjectFilter`, egress).
- **ROADMAP Phase 10 status** paragraph in the usual shape.

#### WP-B6: Make targets, tests and CI
- **Targets** (with `##` help text):
  - `examples-install`;
  - `examples`: `mcp-up` once (X7), then `demo-sarah`, `demo-crm`, `examples-agents`;
  - `demo-sarah`, `demo-crm`, `eval-crm`, `examples-agents`;
  - `notebooks`: executes them in place, for people who want outputs locally;
  - `test-examples`: `pytest tests/examples`, which runs each example as a subprocess and each notebook through `nbclient`, failing with the step or cell that broke.
- **`tests/examples/`:** one test per example and notebook, the CRM config-only check, the no-committed-outputs check, and a run under `KG_REPLICAS=2` in the nightly sweep (X11).
- **CI (`.github/workflows/ci.yml`):** after the existing clean-start demos, run `make examples-install examples test-examples`. A GitHub runner is a fresh clone by definition, so this job **is** the fresh-clone gate. Upload the Sarah and CRM reports as artifacts.
- **Release candidate:** add an `examples` step to `scripts/release_candidate.py` (`STEPS`) after `test-e2e`, with the reports as evidence.

#### WP-B7: Final release-candidate run and hand-over
1. Clean tree; disk check (WP-A2); the perf stack is down (`make perf-down`).
2. `make release-candidate`, about 100 minutes. **It drops the dev volumes**, so tell the owner before starting. Fix whatever fails and rerun with `ARGS="--only <step>"`.
3. Commit "Phase 10: examples, docs, release candidate" (owner approves); rewrite `docs/release/handoff.md` to say the remaining work is the owner actions.
4. **(owner)** `make live-session` → review `proposal.diff` → `make live-session-apply`; optionally `make demo-sarah ARGS=--live`. Set DEC-11 and DEC-07; sign `docs/release/checklist.md` and threat model §5; `git tag v1.0.0-rc1`.

---

## 6. Data and API Changes
None planned. The examples use the existing API, SDK, MCP and CLI. Changes that are allowed: the `examples` dependency group; mock-router tooling, if `tool_calls` fixtures need it; any platform bug fix the examples expose, each in its own commit with a test (after which `make openapi` runs if the API changed, per CLAUDE.md §4).

---

## 7. Sequencing

```
WP-A0 (after the running baseline) ─► WP-A1 ─► WP-A2 ─► WP-A3 commit
                                                         │
WP-B0 kit ─┬─► WP-B1 Sarah ──────┬─► WP-B4 notebooks ─► WP-B5 docs ─► WP-B6 targets/CI ─► WP-B7 RC + hand-over
           ├─► WP-B2 CRM ────────┤
           └─► WP-B3 agents/MCP ─┘
```
- WP-B0 can start while the baseline runs, since writing the kit is not heavy. **Do not run** stack-heavy tests until the baseline finishes.
- B1, B2 and B3 are independent once B0 exists (separate directories), so they can run in parallel sessions. Each needs the dev stack; they do not conflict, because each uses its own workspace (X1). Notebook 07 depends on B2; notebooks 02–06 on B1 and B3.
- **Decision D2:** the release-candidate run happens **once, at the end** (WP-B7), not after Part A. That way the RC covers the examples and any platform fixes they force.

Rough size: A0–A3 about 1 day (plus the baseline already running); B0 0.5 day; B1 1–1.5 days; B2 1.5 days; B3 1.5 days; B4 1.5 days; B5 0.5–1 day; B6 0.5 day; B7 0.5 day plus RC time.

---

## 8. Gate Mapping

| Blueprint id | Shown by |
|---|---|
| AT-01 | Sarah step 2, notebook 01 |
| AT-02..05 | Sarah steps 4 and 7, notebook 02 |
| AT-06..09, AT-12 | Sarah step 6, notebook 04, CRM benchmark |
| AT-10, AT-11, AT-14 | Sarah steps 5 and 8, research/planning agents, unauthorized agent, notebook 05 |
| AT-13, AT-15..17 | Sarah steps 9 and 10, unauthorized agent (injection row), notebook 06 |
| AT-18 | not an example: covered by `restore-rehearsal` in the RC |
| P-01 (reusability) | CRM: config-only check, notebook 07, add-a-domain guide |
| SEC-01, SEC-03, SEC-04 | Sarah step 8, unauthorized agent, MCP parity |

---

## 9. Exit Criteria
1. Part A: the baseline report exists; sizing and DEC-11 proposals are written; the search fix is tested and committed; ROADMAP P9 status is present; `make help` works; CI state is known and green.
2. `make examples` and `make test-examples` pass on the dev stack, twice in a row without a reset, and under `KG_REPLICAS=2`.
3. The CI job (a fresh clone) passes `make tools sync admin-install up seed examples-install examples test-examples`.
4. The CRM diff touches nothing under `apps/` or `packages/`; `make eval-crm` passes (leakage 0, budget 100%, attribution 100%).
5. `make release-candidate` passes, with the examples step included; `docs/release/rc-report-<date>.md` is committed.
6. README quickstart, examples index, architecture overview and extend guide are written; ROADMAP P10 status is present; the handoff names only owner actions.

---

## 10. Risks and Mitigations
| # | Risk | Mitigation |
|---|---|---|
| R1 | The running baseline fails or finds another slow query | Fix it in WP-A0 with a plan-shape test like the search fix; rerun only `perf-baseline` |
| R2 | `known_at` cannot express a future fixture's record date (step 7) | Settle it first in WP-B1 (see the open question there); demonstrate with real record times; no fake-time backdoor |
| R3 | Mock fixtures cannot return `tool_calls` | Verify on day one of WP-B3; extend the mock router (tooling) with a test |
| R4 | The CRM ontology needs something the platform does not support | Treat it as a finding: a generic platform fix in its own commit, or a documented limitation in add-a-domain; never CRM-specific code |
| R5 | Notebooks are slow or flaky in CI | Strict mock, per-cell assertions, explicit waits with timeouts (`_kit.waits`), a 3-minute budget per notebook |
| R6 | Fresh workspaces accumulate in the dev database | They are small; `make reset` clears them; `examples-clean` if a governed delete path exists (D5) |
| R7 | Disk fills during the RC (it happened before) | Disk check and perf stack down before WP-B7 (X14) |
| R8 | Examples drift from the API | They run in CI on every push; the SDK models already fail on drift (`test_models_drift.py`) |

---

## 11. Rollback
Everything in Part B is additive (`examples/`, `tests/examples/`, docs, Make targets, one CI job, one RC step) and can be reverted commit by commit. The search fix in WP-A0 reverts to the previous SQL; its test documents why it should not be reverted.

---

## 12. Decisions (recommended; the owner confirms)
| # | Decision | Recommendation |
|---|---|---|
| D1 | Agent framework for the examples | A router-backed OpenAI-compatible tool loop (it keeps the "every model call goes through the router" rule). Claude Code / Desktop through the MCP configs. A Claude Agent SDK variant only if the owner asks, and documented as bypassing the router |
| D2 | When to run the release candidate | Once, at the end of Phase 10 (WP-B7), so it covers the examples |
| D3 | Second domain | Project / Customer CRM, as the roadmap suggests |
| D4 | Notebook outputs in git | Stripped (deterministic diffs, no stale outputs); `make notebooks` renders locally; the committed Sarah and CRM reports show sample output |
| D5 | Example workspaces | One fresh workspace per run (isolation beats tidiness, X1); cleanup through `make reset` or a governed `examples-clean` |
