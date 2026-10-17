# ADR-040: The SDK and the MCP server are clients of the HTTP contract

**Status:** Accepted  **Decision refs:** AGT-03, SDK/MCP authorisation parity, phase-7 plan §5.7–5.8, T10, decisions 7, 8  **Evidence:** `tests/contract/test_sdk.py`, `tests/contract/test_mcp.py`, `sdk/python/tests`, import-linter contracts 20–22

## Context
Agents integrate through code (a library) or through a tool protocol (MCP). Both must authorise exactly like HTTP, with no privileged path and no way to name another workspace.

## Decision
1. **`sdk/python` (`kg-platform-sdk`)** is a thin typed client. Its pydantic models are **generated from `contracts/openapi.json`** (`scripts/gen_sdk_models.py`, only the schemas its operations reach); a drift test regenerates them and fails on a diff. The client (`KGClient` async, `KGClientSync`) is hand-written on `httpx`.
2. **Rules.** The key comes from an argument or `KG_API_KEY` and is never logged or shown in a `repr`/traceback. Every mutating call gets an `Idempotency-Key` generated once per logical call and reused by each retry, so the server sees one write. Retries (default 3) are bounded to 429/502/503/504, honour `retry_after` with jitter, and **never sleep on a wait longer than `max_retry_wait`** (a daily quota is raised as `QuotaExceeded`). Errors are typed exceptions built from the one envelope (`AuthError`, `NotFound`, `InvalidEvidence`, `QuotaExceeded`, `ContextIncomplete`, …) carrying `code`, `status`, `request_id`, `details`, `retry_after`. Every result carries `request_id`.
3. **`apps/mcp` (`kg-mcp`)** is a **stateless** Streamable HTTP server (the official MCP SDK's low-level `Server`, JSON responses). Each `tools/call` reads `Authorization: Bearer <platform key>` from **its own** HTTP request and builds an SDK client with that key. There is no session state, no service credential, no database or router setting (the configuration has four fields: API URL, host, port, timeout), and no workspace parameter. Nine tools: `build_context`, `search_memory`, `propose_memory`, `save_working_memory`, `publish_memory`, `ingest_interaction`, `get_interaction_status`, `get_evidence`, `list_profiles`.
4. **Closed input schemas.** A tool's input schema is the SDK's request model with `additionalProperties: false`, validated twice (JSON Schema, then pydantic): a `workspace_id`, `group_id`, `principal_id` or `agent_id` argument is a validation error, never ignored (AGT-03).
5. **Outputs.** `build_context` returns the context text as the text content (the P6 data envelope, first line `[CONTEXT DATA …]`) plus structured content (the whole package: items, citations, budgets). Errors are MCP tool errors (`isError`) carrying the platform's envelope (`code`, `message`, `request_id`); a missing key is `unauthenticated`.
6. **Boundaries by import-linter.** `kg_platform_sdk` imports no `kg_*` package (nor SQLAlchemy, Neo4j, Graphiti, OpenAI); `kg_mcp` imports only the SDK; no platform package imports either. A test starts a fresh interpreter, imports the server and asserts that no `kg_*` module other than the SDK is loaded.
7. **Parity is tested, not asserted**: for every tool the MCP data equals the HTTP data for the same key and request; a principal denied over HTTP is denied through MCP; a revoked key fails every tool on the next call, in the same MCP session.

## Consequences
- A new endpoint reaches the SDK only if its operation is added and its models regenerate; the drift test makes that visible.
- Two MCP clients with different keys share one server process with no cross-talk (tested).
- MCP SDK 2.x exists and is a different API; `mcp==1.30.0` is pinned in `compat/versions.lock.yaml` and revisited deliberately.
