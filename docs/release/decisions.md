# Open production decisions register (DEC-01 … DEC-12)

Status after phase 9. "Owner" items need a person's value before production; the platform ships a safe default for each and says so.

| DEC | Question | Status | Where it is settled |
|---|---|---|---|
| DEC-01 | Exact Graphiti, Neo4j and Pydantic versions | **Closed** | ADR-011; `compat/versions.lock.yaml` (`refreshed_on`); upgrade procedure in `docs/runbooks/upgrade-pin-dependencies.md` |
| DEC-02 | Model providers; where private data is processed | **Closed once the live session is applied** | ADR-012 (Provisional until `make live-session-apply` flips it to Accepted); the egress policy and per-workspace denies (ADR-047) are enforced and tested now |
| DEC-03 | Identity provider | **Closed** | OIDC for people (ADR-049), API keys for agents/apps, break-glass flag; `docs/runbooks/oidc-setup.md` |
| DEC-04 | Representing corrected extracted edges | **Closed** | ADR-018, ADR-041 |
| DEC-05 | Runtime Pydantic classes from the ontology | **Closed** | ADR-014 (allow-listed typed classes + validated mapping) |
| DEC-06 | Graph tenancy: shared database + `group_id` vs a database per tenant | **OPEN production decision** | See below |
| DEC-07 | Source retention and backup windows | **Owner-set** | Retention is configurable per workspace (ADR-047). Platform defaults: backups kept `BACKUP_RETENTION_DAYS=30` (always the newest 3); raw events minimised; deleted data returns after a restore for **at most the interval between a deletion and the next restore**, and `kg deletions reapply` removes it again. **Owner: set the retention and the maximum backup age they accept, then record it here.** |
| DEC-08 | Embeddings for all records or selective | **Closed** | ADR-013 (searchable active curated memories only) |
| DEC-09 | Which fields agents may treat as procedural instructions | **Closed** | ADR-030, ADR-038 (published preferences only) |
| DEC-10 | UI framework and visualisation | **Closed** | React 18 + TypeScript + TanStack Query + Cytoscape (`apps/admin/package.json`; exact versions in `package-lock.json`) |
| DEC-11 | p95 and freshness SLOs | **Owner-set from the baseline** | Proposals in `eval/reports/baseline-<date>.md`; the alert thresholds in `deploy/observability/rules.yml` marked `SLO (proposal)` change with the owner's values. **Owner: record the values here.** |
| DEC-12 | Bitemporal point-in-time guarantee | **Closed for MVP** | Valid-time and record-time metadata are kept and queryable (`as_of`, `known_at`); a formal point-in-time *guarantee* (an SLA over history rewrites) is out of scope and documented |

## DEC-06 — graph tenancy (open production decision)
- **Today:** one Neo4j database, `group_id` per workspace, derived from the authenticated workspace only and never accepted from a client; every graph result is re-checked against Postgres ACLs; leak suites and route walkers cover it. Neo4j Community Edition cannot create databases.
- **Options:** (1) keep shared database + `group_id` (cheapest; isolation is application-enforced); (2) one Neo4j **instance** per tenant (strong isolation; operations scale with tenant count); (3) Neo4j Enterprise multi-database (strong isolation, licence cost).
- **Triggers to move off (1):** a regulated or contractual isolation requirement from a tenant; any cross-tenant graph finding in the sweep or in production; a tenant that needs its own backup/retention/region.
- **Owner:** the platform owner decides with security. **Until then** the threat model carries it as accepted finding F-02.

## DEC-11: SLO proposals (the owner sets the values)

**Proposals only.** Source: `eval/reports/baseline-2026-10-18.md` (M dataset, provisional router latency model, one API replica; sizing in [deploy-production.md](../runbooks/deploy-production.md)). A proposal is the measurement with about 50 % headroom, rounded. The alert thresholds marked `SLO (proposal)` in `deploy/observability/rules.yml` do **not** follow these yet; they change once the owner records values.

| Objective | Measured | Proposed | Alert today | Notes |
|---|---|---|---|---|
| Context build p95, 3 builds/s, 50,000 memories | 1.58 s (p50 0.74 s) | **2.4 s** | 8 s (`ContextLatencyHigh`) | 1.65 s at 50,000 and 1.70 s at 200,000 in the size curve, so the build time is not what the dataset size drives |
| Memory search p95, 8/s, 50,000 memories | **3.6 s** (p50 0.45 s) | **no proposal for the exact scan** | none | Any useful objective (say 1 s) fails at 50,000 memories today. Propose the objective with the ANN index, and measure again; see ADR-019 and the trigger in the baseline |
| Ingest accept p95 (`POST /v1/interactions`) | 25 to 42 ms | **100 ms** | none | |
| Freshness p95 (accepted to terminal), up to 0.6 events/s | 147 s steady, 104 s long documents | **300 s** | `OutboxOldestPending` at 10 min | Holds only below about 0.3 events/s sustained per worker at the defaults; at 7 events/s 81 of 227 events were terminal after 300 s |
| Server error rate (5xx) | none in about 1,900 requests (every status was 200, 202 or 429) | **0.1 %** | none | The baseline's own `api_error_rate` counts 429s; this proposal does not |
| Context builds refused (429) | 0.07 steady, 0.79 burst | not an error SLO | none | The 6-in-flight cap per workspace (about 5 builds/s at 50,000 memories) is by design. A client above it should back off on `retry_after` |
| Events reaching a terminal state | 100 % of steady and long events | 99 % within the freshness objective, **zero silent loss** | `DeadLetters` | |

Open questions for the owner: whether search is in or out of the first SLO set until ANN lands; whether the freshness objective should assume a faster worker (raising `WORKER_CONCURRENCY` and `SEMAPHORE_LIMIT`, at router cost); and whether to re-measure after `make live-session-apply`, which replaces the modelled latency.

## Other decisions recorded in phase 9
| Topic | Decision |
|---|---|
| Communities (V12) | **Stay disabled.** Their value cannot be measured offline (the mock extraction has no community structure); revisit after a live-session measurement. |
| ANN index (V9) | **Exact scan until a measured trigger.** The size curve (S/M/L) in the baseline report records context p95 against memories per workspace; add HNSW when the L-size p95 exceeds the proposed SLO (ADR-019 keeps the exact scoped search as the safe default). |
| Per-principal RLS | **Not adopted** (ADR-048). Workspace-level RLS + application ACLs. |
| Content-reader database role | **Not adopted** (ADR-048). |
| LISTEN/NOTIFY outbox dispatch | **Kept as polling.** Cache invalidation uses NOTIFY (ADR-050); job dispatch stays on `SKIP LOCKED` polling unless the baseline's queue-wait numbers say otherwise. |
| Production target | Single VM + Docker Compose reference (ADR-053); Kubernetes is not provided. |

## DEC-13 .. DEC-19: the public site and sandbox (phase 11)

**Decided by the implementing agent on the owner's instruction to use best judgement (2026-10-18). The owner confirms or changes each; none of them is irreversible.**

| DEC | Question | Decision taken | What changing it costs |
|---|---|---|---|
| DEC-13 | Licence | **Apache-2.0** (`LICENSE`, `NOTICE`): permissive, with an explicit patent grant | One file and the README line; do it before the first release |
| DEC-14 | Public name and domain | Placeholder **`kg.oso.group`** (site) and **`sandbox.kg.oso.group`** (sandbox); two hosts so the static site and the API never share an origin. Set `SITE_URL` when building | A build variable and the CORS origins |
| DEC-15 | Static hosting | **Cloudflare Pages** through `.github/workflows/site.yml` (preview per pull request), enabled by the `SITE_DEPLOY` variable and two secrets. Nothing deploys until the owner sets them | A different deploy step in one workflow |
| DEC-16 | Sandbox host and budget | One dedicated VM of the small size from the production sizing table (8 vCPU, 16 GB; about 40 to 60 EUR a month), **its own Postgres and Neo4j**, a cap of 40 live sessions | Compose variables |
| DEC-17 | Sandbox abuse control | Cloudflare Turnstile on "Start a sandbox" (optional until its secret is set), at most 3 sessions per address per hour, 120 requests a minute per principal, bodies capped at the edge | Variables and the Caddyfile |
| DEC-18 | Analytics | **None.** No cookies, so no banner | Adding a cookieless counter is one script tag |
| DEC-19 | Launch status | Launch as **pre-1.0** with the status box on the landing page and the README, unless `v1.0.0-rc1` is tagged first | The landing page's status section |

Still open for the owner: the security contact in `SECURITY.md` (it names GitHub's private vulnerability reporting, which the owner enables), whether to enable GitHub Discussions, and the repository description, homepage and topics.

## Live session 2026-10-21 (first run against the real router, `https://models.oso.group`)

Evidence: `eval/reports/live/2026-10-21/summary.md` (and `b`..`f` reports), `extraction-live-2026-10-21`, `memory-live-2026-10-21`, `context-live-2026-10-21`, `consolidation-live-2026-10-21`. The key was capped at `confidential`; structured purposes run on `oso/code` (+ `oso/code-high`).

**Applied (owner instruction):** `SEMAPHORE_LIMIT=8` (GR-13: 16 parallel requests, no 429), `CONTEXT_TOKEN_MARGIN=0.20` (code default and `.env.example`), and the captured latency profile `tools/mock_router/latency.live.json` (chat p50/p95 ms: oso/fast 605/2544, oso/general 915/39717, oso/code 1098/1679; embedding 232/549).
**Not applied:** the proposal's `CONTEXT_TOKEN_MARGIN=0.85` (driven by `oso/code` on synthetic UUID-heavy text, 1.82x, and number-heavy text, 1.60x; realistic context text is within 1.0-1.2x on every alias, and 0.85 would pack contexts to about half their budget) and the status flips of ADR-012, ADR-013, ADR-020 and `compat/model-purposes.yaml` to Accepted: they stay **Provisional** while the failures below are open.

**What held (strict counters, all required to be exact):** authorisation leakage 0, context budget adherence 100 %, attribution 100 %, memory escalation violations 0, unmapped-subject leaks 0, procedural auto-activations 0, cross-workspace links 0, simulation left 0 staging nodes and changed no workspace row, consolidation PASS. Embeddings are 384-dimensional; related-name cosines 0.89-0.96.

**Failed, and why (proposed thresholds; owner decides blocker or accepted exception):**
| Evaluation | Result | Needs | Cause |
|---|---|---|---|
| Extraction (`eval-extraction-live`) | fact precision 0.67, recall 0.67 | 0.8 | the model invents relation names outside the ontology (`MANAGER_OF`, `MEMBER_OF`) instead of `MANAGES`, and missed a `KNOWS` edge; the platform rejects the invented types (the safeguard works) |
| same | `unquarantined_unparseable` = 1 | 0 (strict) | the `bad_json` scenario needs the mock to return broken JSON; a real model answers normally, so this scenario cannot be evaluated live. An evaluation-design gap, not a platform fault |
| Memory (`eval-memory-live`) | candidate precision 0.33, recall 0.2 | 0.8 | about three times as many candidates proposed as wanted |
| Context (`eval-context-live`) | irrelevant inclusion 0.50; temporal correctness 0.875 | 0.10; 100 % | noise from the extra live memories and facts packed into contexts |
| Simulation (`eval-simulation-live`) | reported relationship types the draft does not declare (MANAGES, MEETS_WITH) | none | same invented-relation behaviour; cost actual $0.012 |

**Speed (affects DEC-11 and the sizing in the production runbook):** real extraction takes a median 105 s per event (p95 161 s; 4.7 chat calls, 8.2k tokens); memory proposal a median 97 s (p95 146 s). At the default concurrency that is roughly 0.04 events/s per worker, against 0.3 events/s in the modelled baseline. The freshness proposal (p95 300 s) and the VM sizing must be redone from live numbers; `make perf-baseline` with `MOCK_ROUTER_LATENCY_PROFILE=tools/mock_router/latency.live.json` is the first step.

**Recommendation recorded:** none of the failures is a safety or isolation failure. Treat extraction quality as a blocker for enabling LLM extraction in production, not for the release candidate; compare stronger aliases (`oso/code-high`, `oso/frontier`), put the ontology's relation names in the extraction prompt, and cap memory candidates per event, then re-run the live evaluations. Mark the `bad_json` scenario mock-only.

### Alias comparison for structured purposes (live, 2026-10-21; extraction and memory evaluations, same scenarios)
Reports: `eval/reports/{extraction,memory}-live-2026-10-21[-oso-code-high|-oso-frontier].{json,md}`. `oso/code-high` and `oso/frontier` both start their chain with `glm-5.2`, so they are effectively one model against `oso/code` (`qwen3-coder` first); no fallback fired as far as the latencies show.
| | `oso/code` (baseline) | `oso/code-high` | `oso/frontier` |
|---|---|---|---|
| Extraction fact precision / recall (need 0.8) | 0.67 / 0.67 | **0.86 / 1.00** | **0.86 / 1.00** |
| Extraction latency p50 / p95 per event | 105 s / 161 s | **28 s / 45 s** | 29 s / 30 s |
| Tokens per extraction event | 8,219 | 6,756 | 6,264 |
| Invented relation names | `MANAGER_OF`, `MEMBER_OF` | one wrong edge (`CONSIDERING` cycling) | one extra edge (`ENCOURAGES`) |
| Memory candidate precision (need 0.8) / recall | 0.33 / 0.20 | **0.67** / 0.40 | 0.40 / 0.40 |
| Memory recall@3 (need 0.9) | 1.00 | 0.75 | 1.00 |
| Memory latency p50 / p95 | 97 s / 146 s | **39 s / 48 s** | 38 s / 87 s |
| Safety counters (escalation, unmapped-subject leaks, procedural auto-activation, scope violations, cross-workspace links) | all 0 | all 0 | all 0 |

Reading: extraction meets its thresholds with `glm-5.2` (the only remaining extraction problem, `unquarantined_unparseable`, is the mock-only `bad_json` scenario). Memory candidate precision is still below 0.8 on every alias, so that needs prompt or policy work (`extraction_min_importance`, `max_memories_per_interaction`), not a different model. Cost is higher per event with `glm-5.2` (price table: input 1.4 / output 4.4 USD per million tokens, against 0.22 / 1.8 for `qwen3-coder`), roughly 0.02 against 0.01 USD per extraction event from the measured token counts; the router ledger is authoritative.
**Applied 2026-10-21 (owner instruction):** `oso/code-high` is the primary for the structured purposes with `oso/code` as fallback (`compat/model-purposes.yaml`, still `provisional`); tests updated; the live evaluations are to be re-run for the record.

### Memory tuning on `oso/code-high` (live, 2026-10-21): `extraction_min_importance` x `max_memories_per_interaction`
Reports: `eval/reports/tuning-memory-2026-10-21/`. Five runs of the live memory evaluation (8 scenarios, 5 gold memories), policy varied through a wrapper, no repo file changed. The first row is the unchanged default; the earlier comparison run with the same default scored precision 0.67 / recall 0.40.
| min importance | max per event | TP / FP / FN | precision | recall | recall@3 | safety counters |
|---|---|---|---|---|---|---|
| 0.5 (default) | 3 (default) | 1 / 6 / 4 | 0.14 | 0.20 | 1.00 | all 0 |
| 0.7 | 3 | 1 / 4 / 4 | 0.20 | 0.20 | 0.75 | all 0 |
| 0.3 | 3 | 2 / 1 / 3 | 0.67 | 0.40 | 1.00 | all 0 |
| 0.5 | 1 | 1 / 3 / 4 | 0.25 | 0.20 | 1.00 | all 0 |
| 0.7 | 1 | 2 / 1 / 3 | 0.67 | 0.40 | 0.75 | all 0 |

**Conclusion: the two knobs do not explain the result, and the default stays.** The unchanged default scored 0.67 in one run and 0.14 in another, a spread larger than any difference between settings, and no setting got past 2 of the 5 gold memories. Two causes, neither of them a threshold:
1. **Nothing pins the model's temperature.** No model call in the platform sets `temperature`, so structured extraction runs at the router's default (documented as 0.7) and gives different answers on each run.
2. **The gold check is an exact substring match** (`must_contain` in `eval/memory/score.py`), so a correct memory in different words counts as both a false positive and a miss (for example "Sarah Johnson is considering entrepreneurship as a path" against a gold that wants "software business" and "Michael").
**Recommended next steps (done, see the next subsection):** pin `temperature` to 0 for the structured purposes (a per-purpose setting in `compat/model-purposes.yaml` passed by the gateway), replace the substring check with a semantic match for live mode, and report the mean and range of three runs per scenario. Only then are precision and recall figures worth tuning against.

### Temperature pinned, semantic matching, repeated runs (applied and re-run live, 2026-10-21)
**Applied (owner instruction):** `temperature: 0` per purpose in `compat/model-purposes.yaml` on the five structured purposes (`PurposeSpec.temperature`, sent by `ModelGateway`, and used by the Graphiti client, whose own default is 1.0; tests in `packages/modelgw`, `packages/graph`); the live memory evaluation now takes `--repeat N` (every scenario N times in fresh workspaces, mean and range per run and per scenario) and `--semantic` (on in live mode): when the exact `must_contain` check fails, a judge model (`oso/code-high`, temperature 0, strict JSON schema) says whether the memory conveys the required elements in other words; `must_not_contain`, type, status, subjects, scope and linked facts stay exact; every verdict is in the report (`eval/memory/judge.py`, `eval/tests/test_memory_semantic.py`). The offline evaluation is unchanged (100 %).
**Probe:** the router accepts `temperature: 0` with structured output on `oso/code-high`. `glm-5.2` spends its first tokens on hidden reasoning, so a small `max_tokens` (about 50) returns an empty reply with `finish_reason: length`; give structured calls generous limits (the judge uses 1500).

| min importance / max per event | runs | precision mean (range) | recall mean (range) | TP / FP / FN per run | recall@3 | strict counters |
|---|---|---|---|---|---|---|
| 0.5 / 3 (default) | 3 | 0.52 (0.40-0.67) | **0.40 (0.40-0.40)** | 2/3/3; 2/2/3; 2/1/3 | 1.00 | all 0 in every run |
| 0.5 / 1 | 2 | 0.46 (0.25-0.67) | 0.30 (0.20-0.40) | 1/3/4; 2/1/3 | 1.00 | all 0 |
| 0.7 / 3 | 2 | 0.42 (0.33-0.50) | 0.50 (0.40-0.60) | 3/3/2; 2/4/3 | 0.75 | all 0 |

Reports: `eval/reports/memory-live-2026-10-21-temp0-semantic-x3.{json,md}` and `eval/reports/tuning-memory-2026-10-21/temp0-semantic/`. Before the change the same default scored precision 0.14 to 0.67 and recall 0.20 to 0.40 between runs; now the scenarios that count are identical in every run (recall 0.40 each time) and only the number of extra candidates moves.
**Conclusion: keep the defaults (0.5 / 3).** A cap of 1 loses recall; 0.7 lowers recall@3 to 0.75.
**Why precision still misses 0.8, in the scenario gold set (frozen, an owner act to change; not touched):** (1) `mem_evt003` requires the phrase "software business", which the source text ("considering entrepreneurship") never contains: it came from the mock's canned answer, and the model's faithful memory also fails the linked-fact check; (2) `mem_injection` expects the system to propose a malicious procedural memory for review, and the real model refuses, which is the safer behaviour; (3) `mem_cyclist`: the model keeps "40 km" and "Tom" but drops "cycling", a fair miss (the judge said so); (4) the model sometimes splits one event into two or three memories (for example a membership claim next to the ride), which the cap and importance do not remove. Gold changes to consider at the next freeze: require "entrepreneur" instead of "software business" in `mem_evt003`, and accept "no memory" or a reviewed procedural proposal in `mem_injection`.

**Gate baseline:** `eval/baselines/release.json` `context.tokens_per_call` moved from 230.7 to 257.7 (+11.7 %, limit 10 %) because `CONTEXT_TOKEN_MARGIN` went from 0.10 to 0.20 (every token is counted 1.2x instead of 1.1x); that one value was updated by hand and nothing else in the baseline (`--write-baseline` would also have reset the owner's `p95_slack_seconds` of 1.0 to 0.25 and re-based unrelated latency figures, so it was not used).

