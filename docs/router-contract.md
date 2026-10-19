# Router Contract (Phase 0, WP1)

**Status:** Draft for user review. Statements are verified against the router's source unless tagged `[docs]` (documentation pages only) or `[unverified]` (to be settled by `make verify-router`).
**Router repo:** `~/Documents/oso_model_router` (cited below as `router:`). Docs pages are in `web/src/features/docs/content/*.ts` (cited as `docs:<file>`).
**Consumers:** every phase from P1 on codes against this document. The mock router (`tools/mock_router`) reproduces it, and the live kit (`tools/verify_router`) checks it.

---

## 1. Transport

| Item | Contract | Source |
|---|---|---|
| Base URL | **`https://models.oso.group/v1`** (decision 2026-10-19: the platform connects to the router only through its public HTTPS address, never to a local port) | owner decision; `docs:apiReference.ts` |
| Auth | `Authorization: Bearer <key>`; keys carry scopes (`chat`, ...) and a per-environment binding | `router:scripts/seed-logical-router.ts` (api_clients), `docs:apiReference.ts` |
| Key revocation | Effective within **60 s** (TTL cache), sooner when the cache is busted | `router:src/auth/PostgresApiClientStore.ts` (`CACHE_TTL_MS = 60_000`) |
| Endpoints used | `POST /v1/chat/completions`, `POST /v1/embeddings`, `GET /v1/models`, `GET /health` (no auth) | `docs:apiReference.ts` |
| Endpoints NOT available | `/v1/responses`. Any client that uses the OpenAI Responses API or `beta.chat.completions.parse` **will fail** | `router:src/api/` has only `chat`, `embeddings`, `mcp`, `playground` [verify LV-01] |
| MCP | `POST /mcp` is read-only discovery (list models, health). The platform does not use it | `docs:integration.ts` (MCP Server) |

## 2. Request shape (chat)

Validated by zod (`router:src/api/chat/schema.ts`). **Unknown top-level fields are stripped silently**, not rejected.

Fields the platform may send: `model`, `messages`, `temperature`, `top_p`, `max_tokens`, `max_completion_tokens`, `stop`, `tools`, `tool_choice`, `response_format`, `stream`, `user`, `metadata`, `routing`.

**Silently dropped (not in the schema):** `logprobs`, `top_logprobs`, `logit_bias`, `seed`, `n`, `frequency_penalty`, `presence_penalty`, `parallel_tool_calls`. A client that depends on them gets no error and no effect.

- `messages[].role` ∈ `system | user | assistant | tool`. `system` content must be a **string**. The `developer` role is **not** accepted, so a client that sends it gets `invalid_request` (400). `[verify LV-03]`
- `response_format` ∈ `{type:"text"} | {type:"json_object"} | {type:"json_schema", json_schema:{name, schema, strict?}}`.
- **Capability derivation** (`router:src/models/requirements.ts`): any `response_format` other than `text` adds the required capability `structured_output`; `tools` adds `tools`; image parts add `vision`; `stream` adds `streaming`.
- `routing` = `{privacy?, max_cost_usd?, prefer?:"local", data_classification?, task_type?, fallback?:boolean, simulate_failure?}`. `routing.task_type` wins over `metadata.task_type`; explicit `routing.data_classification` wins over `metadata.data_classification` (`docs:routing.ts`, `schema.ts` comments).
- `metadata` = `{application_id, user_id, conversation_id, request_id, task_type, agent, data_classification, simulate_failure, ...passthrough}`.
- `user`: recorded on the router ledger as the request's user.
- `x-request-id` request header: reused as the router request ID **only if it is a valid UUID**; otherwise the router mints one (`router:src/observability/logger.ts`, `genRequestId`). **The platform's `correlation_id` must therefore be a UUID** (UUIDv7 recommended) for the router ledger to join to platform traces.

## 3. Response shape

### Chat (`router:src/api/chat/schema.ts`, `chatCompletionResponseSchema`)
```json
{"id":"chatcmpl_...","object":"chat.completion","created":1760000000,
 "model":"oso/general",
 "choices":[{"index":0,"message":{"role":"assistant","content":"..."},"finish_reason":"stop"}],
 "usage":{"prompt_tokens":12,"completion_tokens":7,"total_tokens":19}}
```
- `model` echoes the **alias**, never the physical model (comment `R§32` in schema.ts).
- `usage` is declared optional in the schema, so the platform must tolerate its absence and fall back to its own estimate (and flag `usage_estimated`).
- `routing_decision` appears only when enabled (`x-include-routing-decision: true` header or playground source). The platform does not rely on it.
- **There is no cost field.** Cost is recorded only in the router ledger (`docs:operations.ts`, Cost Metadata).

### Headers
- `x-gateway-request-id`: set on successful chat and embeddings responses (`router:src/api/chat/route.ts:440`, `embeddings/route.ts:174`).
- `x-request-id`: set on **every** response by an `onSend` hook (`router:src/app.ts:87`). On errors this is the only router ID header, and `error.request_id` in the body carries the same value.
- `x-gateway-provider` / `x-gateway-model`: **only for keys with the `admin` scope**. The platform's keys must not have that scope, so the physical model is unknown to it. It records `alias + router config version` instead.

### Embeddings (`docs:apiReference.ts`)
```json
{"object":"list","data":[{"object":"embedding","index":0,"embedding":[...]}],
 "model":"oso/embedding","usage":{"prompt_tokens":9,"total_tokens":9}}
```
`input` is a string or a string array. No `dimensions` and no `encoding_format` field exist in the schema (`router:src/api/embeddings/schema.ts`), so the SDK's `encoding_format` (base64 by default in some SDK versions) is dropped. `[verify LV-05]` that the response is plain float arrays.

### Models / health
- `GET /v1/models` lists **aliases only**, with `capabilities`, `context_length`, `max_output_tokens` (the minimum across enabled targets) (`docs:apiReference.ts`).
- `GET /health` → `{"status":"ok"}`, unauthenticated. `GET /health/providers` shows provider status.

## 4. Error envelope and platform categories

Envelope (`router:src/errors/http.ts`):
```json
{"error":{"message":"...","type":"<code>","code":"<code>","request_id":"<uuid>","details":{},"routing_decision":{}}}
```
`429` responses carry `Retry-After: <seconds>` (`docs:governance.ts`, Usage & Rate Limits).

| Router code | HTTP | Platform category | Platform behaviour |
|---|---|---|---|
| `rate_limited` | 429 | **retryable** | Backoff honouring `Retry-After`; bounded attempts |
| `provider_unavailable` | 503 | **retryable** | Backoff |
| `timeout` | 504 | **retryable** | Backoff |
| `unknown_provider_error` | 502 | **retryable** | Backoff, then dead-letter |
| `routing_failed` | 503 | **retryable** | Backoff (every candidate failed) |
| `budget_exceeded` | 429 | **budget** | Pause the queue with long backoff, raise an operator alert |
| `cost_limit_exceeded` | 422 | **budget** | Per-request cost ceiling. Not retried unchanged |
| `data_policy_violation` | 422 | **policy-terminal** | Stage → `skipped_by_policy`; never retried |
| `privacy_policy_violation` | 422 | **policy-terminal** | As above |
| `classification_not_permitted` | 403 | **policy-terminal** | As above (the key's `max_classification` is too low) |
| `forbidden` | 403 | **policy-terminal** | As above |
| `authentication_error` | 401 | **config-terminal** | Alert operator; do not retry |
| `model_not_found` | 404 | **config-terminal** | Alert operator (alias missing or not permitted) |
| `unsupported_capability` | 422 | **config-terminal** | Alert operator (e.g. `json_schema` on `oso/fast`) |
| `context_too_large` | 400 | **config-terminal** | Alert; platform must chunk/shrink input. (The router itself treats this as fall-through to the next candidate.) |
| `invalid_request` | 400 | **config-terminal** | Bug in the platform; never retried |
| other admin codes (`not_found`, `conflict`, `confirmation_required`, `validation_required`) | 4xx | **config-terminal** | Not expected on `/v1/*` |

Source for codes and statuses: `router:src/errors/codes.ts` (`GATEWAY_ERROR_CODES`, `HTTP_STATUS_BY_CODE`, `RETRYABLE_CODES`). `RETRYABLE_CODES` there means "the router moves to its next fallback candidate", which is a different notion from the platform's categories above.

## 5. Structured output reality

Capability data from `router:scripts/seed-logical-router.ts`:

- Deployments with `structured_output`: **glm-5.2, gemini-3.8-flash, gemini-3.5-flash, gemini-3.1-pro-preview, gemini-2.5-pro**.
- Alias chains (primary first) and which candidates qualify:

| Alias | Chain | `structured_output`-capable candidates |
|---|---|---|
| `oso/fast` | gpt-oss-20b, gemini-3.5-flash-lite, claude-haiku-4-5 | **none** → `unsupported_capability` |
| `oso/general` | gpt-oss-120b, gemini-3.5-flash, claude-sonnet-5-5 | **1** (gemini-3.5-flash). No fallback for extraction |
| `oso/reasoning` | kimi-k2-thinking, gpt-oss-120b, gemini-3.1-pro-preview, claude-opus-5-5 | 1 (gemini-3.1-pro-preview) |
| `oso/code` | qwen3-coder, glm-5.2, gemini-3.8-flash, claude-sonnet-5-5 | 2 (glm-5.2, gemini-3.8-flash) |
| `oso/code-high` | glm-5.2, qwen3-coder, claude-sonnet-5-5, gemini-3.1-pro-preview | 2 (glm-5.2, gemini-3.1-pro-preview) |
| `oso/vision` | gemma-4-26b, gemini-3.5-flash, claude-sonnet-5-5 | 1 |
| `oso/general-vision` | gemma-4-26b, gemini-3.5-flash | 1 |
| `oso/frontier` | glm-5.2, kimi-k2-thinking, claude-fable-5-1, claude-opus-5-5, gemini-3.1-pro-preview, gpt-6.1-sol | 2 (glm-5.2, gemini-3.1-pro-preview) |
| `oso/embedding` | multilingual-e5 | n/a |

Consequences: (1) structured extraction goes through `oso/general` today with a single eligible model; (2) `oso/code` is the configured fallback *purpose*; (3) the best fix is router-side (§9).

## 6. Embeddings

- `oso/embedding` → `multilingual-e5` → `intfloat/multilingual-e5-small-maas` on Vertex, region `us-central1` only, **no fallback**, $0.015 per million tokens (seed).
- `intfloat/multilingual-e5-small` produces **384-dimension** vectors `[model card; verify LV-05]`. The platform fixes `embedding_dim = 384`, asserts it on every response, and stores `embedding_model_id`, `embedding_dim`, `embedding_version` beside every vector (ADR-013).
- E5 expects `query: ` and `passage: ` prefixes and has a **512-token** input limit. The seed advertises `contextWindow: 8192`, which is the deployment figure, not the model's. The gateway adds the prefixes and chunks to ≤512 tokens.
- A single deployment with no fallback means embeddings are a **single point of failure for writes**. The worker retries, and a backlog state (`memory_failed` / `graph_failed`) stays retryable.

## 7. Data classification and egress

Router tiers (`router:src/routing/types.ts`): `public < internal < confidential < restricted < local_only`. Router `privacy` (a ceiling) ∈ `local_only | open_models_only | cloud_allowed | closed_allowed`. The default environment classification is `internal`, and the default `privacy` is `closed_allowed` (seed router settings). A key's `maxClassification` caps what it may send (the seeded demo client is `internal`, so **the platform's keys need `confidential`/`restricted` raised explicitly** by the router admin).

| Platform class (blueprint) | Router `data_classification` | Notes |
|---|---|---|
| public | `public` | |
| internal | `internal` | |
| confidential | `confidential` | Router restricts to zero-retention providers only `[docs:governance.ts]` |
| restricted | `restricted` | Router restricts to sovereign providers only `[docs:governance.ts]`. May have no eligible candidate → `data_policy_violation`/`routing_failed` `[verify LV-06]` |
| (no egress) | `local_only` | **Rejected** (`data_policy_violation`): the standard setup has no local deployment `[docs:governance.ts]` |

**Platform rule (`skipped_by_policy`):** before any call, the gateway checks the data class against `compat/model-purposes.yaml → egress`. A denied class raises `EgressDenied` and **the request is never sent**. The pipeline stage becomes `skipped_by_policy`, a terminal, non-error state that is not retried. The same state is used for the router's policy-terminal errors (§4).

## 8. Metadata propagation (never raw content)

| Router field | Platform value |
|---|---|
| `x-request-id` header | `correlation_id` (UUID) |
| `metadata.application_id` | `kg-platform` |
| `metadata.request_id` | `correlation_id` (duplicate for the ledger's field) |
| `metadata.task_type` / `routing.task_type` | `extraction | summarization | embedding | ...` |
| `metadata.data_classification` / `routing.data_classification` | the item's class |
| `metadata.agent` | service name (`kg-api` or `kg-worker`) |
| `metadata.user_id`, `user` | **HMAC of the principal ID** (never a name or email) |
| `metadata.conversation_id` | HMAC of `workspace_id` |

Never send raw subject names or emails in metadata. Content goes only in `messages`/`input`, and only after the egress check.

## 9. Fallback, limits, token accounting

- Fallback is automatic on retryable provider failures. `routing.fallback:false` stops after the first candidate. Streams commit to a candidate at the first byte (`docs:integration.ts`). The platform keeps fallback **on** for extraction.
- Limits are client RPM/TPM, alias RPM and provider concurrency. Budgets can warn, throttle or block (`docs:governance.ts`). Graphiti fans out many LLM calls per episode, so the platform sets `SEMAPHORE_LIMIT` plus a client-side token bucket under the key's limits (value fixed by GR-13).
- **Token counting:** the router chooses the physical model, so the platform cannot use that model's tokenizer. It counts its own output with `tiktoken o200k_base` plus a safety margin (default 10%, calibrated by TK-02) and records the tokenizer ID in every trace (ADR-020).

## 10. Known gaps

1. No cost in responses (ledger-only) → local price table, estimates labelled as such.
2. Embeddings: no fallback, one region, 512-token model limit vs. advertised 8192.
3. Only one `structured_output` model behind `oso/general`; none behind `oso/fast`.
4. `logprobs` and friends are dropped, so Graphiti's default `OpenAIRerankerClient` cannot work.
5. Physical model hidden from non-admin keys → record alias + config version.
6. `local_only` unusable until a local deployment exists.
7. Seeded client key is capped at `internal`.

## 11. Router-side change requests (for the user to action; this repo does not change the router)

1. **Create an `oso/structured` alias** with chain: glm-5.2 → gemini-3.5-flash → gemini-3.8-flash → gemini-3.1-pro-preview (all `structured_output`-capable). Then `graph_extraction*` and `memory_extraction` point at it and `fallback_alias` is dropped.
2. **(Optional) A second 384-dim embedding deployment** (same model family) as a fallback for `oso/embedding`.
3. **Issue two non-admin keys**, `kg-api` and `kg-worker`, scope `chat`, `maxClassification: restricted` (or the highest the user is comfortable with).
4. **(Optional) Return a cost field or header** so the platform need not estimate.

## 12. Checked against the hosted documentation and this machine's router (2026-10-19)
- **Documentation:** `https://models.oso.group/ui/docs/` (readable without a login; the live alias and capability tables on it are empty without an admin session). Read: overview, environments, authentication, request and response contracts, model aliases, structured output, embeddings, data classifications, usage limits, changelog, API versions.
- **Matches this contract:** the nine `oso/*` aliases and every alias chain (compared programmatically against `tools/mock_router/catalog.json`: zero differences); `Authorization: Bearer`, keys scoped per environment; `POST /v1/chat/completions` and `POST /v1/embeddings` (`oso/embedding` or `multilingual-e5`, OpenAI-style list, `usage.prompt_tokens`/`total_tokens`); `response_format.json_schema` filters out candidates without structured-output support; `routing.data_classification`, five tiers, `local_only` = local deployments only; 429 with `Retry-After`; `/health` without auth. Changelog: stable v1.0.0 (2026-09-25); an unreleased note says embeddings reference and MCP guide were added and Jev decisions removed. No breaking change for this platform.
- **Not confirmable from the docs:** which models advertise `structured_output` (this contract's list is from the router source), the embedding dimension (384 assumed), whether a cost field is returned, and the key's `maxClassification`. These need a key: `make verify-router` (LV-01..10).
- **Decision (2026-10-19): the router is a standalone service and the platform reaches it only through `https://models.oso.group/v1`**, from host-run tools and from containers alike. No local port, no `host.docker.internal`, no forwarder, no shared Docker network. (A local container named `model-gateway` also answers on `127.0.0.1:8780` with the same health body and 401; the platform deliberately does not use it.)
- **Reachability, tested without a key:** `GET /health` returns 200 and `GET /v1/models` returns 401 "Authorization header is required" from the host, and from the api and worker containers.
- **Cloudflare blocks one client signature.** The public hostname sits behind Cloudflare, which answers 403 (`error code: 1010`) to the default `Python-urllib` user agent. `httpx` and the OpenAI SDK (the clients the platform uses; Graphiti uses the SDK) are accepted, as is `curl`. Do not call the router with `urllib` or `requests`-style defaults in scripts; set a `User-Agent`.
- **Keys are per environment** (docs: environments). Use a key issued for the environment this hostname serves.
- **Consequence for deployment:** every model call, and the data in it, crosses the public internet to Cloudflare over TLS. That replaces the plain-HTTP-on-a-private-network assumption in threat finding F-07; the egress policy and per-workspace denies still decide what may be sent. The first live run (`verify-router`) is where classification handling is confirmed.

### 12.1 What `make verify-router` found against `https://models.oso.group` (2026-10-19, key capped at `internal`)
| Check | Result | Note |
|---|---|---|
| LV-01 aliases and `/v1/models` | pass | all nine aliases present; the health body also carries `version` and `gitSha` (the check was too strict and was relaxed) |
| LV-02, LV-03, LV-05, LV-07, G-05, GR-13 | pass | usage and request id round trip; embeddings are 384-dimensional; failure simulation behaves; 16 parallel requests with no 429 (recommended Graphiti `SEMAPHORE_LIMIT` 8) |
| LV-04 / GR-12 structured output | pass for `oso/code` and `oso/code-high` (and `oso/frontier`) | **`oso/general` and `oso/fast` answer 422 `unsupported_capability`**; `oso/reasoning` was inconsistent (invalid JSON once, 503 once). Hence every structured purpose in `compat/model-purposes.yaml` uses `oso/code` with fallback `oso/code-high` |
| LV-06 classification matrix | fail (precondition) | the key may send `internal` only: `confidential`, `restricted` and `local_only` answer 403 `classification_not_permitted`, so the matrix cannot be evaluated. Raise the key's permitted classifications in the router admin (Access > API Credentials) when confidential data is to be processed |
| LV-08 request log | manual | search the request id the run prints in the router's Operations > Request Logs; expect `application_id=kg-platform` |
| LV-09 GR-11 extraction quality | **fail (quality, not plumbing)** | real extraction through `oso/code` runs without errors, types Sarah as Person and Acme as Organization and closes the first job at 2024-06, but did **not** extract the CONSIDERING edge and left more than one active WORKS_AT. This spike feeds employment text to the model, which the platform does not (employment arrives by command); the real measurement is `make eval-extraction-live` in the live session. Cost and speed of the three episodes: 4, 5 and 8 chat calls, 5.0k, 5.8k and 8.5k prompt tokens, 1.0k, 1.9k and 5.0k completion tokens, 50 s, 72 s and 220 s |
| LV-09 TK-02 token calibration | flaky | `oso/general` has a long latency tail (a 16-token reply took 36-52 s twice); the test now retries once with a 240 s timeout |
| LV-10 fixture recording | fail | the recorder script needs the same Graphiti run as GR-11; it reruns with it |

**Reliability notes:** a request occasionally answers 503 `routing_failed` / "No available target" for a few seconds (seen on `oso/embedding` once and `oso/reasoning` once); later 20 of 20 embedding calls and 32 of 32 chat calls succeeded. The gateway retries these. Expect real-model extraction to be slow: an episode with three entities took over 3 minutes of model time, so size `WORKER_CONCURRENCY` and the freshness SLO from the live baseline, not the modelled one.
