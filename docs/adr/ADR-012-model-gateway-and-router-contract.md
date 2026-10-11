# ADR-012: Model gateway and router contract

**Status:** Provisional (pending live router verification: G-05, GR-11, GR-12, GR-13, LV-01..LV-10)  **Decision refs:** DEC-02, SEC-06, ING-06
**Evidence:** G-01..G-04, MR-01, GR-02, GR-05 · `docs/router-contract.md`, `compat/model-purposes.yaml`, `tools/mock_router`, `spikes/gateway`

## Context
All LLM and embedding traffic goes through the user's Oso Model Router (OpenAI-compatible, chooses the physical model, ledger-side cost). The router's actual behaviour differs from vanilla OpenAI in ways that break common libraries.

## Decision
1. **One `ModelGateway`** is the only code that talks to the router. Callers name a **purpose** (`graph_extraction`, `memory_extraction`, `context_compression`, `embedding`), never a model or alias. `compat/model-purposes.yaml` maps purposes to aliases and is the only place aliases appear.
2. **Chat completions only.** The router has no `/v1/responses`; Graphiti's default `OpenAIClient` (Responses API) fails against it (GR-02), so Graphiti is constructed with `OpenAIGenericClient` in `json_schema` mode. Everything it sends (`model, messages, temperature, max_tokens, response_format`) survives the router's schema.
3. **Never rely on dropped fields:** `logprobs`, `top_logprobs`, `logit_bias`, `seed`, `n` are stripped silently, so logprob rerankers and seeded determinism are unavailable (ADR-019 search recipes avoid the cross-encoder).
4. **Egress is checked before the call.** Classes in `egress.denied_classifications` (`local_only`) raise `EgressDenied`; the call is never sent (asserted with a transport spy, G-02) and the stage becomes the terminal, non-error **`skipped_by_policy`**. Router policy errors map to the same state.
5. **Error categories:** retryable (`rate_limited, provider_unavailable, timeout, unknown_provider_error, routing_failed`) back off exponentially with `Retry-After` taking precedence and a bounded attempt count (default 4); policy-terminal → `skipped_by_policy`; config-terminal → operator alert, no retry; budget → pause the queue. An exhausted retryable primary falls back to the purpose's `fallback_alias` once; budget/policy/config errors never fall back (G-03).
6. **Metadata for joinability, never content:** `x-request-id` = platform `correlation_id` (must be a UUID or the router ignores it), `application_id=kg-platform`, `task_type`, `data_classification`, and HMACs of principal and workspace. The call log is metadata-only (purpose, alias, tokens, latency, router request id, estimated cost, tokenizer).
7. **Cost is estimated locally** from `compat/pricing.yaml` (alias → primary candidate; a floor when fallback fires) because the response carries no cost; the router ledger stays authoritative.
8. **Structured output:** `oso/fast` has no eligible model; `oso/general` has exactly one (gemini-3.5-flash). Initial map: `graph_extraction`/`memory_extraction` → `oso/general`, fallback `oso/code`. Change request to the router admin: an **`oso/structured`** alias (glm-5.2 → gemini-3.5-flash → gemini-3.8-flash → gemini-3.1-pro-preview).
9. **Cost of Graphiti (offline call counts through the mock, Sarah episodes with the compiled ontology):** evt_001 = 4 chat calls, evt_002 = 5, evt_003 = 8, each with roughly 6–8 embedding calls. Chat calls grow with existing edges (dedupe/contradiction and timestamp calls). Token cost and latency per episode are GR-11.
10. **Use a non-admin key per service** (`kg-api`, `kg-worker`): admin keys expose provider/model headers and defeat attribution.

## Consequences
The mock router is a first-class test tool (49 contract tests) so P1–P6 never need the live router; the live kit turns this ADR from Provisional to Accepted.

## Live follow-up (what changes this ADR)
LV-04/GR-12 decide the alias map or the `oso/structured` request; LV-06 decides whether platform keys need a higher `maxClassification`; GR-13 sets `SEMAPHORE_LIMIT`; GR-11 sets expected cost per episode.
