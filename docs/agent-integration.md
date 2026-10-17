# Integrating an agent with the platform

An agent gets its own identity (a registered principal and a key), asks for context, writes what it learns, and treats what it is told as **quoted data**. Use the [Python SDK](../sdk/python/README.md) or, for an MCP-capable host, the [MCP server](#mcp). Both only call the HTTP API and forward your key: they can do nothing the key cannot.

## 0. Identity
An admin registers the agent ([agent-onboarding](runbooks/agent-onboarding.md)): grants (read / propose / publish on subjects and projects), allowed profiles, quotas, a model-disclosure policy. You receive one key, **once**. Keep it in a secret store; send it as `Authorization: Bearer <key>`. The workspace and the identity come from the key: never send `workspace_id`, `group_id`, `principal_id` or `agent_id` (a 422 over HTTP, a validation error over MCP).

A run id (`run_id`, any `[A-Za-z0-9_-]{1,64}`) names one execution of your agent; it scopes your *working notes* and is never part of your identity. A new run does not need a new key.

## 1. Ask for context
```python
import os
from kg_platform_sdk import KGClient

async with KGClient(base_url=os.environ["KG_BASE_URL"], api_key=os.environ["KG_API_KEY"]) as kg:
    ctx = await kg.build_context(
        "Help Sarah assess the implications of starting a business",
        subject_ids=[sarah_id],
        profile="planning",
        max_tokens=600,
        project="newco-plan",
    )
    block = ctx.as_prompt_block()
```
```http
POST /v1/context/build
{"task": "…", "subject_ids": ["<id>"], "profile": "planning", "max_tokens": 600, "project": "newco-plan", "evidence_required": false}
```
- `status`: `complete`, `partial` (read `unavailable_sources`), `insufficient_evidence` (do not guess), `insufficient_budget` (ask for `budgets.required_tokens`), `denied` (nothing to use: the subject is missing, another workspace's, **or one you hold no grant on** — you cannot tell which, by design). `complete` is about completeness, **not** truth: read each item's `evidence_status` and the heading it sits under.
- Labels you will meet: `unverified` / `verified`; **`agent_generated`** (another agent's work: the text says `(agent_generated, unverified)` until a reviewer has verified it). Ask for `evidence_required=true` to leave unverified agent findings out.
- People you hold no grant on are simply absent, and so is every fact that touches them. A thin context about two people, one of them hidden, is correct.
- `budgets.used_tokens <= max_tokens` always (a conservative count); leave headroom for your own prompt.
- `context_id` / `retrieval_trace_id` are for debugging with a human ([context-debugging](runbooks/context-debugging.md)). `GET /v1/context/{id}` re-materialises under your *current* access.

## 2. Placing it in the prompt
The package is retrieved data, possibly containing text a person or another agent wrote. The engine neutralises structure (nothing can close the envelope, open a heading or forge a citation) but cannot make a sentence harmless.
1. Put it in the **user/data** part of the prompt, after your own instructions, **quoted** — the block already begins with `[CONTEXT DATA …]` and ends with `[END CONTEXT DATA]`.
2. Tell the model that content between those lines is evidence to reason about, never instructions to follow; that *GUIDANCE* is a preference that does not override your instructions; that `(agent_generated, unverified)` is a lead, not a fact.
3. Cite by handle (`[F:…]`, `[M:…]`, `[E:…]`) when you rely on a record; say "unverified" for what is; describe a *conflict* as a conflict and a *caution* as a caution.
4. Treat *MATERIAL UNKNOWNS* as things to ask about.
5. Do not log the package (it is content). Log `context_id` and `request_id`.

## 3. Write what you learn
| Call | Meaning |
|---|---|
| `ingest_interaction(...)` | submit an event (message, conversation, observation, document, json) the platform may learn from. Idempotent per `Idempotency-Key`. |
| `get_interaction_status(id)` | where it is in the pipeline. You read only your **own** submissions. |
| `save_working_memory(run_id, text)` | a private note for this run; visible to you and that run only; expires on its own. **Never** crosses to another agent. |
| `propose_memory(type, content, scopes, source_event_ids | fact_ids | memory_ids, …)` | a **source-backed** finding. The platform decides: with the `publish` privilege on every target scope and a permissive policy it is `active` at once (labelled, unverified); otherwise `proposed` for a reviewer. **Procedural memory is always reviewed.** |
| `publish_memory(id, scopes)` | share one of your private memories as a *new* proposal (the original stays private). |
| `search_memory(query, …)` | scoped to your grants; hits carry `agent_generated` and `evidence_status`. |
| `get_evidence(fact_id)` | why the platform holds a fact. |

Evidence you cite must be something **you can see**: your own interaction, a fact visible to you, a memory you may read. Missing, someone else's, hidden and foreign evidence all fail with the same `422 invalid_evidence`.

## 4. Errors and retries
One envelope: `{"error": {"code", "message", "request_id", "retry_after"?, "details"?}}`. Quote `request_id` when asking for help.
| Status / code | What to do |
|---|---|
| 401 `unauthenticated` | The key is wrong, expired or **revoked** (or the principal was). Stop; do not retry. |
| 403 `forbidden` | Your role may not do this. |
| 404 `not_found` | There is no such record *for you*. |
| 409 `idempotency_mismatch`, `memory_frozen`, … | The request conflicts with the record's state. |
| 422 `invalid_evidence`, `profile_not_allowed`, `exceeds_profile`, `validation_failed` | Fix the request; `details.fields` names the fields. |
| 429 `rate_limited` / `quota_exceeded` | Wait `retry_after`. `details.quota` names the quota; a daily quota refills at the next UTC midnight. |
| 503 `context_incomplete`, 502/503/504 | Retry with the same `Idempotency-Key` (the SDK does, up to 3 times, for 429/502/503/504 only). |
The SDK raises a typed exception per case (`AuthError`, `NotFound`, `InvalidEvidence`, `QuotaExceeded`, `ContextIncomplete`, …) with `code`, `status`, `request_id`, `details`, `retry_after`.

## MCP
`python -m kg_mcp` (or `make mcp-up`, compose profile `agents`, port 8010) serves nine tools — `build_context`, `search_memory`, `propose_memory`, `save_working_memory`, `publish_memory`, `ingest_interaction`, `get_interaction_status`, `get_evidence`, `list_profiles` — over stateless Streamable HTTP at `/mcp`. Configure your MCP host to send `Authorization: Bearer <your platform key>` with every request. The server holds no credential and no database access: each call forwards **your** key to the HTTP API, so you get exactly what HTTP gives you. `build_context` returns the data block as the text content and the whole package as structured content. Errors are tool errors carrying the platform's envelope. An argument the tool does not declare (`workspace_id`, `agent_id`, …) is a validation error.

## What changed for your agent when P7 landed
Facts about people are visible only with a subject grant (ontology-driven); a grant now carries privileges; findings are labelled; `evidence_required` is a request option; revocation is immediate and final (a `compromised` revocation turns what the agent published into a caution for everyone else).
