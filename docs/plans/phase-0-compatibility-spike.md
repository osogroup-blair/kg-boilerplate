# Phase 0: Compatibility Spike and Version Pinning (Detailed Implementation Plan)

**Roadmap:** [ROADMAP.md](../../ROADMAP.md), Phase 0
**Blueprint inputs:**
- `05-delivery/03` (DEC-01…DEC-12)
- `02-domain/01` (ontology compiler)
- `02-domain/02` (temporal mapping)
- `03-runtime/02` (ingestion)
- `03-runtime/03` (pgvector caveats)
- `04-administration/02` (egress)

**Router inputs:** `~/Documents/oso_model_router`. The routing, governance, capabilities, API-reference and integration pages are in `web/src/features/docs/content/*.ts`. The API schemas are in `src/api/chat/schema.ts` and `src/api/embeddings/schema.ts`. The models and aliases are in `scripts/seed-logical-router.ts`.
**Status:** Implemented. Offline exit criteria met on 2026-10-11 (see `docs/compatibility-report.md`); live finalisation pending `make verify-router`.

---

## 1. Objective and Constraints

Remove the technical unknowns that would otherwise get decided silently in later phases. Phase 0 produces:
1. **Pinned versions** for Python, Graphiti, Neo4j, Postgres, pgvector, Pydantic and the core libraries.
2. **Evidence-backed ADRs** for DEC-01, 02, 03, 04, 05, 06 and 08, plus parts of 11 and 12.
3. **A router contract** (`docs/router-contract.md`) that all later phases code against.
4. **A mock router** that faithfully reproduces that contract, so P1–P6 can be built and tested without the live router.
5. **A live router verification kit** that runs later. That run turns the provisional decisions into final ones.

**Constraint:** the Oso Model Router runs at `127.0.0.1:8780` on the user's machine, so it **cannot be reached from this build environment**. Every check is tagged as one of:
- **OFFLINE:** runs now, against Neo4j, Postgres and the mock router.
- **LIVE-DEFERRED:** scripted now, run later with `make verify-router`. Any ADR that depends on it is marked `Provisional (pending live router verification)`.

Spike code is disposable. It goes under `spikes/` and is not imported by P1. The **deliverables** are the documents, the pin file, the mock router and the verification kit.

---

## 2. What the Router Docs Tell Us (analysis)

These facts come from the router docs and source. They drive the work packages below.

### 2.1 Contract summary

| Topic | Router behaviour | Implication for the platform |
|---|---|---|
| Base URL / auth | `http://127.0.0.1:8780/v1`, `Authorization: Bearer <key>` (keys are scoped per environment, scope `chat`) | `OSO_ROUTER_BASE_URL` and `OSO_ROUTER_API_KEY` in `.env`. Separate keys for `kg-api` and `kg-worker` so spend can be attributed to each. |
| Endpoints | `POST /v1/chat/completions` and `POST /v1/embeddings` (OpenAI-compatible); `GET /v1/models` (lists aliases only); `GET /health` (no auth) | There is no `/v1/responses` endpoint, so any library that calls the OpenAI Responses API or `beta.parse` will fail. Graphiti must use its **chat-completions (generic) client**. |
| Aliases | `oso/fast`, `oso/general`, `oso/reasoning`, `oso/code`, `oso/code-high`, `oso/vision`, `oso/general-vision`, `oso/frontier`, `oso/embedding` | The platform refers to **purposes**, never models. A purpose-to-alias map in config resolves them (§2.3). |
| Structured output | Any `response_format` other than `text` adds the required capability `structured_output`. Only **glm-5.2, gemini-3.8-flash, gemini-3.5-flash, gemini-3.1-pro-preview and gemini-2.5-pro** have it. | `oso/fast` has **no** eligible model and fails with `unsupported_capability`. `oso/general` falls back to a **single** model (gemini-3.5-flash), so there is no fallback for extraction. `oso/code` has 2 eligible (glm-5.2, gemini-3.8-flash). **Recommend asking the router admin for an `oso/structured` alias** (glm-5.2 → gemini-3.5-flash → gemini-3.8-flash → gemini-3.1-pro-preview). |
| Unknown fields | Zod strips unknown top-level request fields | `logprobs`, `top_logprobs`, `logit_bias`, `seed` and `n` are **silently dropped**. Graphiti's default `OpenAIRerankerClient` depends on logprobs, so it **will not work**. Use RRF/MMR search recipes without a cross-encoder (§WP4). |
| Embeddings | `oso/embedding` resolves to `multilingual-e5` = `intfloat/multilingual-e5-small` on Vertex, us-central1 only, **no fallback**, $0.015/MTok | **384 dimensions**: the pgvector column is `vector(384)` and Graphiti uses `embedding_dim=384`. E5 expects `query: ` / `passage: ` prefixes and has a **512-token** input limit. The router advertises 8192, which is the deployment's figure, not the model's. The gateway must chunk and add prefixes. The single deployment is a single point of failure for writes. |
| Data classification | `routing.data_classification` ∈ `public, internal, confidential, restricted, local_only`; `routing.privacy` is a ceiling; a key's `--max-privacy` caps it | Maps almost 1:1 to the blueprint's classes, which gives SEC-06 and ING-06 an enforcement point at the router. `local_only` is **rejected** (`data_policy_violation`) in the standard setup because no local deployment exists. The platform must treat "no egress allowed" as **skip the LLM stage** (stage state `skipped_by_policy`), not as an error to retry. |
| Metadata | `metadata.{application_id, user_id, conversation_id, request_id, task_type, agent, data_classification}`, `user`, header `x-request-id` | Propagate the platform's `correlation_id`, `workspace_id` (hashed) and `purpose` so the router ledger can be joined to platform traces. Never send raw subject names. |
| Response | `model` echoes the alias, `usage{prompt,completion,total}_tokens`, header `x-gateway-request-id`. Provider and model headers are returned **only to admin keys**. `routing_decision` appears "when enabled". | **Cost is not in the response.** The platform records tokens plus the router request ID and estimates cost from a local copy of the price table. The authoritative cost stays in the router ledger. The physical model is unknown to non-admin keys, so record the alias plus the config version. |
| Errors | Envelope with codes: `invalid_request, authentication_error, model_not_found, unsupported_capability, privacy_policy_violation, data_policy_violation, classification_not_permitted, cost_limit_exceeded, context_too_large, rate_limited, budget_exceeded, provider_unavailable, routing_failed, timeout, unknown_provider_error, forbidden…`. 429 comes with `Retry-After`. | Map each to a platform category: **retryable** (rate_limited, provider_unavailable, timeout, unknown_provider_error), **policy-terminal** (data/privacy/classification/forbidden → `skipped_by_policy` or `failed_policy`), **config-terminal** (model_not_found, unsupported_capability, context_too_large → alert the operator) and **budget** (budget_exceeded, cost_limit_exceeded → pause the queue with backoff). |
| Fallback | Automatic on retryable errors; `routing.fallback:false` disables it; streams commit to a candidate at the first byte | Keep fallback on for extraction. Embeddings have no fallback, so the worker retry handles it. |
| Revocation | Effective within **60 s** (cache TTL) | This applies only to the platform's own router keys. Agent revocation is platform-side and immediate (AGT-05), so there is no conflict. |
| Limits | Client/alias RPM and TPM, provider concurrency, budgets that warn, throttle or block | Graphiti fans out many LLM calls per episode. Set `SEMAPHORE_LIMIT` and a client-side token bucket below the key's limit, and honour `Retry-After`. |
| MCP | `/mcp` is discovery only (list_models, health, openapi) | Not used by the platform. Optionally useful for the agents in P10. |

### 2.2 Model-dependent token counting

The router picks the physical model, so the platform cannot use that model's own tokenizer. **CTX-01 counts the platform's output** (the context package), not router calls. Decision to validate: use `tiktoken` `o200k_base` with a configurable safety margin (default 10%), and record the tokenizer ID in every trace. The live run calibrates it against the `prompt_tokens` the router reports for gemini and glm.

### 2.3 Purpose-to-alias map (initial, provisional)

```yaml
model_purposes:
  graph_extraction:      { alias: oso/general,   requires: [structured_output], fallback_alias: oso/code }   # → oso/structured once created
  graph_extraction_small:{ alias: oso/general,   requires: [structured_output] }                             # NOT oso/fast
  memory_extraction:     { alias: oso/general,   requires: [structured_output] }
  context_compression:   { alias: oso/fast,      requires: [] }          # P6 'summarized' mode only; text output
  embedding:             { alias: oso/embedding, dim: 384, max_input_tokens: 512, prefix: {query: "query: ", passage: "passage: "} }
```

---

## 3. Repository Layout Created in Phase 0

```text
kg_boilerplate/
├── ROADMAP.md
├── docs/
│   ├── plans/phase-0-compatibility-spike.md      (this file)
│   ├── router-contract.md                        (WP1)
│   ├── compatibility-report.md                   (WP9: results table, OFFLINE + LIVE columns)
│   └── adr/ADR-011..ADR-020-*.md                 (WP9)
├── compat/
│   ├── versions.lock.yaml                        (pinned stack)
│   └── model-purposes.yaml                       (§2.3)
├── tools/
│   ├── mock_router/                              (WP2: kept, reused by P1–P6 tests)
│   └── verify_router/                            (WP8: live verification kit)
├── spikes/                                       (throwaway evidence code)
│   ├── pyproject.toml   (uv, Python 3.12)
│   ├── docker-compose.spike.yml (neo4j, postgres+pgvector)
│   ├── graphiti/  pgvector/  tokenizer/  auth/  networking/
│   └── results/   (JSON outputs consumed by the report)
├── .env.example
└── Makefile        (spike-up, spike-offline, verify-router, spike-report, spike-down)
```

---

## 4. Work Packages

Each package lists its checks with IDs, tags and pass criteria. The results go to `spikes/results/<id>.json` with the fields `{id, tag, status: pass|fail|skipped|deferred, evidence, notes, versions}`.

### WP0: Spike scaffold
- `spikes/pyproject.toml` (uv). Dependencies:
  - `graphiti-core` (latest stable at start, recorded exactly);
  - `neo4j`, `psycopg[binary]`, `pgvector`, `sqlalchemy`, `alembic`;
  - `pydantic` v2, `openai` (Python SDK), `tiktoken`, `httpx`;
  - `pyjwt` or `authlib`, `fastapi`, `uvicorn`, `pytest`, `pytest-asyncio`, `pyyaml`.
- `docker-compose.spike.yml`:
  - `neo4j:5.26-community` with APOC (confirm the minimum version Graphiti needs);
  - `pgvector/pgvector:pg17`;
  - `mock-router` (WP2);
  - all with health checks.
- Makefile targets: `spike-up`, `spike-offline` (runs every OFFLINE check), `spike-report`, `spike-down`.
- `.env.example`:
  - `OSO_ROUTER_BASE_URL=http://127.0.0.1:8780/v1`
  - `OSO_ROUTER_API_KEY=`
  - `OSO_ROUTER_MODE=mock|live`
  - `NEO4J_*`, `DATABASE_URL`
- **Pass:** `make spike-up` reaches healthy from a clean checkout.

### WP1: Router contract document (OFFLINE)
Write `docs/router-contract.md` from §2. It must include:
- the request and response shapes the platform uses;
- the error-code → platform-category map;
- the classification mapping and the `skipped_by_policy` rule;
- the metadata propagation rules;
- the purpose-to-alias map;
- the known gaps: no cost in the response, embeddings without fallback, the 512-token E5 limit, a single structured-output model on `oso/general`, dropped logprobs;
- the router-side change requests: an `oso/structured` alias, and optionally a second embedding deployment with the same dimension.

**Pass:** reviewed by the user. Every statement cites the router doc page or source file it came from.

### WP2: Mock router (OFFLINE, kept as a long-lived test tool)
A FastAPI app in `tools/mock_router/` that reproduces the router contract closely enough to catch integration bugs early:
- **Endpoints:**
  - `/v1/chat/completions`, `/v1/embeddings`, `/v1/models`, `/health`;
  - Bearer key check that returns `authentication_error`.
- **Faithful behaviours:**
  - strips unknown top-level fields, so a client that sends `logprobs` gets none back;
  - **capability gating**: when the request has a `response_format` other than text, the alias's eligible models are filtered by an alias→models→capabilities table copied from the seed, and an empty set returns `unsupported_capability`;
  - `data_classification: local_only` → `data_policy_violation`;
  - the error envelope with `x-gateway-request-id` and `Retry-After` on 429;
  - `routing.simulate_failure` and env-driven fault injection: 429, 500, timeout, provider_unavailable;
  - `model` echoes the alias, and usage is counted with tiktoken.
- **Content modes:**
  1. *Schema-faker:* for `json_schema`, return a minimal schema-valid object.
  2. *Fixture replay:* responses keyed by a hash of (alias, normalized messages), stored in `tools/mock_router/fixtures/`. The **live kit records these fixtures** (WP8), so offline tests later replay real router outputs.
  3. *Echo / text* for plain chat.
- **Deterministic embeddings:** 384-dim, unit-normalized vectors from a seeded hash of the text. Optionally a small local model behind a flag for realistic similarity: `intfloat/multilingual-e5-small` via sentence-transformers, which gives the **same dimension and model family**.
- **Pass:**
  - contract tests in `tools/mock_router/tests/` assert each behaviour;
  - the OpenAI Python SDK pointed at the mock works for chat, json_schema, the error paths and embeddings.

### WP3: Model gateway prototype (OFFLINE against the mock; LIVE-DEFERRED)
A thin prototype (`spikes/gateway/`) of the P1 `ModelGateway` that proves the design:
- `complete(purpose, messages, response_schema?, classification, correlation)` and `embed(purpose, texts, kind=query|passage)`.
- Purpose resolution comes from `compat/model-purposes.yaml`.
- Egress policy is checked before the call:
  - classification not allowed → `EgressDenied` → stage `skipped_by_policy`;
  - the call is never sent.
- Each call carries `x-request-id=correlation_id`, `metadata.application_id=kg-platform`, `metadata.task_type=extraction|summarization|…`, `metadata.data_classification` and `user=<hashed principal>`.
- Error mapping to the 4 categories in §2.1, with `Retry-After`-aware backoff.
- Embedding behaviour:
  - adds the E5 prefixes;
  - chunks to ≤512 tokens (and records truncation);
  - batches inputs;
  - asserts `len(vector)==384`.
- Metadata-only call log: purpose, alias, tokens, latency, router request ID, estimated cost from `compat/pricing.yaml`. No content.
- **Checks:**

| ID | Tag | Check | Pass |
|---|---|---|---|
| G-01 | OFFLINE | Chat, json_schema and embeddings round-trip via the mock | Typed results, usage recorded |
| G-02 | OFFLINE | `local_only` and denied classifications never reach the transport | No call made (asserted by a transport spy) |
| G-03 | OFFLINE | 429 with Retry-After, 500 and timeout are retried with bounded attempts; `unsupported_capability` is not retried | Matches the category map |
| G-04 | OFFLINE | Embedding chunking and prefixes, 384-dim assertion | Pass |
| G-05 | LIVE-DEFERRED | The same suite against the real router | Same outcomes; real usage recorded |

### WP4: Graphiti compatibility (mostly OFFLINE, extraction quality LIVE-DEFERRED)
Wire Graphiti with:
- its **OpenAI-compatible generic LLM client** (chat completions), with `base_url` set to the gateway or router;
- `model` set to `oso/general` and `small_model` set to `oso/general`;
- `OpenAIEmbedder` with `embedding_model=oso/embedding` and `embedding_dim=384`, wrapped to add the E5 prefixes;
- **no logprob cross-encoder**.

| ID | Tag | Check (blueprint decision) | Pass criteria |
|---|---|---|---|
| GR-01 | OFFLINE | Install and connect: `build_indices_and_constraints` on Neo4j 5.26 | Indices created; versions recorded (DEC-01) |
| GR-02 | OFFLINE | Graphiti's LLM client uses only `/v1/chat/completions` (an httpx spy against the mock); find which `response_format` it sends (`json_object` or `json_schema`) and whether it sends fields the router strips | Endpoint and payload shape documented. If it needs `/responses` or logprobs, choose a subclass or override. |
| GR-03 | OFFLINE | **Runtime ontology compiler (DEC-05):** generate Pydantic entity and edge types from the `06-reference/02` YAML (Person, Organization, Topic, WORKS_AT, CONSIDERING, KNOWS); check them against Graphiti's reserved field names and its entity/edge-type validation; pass them to `add_episode` (with mock schema-faker responses) | Types accepted. Collisions with reserved fields are detected and reported. **Fallback if this fails:** allowlisted static classes plus a validated mapping. |
| GR-04 | OFFLINE | **Namespacing (DEC-06):** two `group_id`s; check that every search API and Cypher query scopes by group; check the allowed `group_id` character set; decide between a shared DB with group_id and a DB per tenant | No cross-group results in search, node fetch or edge fetch. A recommendation recorded with isolation trade-offs. |
| GR-05 | OFFLINE | **Deterministic writes with no LLM (ADR-007):** create entities and edges by direct `EntityNode`/`EntityEdge` save with embeddings from the mock, and check whether `add_triplet` calls the LLM (transport spy) | A documented path that makes **zero** chat calls. If `add_triplet` calls the LLM, P2 uses the direct save path plus a platform-side projection writer. |
| GR-06 | OFFLINE | **Temporal mapping (DEC-12):** set `valid_at`/`invalid_at` on edges, backdate them (evt_002: occurred 2024-06-01, recorded 2024-06-10), then read back `created_at` and `expired_at` | A mapping table from Graphiti fields to platform fields (`valid_from/valid_to/recorded_at/superseded_at`), with any gaps a platform-side store must cover |
| GR-07 | OFFLINE | **Correcting edges (DEC-04):** invalidate an edge, edit its attributes, delete an edge, and check whether a search still returns it | Supported operations listed. Decision: native edit versus a platform correction projection (fact status in Postgres overriding Graphiti) |
| GR-08 | OFFLINE | **Deleting episodes and derivatives:** delete an episode (`remove_episode` or the equivalent); which entities and edges disappear, and which orphans remain; delete everything in a group | Enumerated derivative behaviour, the input for P8 deletion. Orphan-cleanup Cypher drafted. |
| GR-09 | OFFLINE | **Idempotency:** add the same episode twice, with the same and with different `uuid`/name | Documented. If Graphiti doesn't deduplicate, the ledger keys on `(workspace, source_key, stage)` (confirms the ING-01 design). |
| GR-10 | OFFLINE | **Search without a cross-encoder:** hybrid search recipes (RRF, MMR, node-distance) with 384-dim mock embeddings; scoped by group; `center_node_uuid` | Working recipe chosen; the logprob reranker confirmed unused |
| GR-11 | LIVE-DEFERRED | Ingest Sarah `evt_003` through the real router: entities and edges extracted with custom types, CONSIDERING tentative, no wrong WORKS_AT invalidation | Extraction output saved as fixtures. Calls, tokens and latency per episode recorded. |
| GR-12 | LIVE-DEFERRED | Through the router: which alias works for Graphiti's small and medium calls; is `oso/general` single-candidate fragile (simulate `primary_timeout`); does `oso/code` work as a fallback | Final purpose-to-alias map, or a confirmed request for `oso/structured` |
| GR-13 | LIVE-DEFERRED | Concurrency: the `SEMAPHORE_LIMIT` that stays under the key's RPM/TPM; behaviour on 429 | Recommended value recorded |

### WP5: pgvector and Postgres (OFFLINE)

| ID | Check | Pass |
|---|---|---|
| PV-01 | `vector(384)` column; exact cosine search with workspace and scope predicates **in the SQL** (prefilter) on 50k synthetic memories spread across 20 workspaces and many scopes | Results never leave the scope; latency baseline recorded |
| PV-02 | Row-level security: `SET LOCAL app.workspace_id` plus RLS policies on the memory tables; vector search through RLS | Cross-workspace rows invisible even with a known ID (start of SEC-01) |
| PV-03 | HNSW with `hnsw.iterative_scan` versus exact search under selective filters: recall@10 compared with exact | Recall numbers recorded. Confirms the **exact scoped search at MVP** default (DEC-08 / risk "tenant vector recall"). |
| PV-04 | Embedding version columns (`embedding_model_id`, `embedding_dim`, `embedding_version`) and a re-embed path if the router's embedding model changes | Migration sketch documented |

### WP6: Tokenizer and budget (OFFLINE; calibration LIVE-DEFERRED)
- **TK-01:** `o200k_base` versus `cl100k_base` versus a character heuristic on sample context packages. Choose the default and the margin.
- **TK-02 (LIVE):** compare against the router's `prompt_tokens` for the same text on gemini- and glm-backed aliases. Adjust the margin so that what the platform counts is ≥ the observed count in 100% of samples.
- **Pass:** a tokenizer ADR exists with the margin and the rule that the tokenizer ID is recorded in every trace.

### WP7: Auth and networking (OFFLINE)
- **AU-01 (DEC-03):**
  - dev auth uses platform-issued API keys: hashed, prefix displayed, workspace-bound principals;
  - optional short-lived JWT from a local signer;
  - an OIDC path is documented, not built;
  - library chosen (pyjwt or authlib).
- **NW-01:** the router binds **only to 127.0.0.1**, so check whether containers can reach it. Options:
  - (a) Docker Desktop `host.docker.internal` forwarding to the host loopback, tested against a stub on 127.0.0.1;
  - (b) run the router with `HOST=0.0.0.0`, firewalled;
  - (c) run api and worker on the host in dev.

  Record which works on macOS Docker Desktop and set the `OSO_ROUTER_BASE_URL` default per mode.
- **Pass:** decision recorded; the stub test passes.

### WP8: Live router verification kit (build now, run later)
`tools/verify_router/`, run with `make verify-router` while `OSO_ROUTER_MODE=live` and the router is running locally. It runs these checks in order and writes `spikes/results/live-*.json` plus an appendix to the report:

| ID | Check |
|---|---|
| LV-01 | `GET /health`; `GET /v1/models` contains the 9 aliases and their limits |
| LV-02 | Auth: a bad key returns `authentication_error`; the response includes `x-gateway-request-id` |
| LV-03 | Plain chat on each purpose alias |
| LV-04 | `json_schema` on `oso/general` and `oso/code` succeeds; on `oso/fast` it returns `unsupported_capability` (confirms §2.1) |
| LV-05 | Embeddings: dimension = 384; batching; long input > 512 tokens shows the truncation behaviour |
| LV-06 | Classifications: `local_only` → `data_policy_violation`; record which aliases accept `confidential` and `restricted` |
| LV-07 | Fault simulation: `routing.simulate_failure` covers fallback, `fallback:false` and the error mapping |
| LV-08 | Metadata and `x-request-id` show up in the router request log; the user checks this in the Admin UI |
| LV-09 | G-05, GR-11, GR-12, GR-13 and TK-02 |
| LV-10 | **Fixture recording:** saves the real responses for the Sarah episodes into `tools/mock_router/fixtures/` |

- The kit never prints the key.
- Content in recorded fixtures is synthetic Sarah data only.

### WP9: ADRs, pin file and report

**ADRs to write** (status Accepted, or Provisional when they depend on the live run):

| ADR | Topic |
|---|---|
| ADR-011 | Pinned stack (versions.lock.yaml) |
| ADR-012 | Model gateway and router contract (purposes, error categories, egress, `skipped_by_policy`) |
| ADR-013 | Embeddings: 384-dim, E5 prefixes, chunking, re-embed strategy, single-deployment risk |
| ADR-014 | Ontology compiler approach (dynamic versus allowlisted) |
| ADR-015 | Graph tenancy (group_id versus DB per tenant) and the namespace mapping |
| ADR-016 | Deterministic write path into Neo4j |
| ADR-017 | Temporal field mapping and the bitemporal scope for the MVP |
| ADR-018 | Correction and deletion strategy for Graphiti derivatives |
| ADR-019 | Vector search: exact scoped search plus RLS at MVP |
| ADR-020 | Tokenizer and budget accounting; dev auth; dev networking |

**Other deliverables:**
- `compat/versions.lock.yaml`: exact versions and image digests.
- `docs/compatibility-report.md`: one row per check (ID, tag, status, evidence link) and a "decisions unblocked for P1–P5" section.
- Updates to ROADMAP.md: the P0 open items are answered or marked pending live verification.

---

## 5. Sequencing

```
WP0 ─► WP1 ─► WP2 ─► WP3 ─┬─► WP4 ─┐
              WP0 ─► WP5 ─┤        ├─► WP9 (provisional) ─► [user runs WP8 live] ─► WP9 (final)
              WP0 ─► WP6 ─┤        │
              WP0 ─► WP7 ─┘   WP8 (built after WP3/WP4) ┘
```

WP5, WP6 and WP7 run alongside WP2–WP4.

## 6. Exit Criteria

**Phase 0 is done (offline)** when:
- [ ] `make spike-up && make spike-offline` passes from a clean checkout, and every OFFLINE check is `pass` (or `fail` with a chosen fallback documented).
- [ ] The mock router contract tests pass, and the OpenAI SDK works against it.
- [ ] `docs/router-contract.md`, the ADRs (011–020), `versions.lock.yaml`, `model-purposes.yaml` and `compatibility-report.md` exist.
- [ ] `make verify-router` exists. With `OSO_ROUTER_MODE=live` and no reachable router, it fails cleanly with a "router not reachable at …" message.
- [ ] Router change requests are written down for the user (the `oso/structured` alias; optionally a second 384-dim embedding deployment).

**Phase 0 is finalised (live)** when the user runs `make verify-router` against the local router, the LIVE-DEFERRED rows are filled in, the provisional ADRs are accepted or revised, and the fixtures are recorded. **P1 can start after the offline exit.** Only P4 (Graphiti extraction) and P5 (memory extraction) *gate* on the live finalisation.

## 7. Risks Specific to Phase 0

| Risk | Mitigation |
|---|---|
| Graphiti's generic client sends `json_object` with the schema in the prompt, and output quality differs by model | GR-11/GR-12 live fixtures; the extraction alias is limited to structured-output models |
| `oso/general` has only one structured-output model, so extraction has no fallback | `oso/structured` alias request; `oso/code` as the configured fallback purpose |
| The E5-small embedding model is weak or English-biased for entity matching | Measure recall in GR-10 and LV-05. ADR-013 keeps an embedding version so the platform can re-embed later. |
| Embeddings are single-region (us-central1) with no fallback | Worker retry plus a backlog state (`memory_failed` / `graph_failed` stays retryable) |
| The mock drifts from the real router | Contract tests are derived from the router's zod schemas and seed. LV-10 fixtures replace synthetic responses. Re-run `verify-router` after router upgrades. |
| Graphiti API changes between minor versions | Exact pin plus the adapter boundary (`packages/graph`) from P1 onward |

## 8. Out of Scope for Phase 0
- Production code in `apps/` or `packages/` (that starts in P1).
- Any UI.
- Changes to the router repository. These are only *requested* via the contract document.
