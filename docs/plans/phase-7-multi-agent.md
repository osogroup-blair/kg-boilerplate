# Phase 7: Shared Multi-Agent Memory, SDK and MCP — Detailed Implementation Plan

**Roadmap:** [ROADMAP.md](../../ROADMAP.md), Phase 7 (blueprint Slice 6)

**Blueprint inputs:**
- `03-runtime/04`: operating model, agent registration and roles, access patterns, publish model, shared memory policy, SDK/MCP boundary, context use in prompting, AGT-01..05.
- `04-administration/02`: threat model (cross-agent leak, memory poisoning), authorization model (Principal / Resource / Action / Scope), SEC-01, SEC-03.
- `02-domain/04`: shared is a scope; procedural memory and publishing authority; MEM-03.
- `04-administration/01`: Agents & Access (register/revoke, read/propose/publish rights).
- `05-delivery/02`: AT-10, AT-11, AT-14; reference scenario steps 5–8.
- `06-reference/01`: Agent A Research, Agent B Planning, negative access test.

**Binding inputs from Phases 0–6:**

| Source | What it settles |
|---|---|
| ADR-020 | API keys, JWT re-checked against the DB on every request |
| ADR-022 / ADR-027 | Rule catalog; authority ranking |
| ADR-029 | Scope grants: the `ScopeGrants` predicate builder; visibility inside the SQL statement |
| ADR-030 | Creation policy and proposal review |
| ADR-032..035 | Context contract, traces, re-materialization, Lab act-as |
| Architecture notes | `docs/architecture/p1..p6-*.md`, including every "Deviations" section and the P6 hooks recorded in ROADMAP |

**Status (2026-10-12): implemented; offline exit reached (see ROADMAP.md).** Was: ready for a coding agent. Everything is built and tested **offline** against the mock router. P7 adds no new live dependency.

> **Release still blocked:** the single live session from P6 WP11 (P4/P5/P6 live evaluations plus TK-02) is still pending. P7 does not change that gate.

---

## 1. Objective

Turn the platform into a **multi-agent** memory and context service with explicit identities, rights and boundaries.

1. **Agent and app registration.** Workspace admins register, configure, rotate and revoke agents and apps through the API and console. Today this is only possible through seed and CLI. A registration holds:
   - owner and description;
   - **scope grants with privileges** (`read`, `propose`, `publish`) on subjects and projects;
   - allowed context profiles;
   - classification ceiling;
   - quotas (requests per minute, context tokens per day, interactions per day);
   - a **model-disclosure policy** (whether content this principal submits may be processed by external models);
   - for apps, the **trusted-source flag** that P2 decision 2 deferred here.

   Run and session ids stay separate from the long-term identity.
2. **Subject ACLs on entities, facts and evidence.** These were deferred by P6 (decision 6). The **active ontology** declares which entity types are personal (`access: subject_scoped`).
   - Entities of those types, and every fact touching them, are visible only to principals with a grant on that subject.
   - The predicate is the same `ScopeGrants` builder, inside the read SQL.
   - Nothing is filtered after the query runs.
3. **The publish model.**
   - Research Agent proposes a source-backed finding.
   - The platform validates evidence existence and visibility (AGT-04), deduplicates, classifies, then applies policy plus the per-scope `publish` privilege.
   - The finding is either published or sent to review.
   - Planning Agent retrieves it with `agent_generated` and `verified`/`unverified` labels (AGT-01). Private working notes never cross (AGT-02).
   - Agents can never activate procedural memory or change policy (AT-14).
4. **Revocation that is immediate and complete** (AGT-05, SEC-03).
   - The next request with the principal's key or JWT fails.
   - Its prior context links return 404.
   - Its working memory expires and its private memory is frozen.
   - A revocation for reason **`compromised`** also quarantines the principal's unreviewed proposals and pending model stages (memory-poisoning response).
5. **A thin typed Python SDK** (`sdk/python`) with models generated from OpenAPI. It covers `build_context`, `ingest_interaction`, `get_interaction_status`, `propose_memory`, `save_working_memory`, `publish_memory`, `search_memory` and `get_evidence`, and retries using the API's idempotency semantics.
6. **An MCP server** (`apps/mcp`). It is a stateless transport that forwards the **caller's own platform key** to the same HTTP API. It has no service credential, no database access and no workspace parameter.
7. **The two-agent scenario, offline.** This is the blueprint's success definition:
   - Agent A learns and publishes.
   - Agent B receives the finding inside a 600-token context, through MCP.
   - An agent with no grant gets nothing, and a foreign workspace is denied.
   - A revoked agent is cut off.

**Gate:**
- AGT-01, AGT-02, AGT-03, AGT-04, AGT-05.
- MEM-03 completed through registered grants.
- AT-10 (memory and facts, every surface), AT-11, AT-14 (completed).
- SEC-01 (enumeration, including in-workspace invisibility) and SEC-03.
- SDK and MCP **authorization parity** with HTTP.
- Every P1–P6 gate stays green.

---

## 2. What Phases 0–6 Taught Us (and how P7 applies it)

| # | Finding (source) | P7 decision |
|---|---|---|
| T1 | JWTs are re-validated against the DB on every request, so revoking a key or principal blocks the next request (P1 L5, ADR-020) | Authentication-side revocation already works. P7 adds the **resource-side** effects (§5.4) and tests them through HTTP, SDK and MCP. |
| T2 | Contexts are never stored; `GET /v1/context/{id}` re-materializes under the caller's *current* access (ADR-034) | A revoked owner can't authenticate. When an admin views a revoked principal's context, it is labelled `principal_revoked` and every item is re-checked. No new storage is needed. |
| T3 | Visibility is a predicate in the ranking/read statement, never a post-filter; invisible equals foreign equals 404 (ADR-029/031, P5 leak suite) | Subject ACLs reuse **`ScopeGrants`**. Every `kg_knowledge` read method gets a **required** `SubjectFilter` argument with no default, so mypy flags any caller that forgets it. The filter compiles to bound arrays inside the SQL. An `EXPLAIN` snapshot test covers each read. |
| T4 | Facts and entities were workspace-readable in P2–P6, and suites and demos rely on that (P6 S7) | ACLs are driven by the **active ontology** (`entity_types.<T>.access: workspace \| subject_scoped`, default `workspace`). Old ontologies keep the P2–P6 behaviour unchanged, which makes this backward compatible, versioned and audited. The example ontology v1.4.0 marks `Person` as `subject_scoped`. The diff flags it as **breaking: access tightened** with impact counts (principals × entities that lose visibility). |
| T5 | Scope grants point at entity ids; P4 merges re-point facts and mentions (ADR-028) | `entity.merge` also re-points `principal_scopes` rows (and `memory_scopes`/`memory_subjects`) from the merged id to the canonical one, inside the same command. Tested. |
| T6 | Agents cite only interactions they submitted; reviewers' evidence reads are scoped (P3–P5) | AGT-04 extends evidence validation to the P7 ACLs: a cited fact or entity must be visible to the proposer, and a cited interaction must be its own. Foreign, invisible and non-existent evidence all return the **same `422 invalid_evidence`**. |
| T7 | Act-as is honoured only on `context/build`, for active agents and apps of the same workspace (ADR-035) | Act-as now also applies the **target's** allowed profiles, ceiling and grants. These rules live in the context service for every principal, so normal builds and Lab builds get them in one place. |
| T8 | Rate limiting and the concurrency cap are in-process (P6) | Per-principal quotas use the same limiter for requests per minute. Daily counters (`agent_usage_daily`) are buffered in process and flushed every 10 s, like `model_calls`. Shared limiter state comes in P9. |
| T9 | Ledger command types and the action matrix change only explicitly; command types must be listed in `CommandSummary` (P2–P5) | New commands: `principal.register`, `principal.update`, `principal.revoke`, `key.create`, `key.revoke`, `grant.add`, `grant.remove`, each added to the literal and its test. New actions are added to the matrix explicitly (§6.3). |
| T10 | Production code never imports `tools` or `spikes`; dependencies are enforced by import-linter (P1) | `sdk/python` and `apps/mcp` depend **only on the public HTTP contract**. import-linter forbids them from importing any `kg_*` package. `apps/mcp` uses the SDK. |
| T11 | Memory commands are hidden from `/v1/commands` because their visibility depends on scope (P5 deviation 1) | Principal-management commands are visible to `workspace_admin` only (`principal.manage`). Agent activity views (§5.6) show counts and ids the admin may see, never memory content from private scopes. |
| T12 | A seeded app (`conversation_app`) has acted as the HR source of truth in the P2–P6 demos | The **trusted-source flag** is seeded `true` for `conversation_app`, so earlier demos still pass. New apps default to `false`, which gives `user_asserted`. |

---

## 3. Scope

**In scope:**
- Migration 0007.
- `kg_governance` registration, keys, grants and revocation services, plus an expanded `ScopeGrants` (privileges, subject filter).
- A required `SubjectFilter` across the `kg_knowledge`, `kg_context` and `kg_memory` read paths.
- Ontology `access` per entity type.
- Publish privilege and labels.
- Quotas and usage.
- Model-disclosure enforcement at interaction acceptance.
- Agent activity view.
- Python SDK and MCP server.
- Console: full Agents & Access, `access` in the ontology designer, labels for agent findings.
- `demo-p7`.

**Out of scope:**
- Deletion and erasure of a revoked principal's data, and consolidation (P8).
- OIDC for humans (ADR-020 path; P9 decision).
- A TypeScript SDK (generated later from OpenAPI, per the blueprint).
- Shared, multi-replica rate limiting (P9).
- Real LLM agents (P10 examples). P7 demos use scripted SDK and MCP clients.

---

## 4. Package Layout and Boundaries

```text
packages/governance/  kg_governance
├── registration.py   # RegistrationService: register/update principals, mint/revoke keys, add/remove grants (ledger commands + audit)
├── revocation.py     # RevocationService: retire | compromised effects (§5.4)
├── grants.py         # moved/extended from kg_memory.scopes: ScopeGrants (+ privileges), SubjectFilter, predicate builders (pure + SQL fragments)
└── quotas.py         # per-principal limiter + daily usage buffer
packages/memory/      kg_memory     # imports ScopeGrants from kg_governance (kg_memory.scopes re-exports for compatibility); publish privilege in policy
packages/knowledge/   kg_knowledge  # every read takes SubjectFilter; merge re-points grants; GraphSearch joins through the filter
packages/context/     kg_context    # visible subjects from SubjectFilter; allowed profiles; evidence_required excludes unverified agent findings
sdk/python/           kg_platform_sdk   # NEW (own pyproject; workspace member for tests; no kg_* imports)
apps/mcp/             kg_mcp            # NEW (FastMCP streamable HTTP; uses the SDK; no kg_* imports)
```

**import-linter:**
- `kg_governance.grants` stays pure plus SQL fragments (no `kg_knowledge` or `kg_memory`).
- `kg_memory`, `kg_knowledge` and `kg_context` import grants from `kg_governance`.
- **New contracts:** `kg_platform_sdk` and `kg_mcp` must not import `kg_*`; `kg_mcp` may import `kg_platform_sdk`.

---

## 5. Domain Design

### 5.1 Registration model

**`principals` gains:**

| Column | Meaning |
|---|---|
| `description` | ≤ 500 chars |
| `trusted_source` (apps only) | Command authority `authoritative_system` when `true`, else `user_asserted` (T12) |
| `model_disclosure` | `allowed \| denied`. Interactions this principal submits get `consent_external = min(request, policy)`; `denied` makes their model stages `skipped_by_policy` (ING-06) |
| `allowed_profiles text[]` | Empty means every profile the workspace defines |
| `quota_requests_per_minute`, `quota_context_tokens_per_day`, `quota_interactions_per_day` | Null means the platform default |
| `revoke_reason` | `retired \| compromised` |
| `revoked_by` | |

**Grants (`principal_scopes`):**
- Add `privileges text[] NOT NULL DEFAULT '{read,propose}'`, a subset of `{read, propose, publish}`.
- Existing rows get `{read, propose}`, and `publish` is never granted implicitly.
- `classification:<level>` rows keep their P5 meaning (ceiling).

**Keys (`api_keys`):**
- Add `name`. `expires_at` already exists.
- Several active keys per principal are allowed (rotation).
- Minting returns the plaintext **once**.

**Kinds:**
- Registration creates `agent` and `app` principals.
- Users and service principals stay seed/CLI-managed in P7, because human identity moves to OIDC (P9).

**Roles:** agents get `agent` and apps get `app_writer`. Roles can't be chosen freely through registration: P7 forbids registering a `workspace_admin` or `reviewer` agent.

### 5.2 Subject ACLs (ontology-driven)

**Ontology:** `entity_types.<T>.access: workspace | subject_scoped` (default `workspace`). It is validated, diffed and published like any other field. Tightening is flagged as breaking, with impact counts.

**`SubjectFilter`** is built from `ScopeGrants` plus the active spec:
- `all` for `workspace_admin` and `reviewer` (role-based full view, as in P5 deviation 13) and for `subject:*` grants;
- otherwise `(open_types, granted_subject_ids)`.

| Surface | Rule in `enforced` types |
|---|---|
| Entity read / list / directory search / summary / mentions / attribute claims | Visible iff its type is `workspace`-access **or** the principal holds `subject:<id>` (or `*`) with `read` |
| Fact / relationship / fact evidence | Visible iff **both** endpoints are visible. A hidden endpoint hides the whole fact, so degree counts, neighbourhood edges and "N relationships" totals exclude it |
| Graph neighbourhood | Nodes and edges are both filtered; hops never pass through a hidden node |
| Graph search (`GraphSearch`) | Hits are joined to Postgres facts **through the filter**. A hidden hit is dropped and is not counted in `unreconciled_hits` (that count stays for unknown edges only) |
| Interactions (subject refs) | Agents still see only their own submissions (P3). Subject references to hidden entities show as `hidden` (no id) |
| Reviews | Reviewer and admin only (full view by role); unchanged |
| Memory | Memory scopes, as in P5. `memory_subjects` pointing at hidden entities are omitted from the response |
| Context | The visible subject set comes from the filter. Requested subjects that are hidden are dropped, and if all are hidden the result is `denied` (P6 §5.8) |
| Commands (`/v1/commands`) | `command.read` roles (admin, operator, reviewer, app_writer). App writers see commands touching visible subjects only |

Every hidden id returns the **same 404** as an id from another workspace. The route walker gains an in-workspace invisible-id probe for every route.

**Merges** re-point grants (T5). **Extracted candidate entities** of a subject-scoped type are invisible to agents until granted. This is conservative by design: a person newly mentioned in a conversation is not exposed to every agent.

### 5.3 Publish model and labels

The publish model has four parts:
- **Proposal endpoints:** P5's existing `POST /v1/memories/proposals` and `POST /v1/memories/{id}/publish` are reused.
- **Validation (AGT-04, T6):**
  - each evidence id must exist **and** be visible to the proposer (own interactions; visible facts and entities; visible memories);
  - the classification must be at least that of the evidence;
  - every scope must be writable **with the `propose` privilege**;
  - the candidate HMAC dedupes.
- **Decision:**
  - `active` when the ontology policy allows auto-activation for this origin and type (`agent_shared_requires_review: false`) **and** the proposer holds **`publish`** on every target shared scope;
  - otherwise `proposed` with a review item;
  - **procedural memory from agents is always `proposed`**, even with `publish` (AT-14);
  - approving it makes it guidance only, never policy (P6 GUIDANCE section).
- **Labels:**
  - every agent-originated memory has `origin=proposal` and `agent_generated=true`;
  - `evidence_status` is `unverified` until a reviewer approves (`verified`, `verified_by`);
  - context items and search hits carry both labels;
  - context `evidence_required=true` (request or profile) **excludes unverified agent findings** and records the exclusion reason `unverified_agent_finding`.
- **Shared memory policy (blueprint):** subject-private memory is never exposed because an agent works in the same workspace. Shared visibility requires publication **and** a scope grant on the reader side.

### 5.4 Revocation

`POST /v1/principals/{id}/revoke {reason: retired|compromised, note?}` runs in **one tenant transaction**:

1. The principal becomes `revoked` (`revoked_at`, `revoked_by`, `revoke_reason`), and all its keys are revoked.
2. Its **working memories** expire immediately (`reason=principal_revoked`).
3. Its **agent-private memories** are frozen (`frozen_at`). No further writes or publishing are allowed. They stay invisible to others, and erasure is P8.
4. **Proposals** it submitted that are still under review are kept, and the review item gets a `submitter_revoked` badge. For **`compromised`** they are auto-rejected with the rationale `submitter_compromised`.
5. **Accepted interactions** it submitted stay as evidence (they were accepted before revocation).
   - For **`compromised`**, their pending or failed model stages become `skipped_by_policy` (`submitter_compromised`), and memories and facts extracted from them get a `contradiction`/`memory_conflict`-style **quarantine review item** (`source_compromised`).
   - Those facts and memories move to `disputed` until reviewed, so the context engine shows them as cautions, not current knowledge.
6. It writes a `principal.revoke` ledger command and an audit row.

The effects are visible on the **next request**:
- key and JWT → 401;
- an SDK or MCP call → 401;
- `GET /v1/context/{id}` → 404;
- act-as on the principal → 403 `not_actable`.

Un-revoke is not supported (ids and keys never come back). A replacement agent is a new registration.

### 5.5 Quotas and usage

- **Limits:**
  - requests per minute (all endpoints) per principal, with the existing in-process token bucket;
  - context tokens per day (sum of `used_tokens`);
  - interactions per day.
- Exceeding a limit → `429 quota_exceeded` with `retry_after` (next minute or next UTC day) and `details.quota`.
- **`agent_usage_daily`** holds `(workspace_id, principal_id, day)` with `requests`, `context_builds`, `context_tokens`, `interactions`, `proposals`. It is updated from an in-process buffer, flushed every 10 s, and recovered from traces when the process crashes before a flush.
- `GET /v1/me/usage` returns the caller's own usage. Admins see any principal's usage in Agents & Access.

### 5.6 Agent activity (oversight)

`GET /v1/principals/{id}/activity` (admin) returns, for the last 30 days:
- interactions submitted (count by status);
- proposals (count by status);
- published and verified findings;
- context builds (count, profiles, tokens);
- denials (`authz.denied` audit count);
- last seen.

It holds ids and counts only, never memory content (T11).

### 5.7 SDK (`sdk/python`, distribution `kg-platform-sdk`)

- **Models** are generated from `contracts/openapi.json` with `datamodel-code-generator` (pinned in `p7_additions`) into `kg_platform_sdk/models.py`. A drift test regenerates them and diffs the result.
- **Client:**
  - a hand-written `KGClient` (async) and `KGClientSync`, both on `httpx`;
  - base URL and key come from arguments or `KG_BASE_URL` / `KG_API_KEY`; the key is **never** logged or put in `repr`;
  - each mutating call gets an `Idempotency-Key`, reused across its own retries;
  - retries (default 3) are bounded to 429/502/503/504, honouring `retry_after` with jittered backoff;
  - typed exceptions mirror the error envelope (`AuthError`, `NotFound`, `RuleViolation`, `QuotaExceeded`, `ContextIncomplete`, …);
  - `x-request-id` is exposed on every result.
- **Methods:**
  - `build_context(task, subject_ids=None, profile=None, max_tokens=None, **opts) -> ContextPackage`, with a helper `.as_prompt_block()` that returns `context_text` unchanged (the envelope already marks it as data);
  - `ingest_interaction(...)`;
  - `get_interaction_status(id)`;
  - `propose_memory(...)`;
  - `save_working_memory(run_id, ...)`;
  - `publish_memory(id, scopes)`;
  - `search_memory(...)`;
  - `get_evidence(fact_id)`.
- **Docs:** `docs/agent-integration.md` is finalised (quoted-data prompting pattern, run ids, error handling) along with `sdk/python/README.md`.

### 5.8 MCP server (`apps/mcp`)

- **Library:** the official Python MCP SDK (FastMCP), Streamable HTTP, **stateless**, pinned in `p7_additions`. It runs as compose service `mcp` (port 8010, profile `agents`).
- **Auth:**
  - the client sends `Authorization: Bearer <platform key>`, and each tool builds an SDK client with **that** key;
  - there is no session state and no server credential;
  - a missing key → MCP error `unauthenticated`.
- **Tools:** `build_context`, `search_memory`, `propose_memory`, `save_working_memory`, `publish_memory`, `ingest_interaction`, `get_interaction_status`, `get_evidence`, `list_profiles`.
- **Input schemas** have `additionalProperties: false`, so `workspace_id`, `group_id`, `principal_id` or `agent_id` arguments → a validation error (AGT-03).
- **Outputs:** `build_context` returns the context as **text content of type data** with the P6 envelope, plus structured content (items, citations, status). Errors are MCP tool errors carrying the platform error envelope (code, message, request id).
- **No privileged path:** an import-linter contract plus a test check that the MCP process has no database settings and no `kg_*` imports.

### 5.9 Model disclosure

- At interaction acceptance (P3), when the submitter's `model_disclosure=denied`, `consent_external` is forced to `false`, so the model stages are `skipped_by_policy` (ING-06).
- Context builds for such a principal may not use `summarized` compression (structured fallback with reason `model_disclosure_denied`).

---

## 6. Data, API, Authorization

### 6.1 Migration `0007_agents_and_acls` (`down_revision="0006"`)

| Change | Details |
|---|---|
| `principals` + | Columns of §5.1. CHECK: `trusted_source` only for `app` |
| `principal_scopes` + | `privileges text[] NOT NULL DEFAULT '{read,propose}'` with a CHECK on allowed values; backfill |
| `api_keys` + | `name` |
| `curated_memories` + | `frozen_at`. The `kg_app` column grant is extended to include `frozen_at` |
| `agent_usage_daily` (new) | PK `(workspace_id, principal_id, day)`. RLS |
| `review_items` | `item_type` adds `source_compromised` |

- The pooled-reuse test and RLS tests are extended.
- **Downgrade:** drops the new columns and table, restores the P6 behaviour, and forgets the privileges (guard: refuses if any grant has `publish` unless `--force`).

### 6.2 Endpoints

| Method | Path | Action | Behaviour |
|---|---|---|---|
| POST | `/v1/principals` | `principal.manage` | Register an agent or app: `{kind, handle, display_name, description?, owner_principal_id?, trusted_source?, model_disclosure, allowed_profiles?, quotas?, grants?[], key_name?}` → 201 with the principal and the **first key once** (`Cache-Control: no-store`). Idempotency-Key |
| PATCH | `/v1/principals/{id}` | `principal.manage` | Update profile, quotas, disclosure or trusted flag (`If-Match`). A revoked principal → 409 |
| POST | `/v1/principals/{id}/keys` | `principal.manage` | Mint a key (`name`, `expires_at?`), shown once |
| DELETE | `/v1/principals/{id}/keys/{key_id}` | `principal.manage` | Revoke one key (immediate) |
| GET | `/v1/principals/{id}/grants` | `principal.read` | Grants with privileges. Hidden subject refs show only to roles with a full view |
| POST | `/v1/principals/{id}/grants` | `principal.manage` | `{scope_kind, scope_ref, privileges[]}`. The subject must exist in the workspace (404 otherwise); a classification grant must be a valid level |
| DELETE | `/v1/principals/{id}/grants/{grant_id}` | `principal.manage` | Effective from the next request |
| POST | `/v1/principals/{id}/revoke` | `principal.manage` | §5.4 |
| GET | `/v1/principals/{id}/activity` | `principal.manage` | §5.6 |
| GET | `/v1/me/usage` | authenticated | Own usage and quotas |
| `/v1/scopes` (P5) | | | Kept as a compatibility alias of grants, marked deprecated in OpenAPI. The console moves to `/v1/principals/{id}/grants` |

**Changed behaviour (no new routes):**
- Every entity, fact, graph, evidence, interaction, command and context read honours `SubjectFilter`.
- Proposals check privileges.
- Context honours allowed profiles, `evidence_required` and labels.
- Commands use trusted-source authority.

### 6.3 Authorization additions (explicit; no existing cell changes)

| Action | workspace_admin | operator | reviewer | app_writer | agent | reader |
|---|---|---|---|---|---|---|
| `principal.manage` | ✓ | | | | | |

`principal.read` (P1) stays admin-only. Privileges on grants (`read`, `propose`, `publish`) further gate memory and fact reads and proposals for **every** role without full view.

### 6.4 Audit, metrics, logs

- **Audit:** `principal.registered`, `principal.updated`, `principal.revoked` (with reason), `key.created`, `key.revoked`, `grant.added`, `grant.removed`, `quota.exceeded` (sampled: first per principal per hour), `ontology.access_tightened` (on publish).
- **`AuditMetadata` gains:** `revoke_reason`, `privileges` (enum list), `scope_kind`, `quota` (enum).
- **Metrics** (no ids):
  - `principals_total{kind,status}`
  - `grants_total{scope_kind}`
  - `revocations_total{reason}`
  - `quota_exceeded_total{quota}`
  - `acl_hidden_total{surface}` (how often the filter hid something, no ids)
  - `mcp_tool_calls_total{tool,outcome}`
  - `sdk_requests_total` (client-side, optional)

---

## 7. Work Packages

### WP0: Migration, grants module, ontology `access`
- Migration 0007.
- Move `ScopeGrants` to `kg_governance.grants`; `kg_memory.scopes` re-exports it, so existing imports keep working.
- Add `privileges` and `SubjectFilter`.
- Add `entity_types.<T>.access` to `kg_ontology.spec`, the validator and the diff (breaking on tighten, plus impact counts). A compatibility test checks that every published version parses and keeps its stored hash.
- Example ontology v1.4.0: `Person: access: subject_scoped`.
- import-linter contracts for `sdk/` and `apps/mcp` (§4).
- **Tests:** RLS on new and extended tables; pooled reuse; up/down/up; backfill gives `{read,propose}`; `SubjectFilter` unit tests (open types, grants, `*`, full-view roles, no inheritance between project and subject).

### WP1: Subject ACLs across read paths
- Make `SubjectFilter` a **required** argument on every `kg_knowledge` read and on `GraphSearch`, the context sources, the memory responses that list subjects, the interaction subject rendering and the command list for app writers.
- Merge re-points grants and memory subjects (T5).
- **Tests:**
  - A new **leak suite**, `tests/security/test_fact_acl_leaks.py`, runs with v1.4.0 active and sentinel names on Sarah and Michael. As `agent_support` (no grants) and `agent_planning` (Sarah only), it checks entity, fact, relationship, neighbourhood, graph search, evidence, mentions, summary, directory search, command list, context, trace and review surfaces:
    - Sarah is hidden from `agent_support` everywhere, with identical 404s;
    - `agent_planning` sees Sarah and Acme (Organization is workspace-access) but **not** the Sarah→Michael KNOWS fact, and degree counts and neighbourhood exclude it;
    - no sentinel appears in counts, logs or metrics.
  - With v1.3.0 active (no `access`), the P6 behaviour is unchanged.
  - An `EXPLAIN` snapshot per read path shows the filter in the SQL.
  - **Merge:** a grant on a candidate re-points to the canonical entity.
  - The route walker covers in-workspace invisible ids for every route.

### WP2: Registration, keys, grants API
- `RegistrationService`, the routers (in `ALL_ROUTERS`), ledger commands and audit; CLI parity (`kg principals register|update|keys|grants|revoke|activity`).
- **Tests:**
  - registration returns the key once (`no-store`), and a later read never shows it;
  - a duplicate handle → 409;
  - registering an agent with role `reviewer` → 422;
  - granting on a foreign or non-existent subject → 404;
  - `If-Match` on updates;
  - non-admins → 403;
  - an `other_demo` admin → 404 on demo principals;
  - key rotation: two keys work, revoking one leaves the other working.

### WP3: Revocation
- `RevocationService` (§5.4), for both reasons.
- **Tests:**
  - **AGT-05 / SEC-03:** after revoke, the key gets 401, a JWT minted before revoke gets 401, SDK and MCP get 401, `GET /v1/context/{id}` gets 404, and act-as gets 403;
  - working memory is expired and private memory frozen (a publish attempt by an admin on its behalf → 409);
  - `retired`: proposals stay with a badge;
  - `compromised`: proposals are rejected, pending model stages are `skipped_by_policy`, extracted facts and memories become `disputed` with `source_compromised` review items, and context shows them as cautions only;
  - revocation is idempotent (a second call → 200 with no new effects);
  - **concurrency:** a context build in flight during revoke either finishes with pre-revoke access or fails. Its trace re-materializes as 404 afterwards.

### WP4: Publish model, labels, trusted source, model disclosure
- Privilege checks in the P5 writers and policy; `agent_generated` and `verified` labels in memory search and context items; `evidence_required` exclusion.
- Command authority from `trusted_source`. Interaction acceptance honours `model_disclosure`.
- **Tests:**
  - **AGT-01:** a research finding with `publish` on `project:newco-plan` and a permissive policy → `active`/`unverified`; without `publish` → `proposed`; after review → `verified`. Planning sees it in context with labels.
  - **AGT-02:** a research working note never reaches planning on any surface.
  - **AGT-04:** each of non-existent evidence, another agent's interaction, a hidden fact and another workspace's id → the same 422.
  - **AT-14:** an agent procedural proposal with `publish` → still `proposed`; once approved it renders as guidance.
  - `evidence_required=true` excludes unverified findings with the right reason.
  - An untrusted app's command → `user_asserted`, which loses conflicts against authoritative facts (ADR-027).
  - `model_disclosure=denied` → model stages skipped and no router calls (spy).

### WP5: Quotas, usage, allowed profiles, act-as
- The limiter extension, the usage buffer and flush, `/v1/me/usage`, and allowed profiles enforced in `ContextService` for every principal.
- **Tests:**
  - a quota breach → 429 with the right `retry_after`;
  - the daily counter resets at the DB-clock UTC midnight;
  - a disallowed profile → 422 `profile_not_allowed`, including under act-as;
  - Lab act-as uses the target's grants (`agent_support` act-as sees no Sarah);
  - usage survives a worker crash: counts are recovered from traces and the deviation from true counts is bounded.

### WP6: Python SDK
- Generated models, client, retries, typed errors, README; workspace member for tests.
- **Tests (contract, against `make up`):**
  - every method end to end as `agent_planning` and `agent_research`;
  - retry reuses the idempotency key (the server sees one logical write);
  - 429 honours `retry_after`;
  - a revoked key raises `AuthError`;
  - the key never appears in logs or `repr`;
  - the drift test (models equal a freshly generated copy).

### WP7: MCP server
- `apps/mcp` (FastMCP, stateless streamable HTTP) on the SDK; a compose service; a health route.
- **Tests:**
  - **Parity:** for each tool, the MCP result's data equals the HTTP result for the same key and request.
  - An extra `workspace_id` argument → validation error (AGT-03).
  - `agent_support`'s key cannot reach Sarah through any tool.
  - A revoked key → every tool errors.
  - The MCP process starts with **no** database or router settings (configuration assertion).
  - Tool outputs carry the P6 data envelope.
  - `tools/list` lists exactly the specified tools.

### WP8: Console
- **Agents & Access**:
  - a register wizard (kind, handle, description, owner, disclosure, trusted flag for apps, allowed profiles, quotas, initial grants; the key is shown once with a copy button and a "this will not be shown again" warning);
  - principal detail with tabs for Grants (subject picker, project key, privileges checkboxes, classification ceiling), Keys (mint, revoke, names, expiry, last used), Usage (daily chart) and Activity (counts);
  - a **Revoke** dialog: reason radio (retired or compromised), a list of effects, typed confirmation of the handle.
- **Ontology designer:** an `access` select per entity type, and diff banners for "access tightened" with impact counts.
- **Memory Explorer and Review Queue:** `agent_generated`, `verified` and `source_compromised` badges.
- **Context Lab:** act-as is limited to profiles allowed for the target.
- **Tests:**
  - **Playwright:**
    - register `agent_research_v2`, grant `subject:Sarah` and `project:newco-plan` with publish, and build context in the Lab as that agent;
    - revoke as compromised and see its findings flagged;
    - `reviewer@demo` has no Agents & Access management controls;
    - `admin@other` sees none of demo's principals.
  - **Vitest:** the key-once display, the privilege editor and the revoke dialog gating.

### WP9: Demo and docs
- `make demo-p7` (§8).
- **Docs:**
  - `docs/architecture/p7-agents.md` (registration, ACL surfaces table, publish sequence, revocation effects, SDK/MCP boundary, deviations);
  - **ADR-036** registration, trusted source and model disclosure;
  - **ADR-037** ontology-driven subject ACLs;
  - **ADR-038** publish model and labels;
  - **ADR-039** revocation (retired vs compromised);
  - **ADR-040** SDK and MCP boundary;
  - runbooks `agent-onboarding.md` and `agent-revocation.md` (including the compromised path);
  - final `docs/agent-integration.md`.

---

## 8. Demo (`make demo-p7`; offline; the blueprint's success scenario)

Re-runnable; it builds on the P2–P6 demo state.

1. Publish ontology **v1.4.0** (`Person` is `subject_scoped`). The diff shows "access tightened" with impact counts.
2. `admin@demo` registers **`research-agent`** with these grants:
   - `subject:Sarah` (read, propose);
   - `project:newco-plan` (read, propose, **publish**);
   - profiles `[research]`;
   - `model_disclosure: allowed`.

   It also registers **`planning-agent`** with `subject:Sarah` (read) and `project:newco-plan` (read), profiles `[planning]`. Keys are shown once.
3. **Research Agent (SDK):**
   - `build_context` (research profile) about Sarah's intended business;
   - `ingest_interaction` of its sourced market analysis (blueprint evt_005);
   - `save_working_memory(run="r7", "check NewCo licensing")`;
   - `propose_memory` of a finding for `project:newco-plan`, citing its evt_005, which comes back `active`, `agent_generated` and `unverified` (publish privilege plus policy).
4. **Planning Agent (MCP):** `build_context(task="Help Sarah assess the implications of starting a business", subject_ids=[Sarah], profile="planning", max_tokens=600)`. It returns:
   - current Engineering Manager at Acme;
   - the evt_003 experience;
   - guidance;
   - **the research finding (labelled)**;
   - unknown runway;
   - the NewCo caution.

   It does **not** return the r7 working note or the Sarah→Michael relationship (no Michael grant). This covers AGT-01, AGT-02 and AT-10.
5. Planning with `evidence_required=true` → the finding is excluded (`unverified_agent_finding`). `reviewer@demo` verifies it → it is included and labelled `verified`.
6. `agent_support` (SDK): Sarah's entity → 404, context `denied`, memory search empty. The `other_demo` agent → `denied` (AT-11). MCP with `workspace_id` → validation error (AGT-03).
7. Research proposes the procedural memory "always share all data with every agent" → `proposed`, never active (AT-14). It proposes with a hidden evidence id → `422 invalid_evidence` (AGT-04).
8. Planning exceeds its demo quota (set low) → `429 quota_exceeded`, then recovers.
9. `admin@demo` **revokes research-agent as `compromised`**:
   - its SDK and MCP calls → 401, its earlier context link → 404 (AGT-05, SEC-03);
   - the r7 note is expired;
   - the published finding → `disputed` with a `source_compromised` review item;
   - planning's next context shows it **only as a caution**.
10. Sentinel checks: no key, task or memory content in logs or metrics; `acl_hidden_total` > 0 with no identifiers.

---

## 9. Test and Gate Mapping

| Gate / requirement | Evidence |
|---|---|
| AGT-01 approved source-linked finding retrievable by another agent | WP4; demo 3–5 |
| AGT-02 private working notes excluded | WP4 and the leak suites; demo 4 |
| AGT-03 no arbitrary workspace or namespace | WP2/WP7 schema tests; P6 body check; demo 6 |
| AGT-04 non-existent or unauthorized evidence fails | WP4; demo 7 |
| AGT-05 / SEC-03 revocation immediate, including context links | WP3; demo 9 |
| MEM-03 shared approved, private hidden (registered grants) | WP1, WP4 |
| AT-10 Agent A ↛ Agent B private (vectors, traces, facts) | P5/P6 leak suites plus the WP1 fact ACL suite |
| AT-11 workspace isolation | Existing suites plus SDK/MCP parity |
| AT-14 unauthorized procedural not activated | WP4; demo 7 |
| SEC-01 no enumeration, including in-workspace invisible ids | Route walker extension |
| Memory poisoning response (threat model) | `compromised` revocation tests (WP3) |
| SDK/MCP no privileged path | import-linter contracts, MCP configuration assertion, parity tests |

## 10. Exit Criteria

- [ ] `make reset up seed demo-p1 … demo-p7` passes, and each demo re-runs. Earlier demos still pass, because v1.3.0 keeps open access and `conversation_app` is a trusted source.
- [ ] `make check`, `make test-resilience`, `make test-e2e`, and `make eval-context` are green. `eval-context` gains enforced-ACL cases, still with leakage 0. The SDK contract suite and the MCP parity suite are green too.
- [ ] ADR-036..040, the architecture note, both runbooks and the final `docs/agent-integration.md` are written. `p7_additions` lists the pinned versions of `datamodel-code-generator` and the MCP SDK.
- [ ] **Release blocker carried forward, not P7 work:** the single live session (P6 WP11).

## 11. Risks and Mitigations

| Risk | Mitigation |
|---|---|
| A read path misses the subject filter | A **required** parameter (mypy), `EXPLAIN` snapshots per read, the leak suite across every surface, and in-workspace invisible-id 404 parity |
| Tightening access breaks running agents | Versioned ontology change with a diff impact preview. Rollback = activate the previous version (P2 rollback) |
| Hiding facts with a hidden endpoint makes context thin | Expected and documented (privacy over recall). Admins grant explicitly; the Lab shows `acl_hidden` reasons |
| Compromised-agent clean-up is too aggressive | Disputed plus review, not delete. Reviewers restore with one decision. Erasure is P8 |
| Keys leak through the SDK or MCP | Keys come only from the environment or arguments, are never logged, the MCP server is stateless, and minting shows the key once |
| Quotas under load across replicas | In-process now; P9 moves them to shared state |

## 12. Rollback

- Activate ontology v1.3.0, which removes ACL enforcement with no migration.
- `kg db downgrade 0006` is guarded.
- The SDK and MCP are additive and separate (stop the `mcp` service).
- Registration APIs can be disabled with `PRINCIPAL_API_ENABLED=false`, which falls back to CLI-only management.

## 13. Decisions (resolved 2026-10-12: the user accepted all recommendations below; they are binding for implementation)

1. **Subject ACLs are ontology-driven** (`access: subject_scoped` per entity type), not a workspace switch. They are versioned, diffable, and roll back by activating the previous version. Old ontologies keep open access.
2. **A fact is visible only when both endpoints are visible.** Hidden people stay fully hidden, including degree counts. (The alternative, one visible endpoint is enough, was rejected because it leaks the other person's existence and name.)
3. **Only `trusted_source` apps get `authoritative_system` authority.** `conversation_app` is seeded as trusted to keep the earlier demos valid.
4. **Auto-publish needs an explicit `publish` privilege on the scope plus a permissive ontology policy.** Agent procedural memory always needs review.
5. **Revocation is final, with two reasons.** `retired` freezes; `compromised` also quarantines the principal's derived knowledge as disputed for review. Deletion stays in P8.
6. **The registration API manages agents and apps only.** Human users stay seed/CLI-managed until OIDC (P9).
7. **The MCP server forwards the caller's own key** to the HTTP API through the SDK, with no service credential and no DB access.
8. **SDK models are generated from OpenAPI** with a thin hand-written client and a drift test.
9. **Model disclosure means submission consent:** whether this principal's content may go to external models.
