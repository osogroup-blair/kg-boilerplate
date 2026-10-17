# Phase 7: shared multi-agent memory, the SDK and MCP

Decisions: [ADR-036](../adr/ADR-036-agent-registration-trusted-source-and-model-disclosure.md) (registration, trusted source, model disclosure), [ADR-037](../adr/ADR-037-ontology-driven-subject-acls.md) (subject ACLs), [ADR-038](../adr/ADR-038-publish-model-and-labels.md) (publish model and labels), [ADR-039](../adr/ADR-039-revocation-retired-vs-compromised.md) (revocation), [ADR-040](../adr/ADR-040-sdk-and-mcp-boundary.md) (SDK and MCP). Plan: [phase-7-multi-agent.md](../plans/phase-7-multi-agent.md). Run it: `make reset up seed demo-p1 demo-p2 demo-p3 demo-p4 demo-p5 demo-p6 demo-p7`. Runbooks: [agent-onboarding](../runbooks/agent-onboarding.md), [agent-revocation](../runbooks/agent-revocation.md). Integrating an agent: [agent-integration](../agent-integration.md), [sdk/python/README.md](../../sdk/python/README.md).

## Packages
| Package | P7 |
|---|---|
| `kg_governance` | `grants` (`ScopeGrants` with privileges, `SubjectFilter`, the SQL fragments), `principals` (`Registration`: register, update, keys, grants, activity; the request/response models), `revocation` (`Revocation`), `quotas` (`Quotas`: the request bucket, the daily limits, the usage buffer); action `principal.manage` |
| `kg_memory` | `scopes` re-exports `ScopeGrants`; `decide(can_publish=)`; the writers check `propose`/`publish`, cite only visible evidence, refuse a frozen memory; `reviewing` verifies agent findings and decides `source_compromised`; `compromised_cautions`; `agent_generated` on summaries and hits |
| `kg_knowledge` | every read takes a required `SubjectFilter`; `SpecCache.scoped_types`; `Reads.subject_filter`; merge re-points grants and memory references; `derive_authority` honours `trusted_source`; `access_impact` in the ontology diff |
| `kg_ontology` | `entity_types.<T>.access` (omitted from the canonical form when `workspace`) and the "access tightened" change |
| `kg_context` | subject filter, allowed profiles, `model_disclosure`, `evidence_required` + `unverified_agent_finding`, labels in text and items, cautions, `principal_revoked` on re-materialisation |
| `kg_ingestion` | consent = min(request, `model_disclosure`); subject references to hidden entities are `hidden` |
| `kg_extraction`, `kg_memory.extract` | a compromised submitter's job skips itself (`submitter_compromised`) |
| `apps/api` | `/v1/principals…` (register, update, keys, grants, revoke, activity, usage), `/v1/me/usage`, the request quota in the guard, the daily quotas in the context and interaction routes |
| `apps/worker` | gauges `principals_total`, `grants_total` from `ops.principal_stats` / `ops.grant_stats` |
| `apps/cli` | `kg principals list|register|update|keys|grants|revoke|activity` (an HTTP client of the same API) |
| `sdk/python` | `kg_platform_sdk`: `KGClient`, `KGClientSync`, typed errors, models generated from OpenAPI |
| `apps/mcp` | `kg_mcp`: the nine tools over the SDK, stateless |
| `apps/admin` | Agents & Access (register wizard, grants and privilege editor, keys, usage, activity, revoke dialog), the ontology `access` select and diff banner, labels, `source_compromised` reviews, Lab limited to the target's profiles |

## The data (migration `0007_agents_and_acls`)
`principals` + `description, trusted_source, model_disclosure, allowed_profiles, quota_*, revoke_reason, revoked_by, updated_at, revision` · `principal_scopes.privileges` · `api_keys.name` · `curated_memories` + `frozen_at, verified_by, verified_at` · `agent_usage_daily` (RLS) · `review_items` type `source_compromised` · `commands` types `principal.*`, `key.*`, `grant.*` (no ontology version needed) · `ops.principal_stats()`, `ops.grant_stats()` · three triggers (below). Downgrade refuses while a grant carries `publish` unless `--force-forget-privileges`.

Triggers: `curated_memories_evidence_guard` (the only legal change of `evidence_status` is `unverified → verified`, by a named reviewer, on an agent finding), `memory_scope_repoint_guard` and `memory_subject_repoint_guard` (a memory's scope or subject changes only when the old subject is a merged entity whose canonical id is the new value).

## Where subject ACLs apply (ontology v1.4.0: `Person` is `subject_scoped`)
| Surface | Rule |
|---|---|
| Entity read, list, directory search, summary, mentions, attribute claims | visible iff its type is open **or** the caller holds `subject:<id>` / `subject:*` with `read`, or is an admin / reviewer |
| Fact, relationship, fact evidence | visible iff **both** endpoints are; a hidden end hides the fact, its degree count and its edges |
| Graph neighbourhood | nodes and edges both filtered; a hop never passes through a hidden node |
| Graph search | hits are joined to Postgres through the filter; a hidden hit is dropped (counted in `acl_hidden_total{surface}`), not counted as unreconciled |
| Interactions | agents see only their own submissions (P3); for staff, a subject reference to a hidden entity is `hidden` without its id; filtering by a hidden subject is empty |
| Commands (`/v1/commands`) | a command touching a hidden entity is not listed or readable |
| Memory | memory scopes (P5); `memory_subjects` naming a hidden entity are omitted from the response |
| Context | the visible subject set comes from the filter; hidden requested subjects are dropped, all hidden → `denied` |
| Reviews | admin / reviewer, full view by role |

A hidden id is the same 404 as a foreign or missing one. With v1.3.0 (no `access`) nothing changes.

## The publish sequence
```
Research Agent                         Platform                                                   Planning Agent
 ingest_interaction(evt_005) ───────►  accepted (own submission; pipeline runs unless model_disclosure=denied)
 propose_memory(finding, evidence) ──► evidence visible to the proposer? scopes writable (propose)? dedupe (HMAC)
                                       policy lets an agent through AND publish on every target scope?
                                          yes ► active, origin=proposal, agent_generated, unverified  + a review item to verify it
                                          no  ► proposed + review item (agent_shared_requires_review | publish_privilege_required)
                                       procedural from an agent ► ALWAYS proposed (AT-14)
                                                                                                    build_context(...) ◄──
                                       memory visible by scope+grant ► item labelled "(agent_generated, unverified)"
                                       evidence_required ► excluded: unverified_agent_finding
 reviewer: approve ──────────────────► evidence_status=verified, verified_by/at  ► next context: "(agent_generated, verified)"
```

## Revocation, by effect
See the table in [ADR-039](../adr/ADR-039-revocation-retired-vs-compromised.md). The next request with the key or a JWT minted before is a 401 through HTTP, SDK and MCP; the principal's context links are 404 to everyone but an admin; act-as on it is 403 `not_actable`.

## Quotas and usage
Requests per minute: an in-process token bucket per principal (cache of the registration for 5 s, dropped on update); context tokens and interactions per **UTC day by the database clock**, counted from the durable tables (`context_traces` without Lab builds, `interactions`), so a restart or crash loses nothing. A refusal is `429 quota_exceeded` with `retry_after` and `details.quota`; audited once per principal per quota per hour. `agent_usage_daily` is flushed every `PRINCIPAL_USAGE_FLUSH_SECONDS`; only the request counter is lost in a crash (at most one window). `GET /v1/me/usage` (own), `GET /v1/principals/{id}/usage|activity` (admin: ids and counts only, never private content).

## SDK and MCP boundary
`kg_platform_sdk` and `kg_mcp` import no platform package (import-linter); the MCP process has no database or router configuration. Each tool call forwards the caller's own key. See ADR-040.

## Deviations from the plan (and why)
1. **A finding that policy activated opens a `memory_proposal` review item** (the plan says it is "unverified until a reviewer approves" without saying where the reviewer finds it). Approve verifies, reject withdraws.
2. **Verification is recorded** (`verified_by`, `verified_at`) and guarded by a trigger; the plan listed only the label. Likewise the two repoint triggers narrow the column grants that merge re-pointing needs.
3. **Daily quotas are read from the durable tables, not from a flushed buffer** (the plan's `agent_usage_daily` recovery "from traces" is the same idea, applied at check time). Only the request counter is buffered. Lab builds are not charged to the target's quota.
4. **A quota refuses *after* it is exhausted**: the last allowed build may overshoot the daily token quota by up to one budget.
5. **`model_disclosure=denied` also stops the task being embedded** (graph unavailable, memory lexical-only, `partial`), not only summarisation: the task is content the principal may not send out.
6. **Two new review decisions**, `restore` and `confirm_removal`, for `source_compromised` (the closed decision set had no word for it).
7. **Facts created by a compromised *app's commands* are not quarantined** (only what was extracted from its submissions); the ledger shows its commands for a manual review.
8. **A revoked owner's own context link is a 401, not a 404** (it cannot authenticate); everyone else gets 404; an admin gets 200 labelled `principal_revoked`.
9. **Grants of kind `workspace` (ref `*`) and `classification` exist** so publishing to the workspace scope and the classification ceiling are expressible; a classification grant is read-only.
10. **The memory `Viewer` carries its `SubjectFilter` with an unenforced default** (the entity, fact, command, interaction and graph reads require it). The memory visibility predicate is the scope predicate; the filter there only decides which subject ids a response may name.
11. **The MCP server uses the low-level `Server`, not FastMCP**, to own the input schemas (`additionalProperties: false`) and the error envelope.
12. **`/v1/scopes` stays as a deprecated read-only alias**; the console moved to `/v1/principals/{id}/grants`.
13. **`PRINCIPAL_REQUESTS_PER_MINUTE` defaults to 1200** (the plan says "the platform default" without a figure); the shared limiter is P9.
14. **Not built (as planned)**: erasure of a revoked principal's data and consolidation (P8), OIDC (P9), a TypeScript SDK, a shared limiter (P9), real LLM agents (P10).
