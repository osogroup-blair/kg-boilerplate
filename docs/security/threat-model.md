# Threat review (phase 9)

**Status:** review complete for release candidate 1; **owner sign-off pending** (the checklist in `docs/release/checklist.md` carries the box).
**Method:** STRIDE over every trust boundary the platform has built, then the blueprint's threat table re-checked row by row against what exists, then the new findings. Every
control below names the test or report that proves it. A control with no evidence is listed as a finding, not as a control.

**Standing rule (the plan's WP6):** every *high* or *critical* finding is fixed in this phase or explicitly accepted by the owner. Nothing high or critical is open without one of the two.

## 1. Trust boundaries

```
 agents / apps / people (untrusted callers)
        │  TLS (Caddy)                                    ┌─ OIDC identity provider (external, trusted for identity only)
        ▼                                                 │
 ┌── edge ──┐   ┌── API replicas (≥2) ──┐   ┌── worker ──┐ │
 │ Caddy    │──▶│ authn · authz · RLS   │   │ outbox ·   │◀┘
 │ admin SPA│   │ shared limiter        │   │ extraction │
 └──────────┘   └───────┬───────┬───────┘   └──┬──────┬──┘
   MCP server ──▶ API    │       │              │      │
                         ▼       ▼              ▼      ▼
                    Postgres   Neo4j        model router (egress, trusted infrastructure boundary)
                    (RLS)     (group_id)
 backups (age-encrypted, off-host)   deletion journal (HMAC chain, outside DB and backups)   secrets (files)
```

| # | Boundary | Crossing data |
|---|---|---|
| B1 | Internet → edge (Caddy) | TLS requests; API keys / JWTs; OIDC redirects |
| B2 | Edge → API | proxied requests, `X-Forwarded-*` |
| B3 | API/worker → Postgres | all state; only as `kg_app` (no BYPASSRLS) |
| B4 | API/worker → Neo4j | Graphiti calls scoped by `group_id` |
| B5 | API/worker → model router | prompts and completions (classification-gated) |
| B6 | Console (browser) → API | JWT in memory, refresh cookie (httpOnly, SameSite=Strict) |
| B7 | SDK / MCP → API | the caller's own key, forwarded |
| B8 | Platform → backups | encrypted dumps, key escrow |
| B9 | Worker → deletion journal | ids and HMACs only |
| B10 | Operator → CLI / secret files | break-glass and rotation |

## 2. STRIDE by boundary

Legend: **S**poofing · **T**ampering · **R**epudiation · **I**nformation disclosure · **D**enial of service · **E**levation of privilege. "Evidence" is a test, a report or a file; `ST` = `tests/security`.

### B1/B2 Edge and API
| | Threat | Control | Evidence |
|---|---|---|---|
| S | Forged or replayed token | RS256/ES256 pinned algorithms, `kid` ring, key re-check against the database on every request (revocation is immediate) | `ST/test_revocation.py`, `packages/governance/tests/test_oidc.py`, `tests/integration/test_p9_oidc.py` |
| S | Algorithm confusion (`none`, HS with a public key) | the verifier accepts only the configured asymmetric algorithms | `packages/governance/tests/test_oidc.py::test_everything_else_is_refused`, `::test_hmac_and_none_can_never_be_configured` |
| T | Request smuggling / oversized body | per-route body limits (256 KiB by default, 1 MiB for interactions and ontologies); Caddy `max_size` 2 MB as a second fence | `tests/integration/test_p9_hardening.py` |
| R | Admin action without trace | every mutation writes `audit_log` (append-only, purge-gate protected) | `ST/test_deletion_residuals.py`, `tests/integration/test_p8_privacy_audit.py` |
| I | Verbose errors / stack traces | one error envelope; production never returns internals; request id only | `tests/integration/test_api_contract.py` |
| I | Browser-side injection / framing | API: `default-src 'none'; frame-ancestors 'none'`, `nosniff`, `Referrer-Policy`; console: CSP without inline script (Caddyfile) | `test_p9_hardening.py`, `make prod-smoke` |
| D | Request flood | shared Postgres-backed limiter (one budget across replicas, database clock); context builds fail closed | `tests/integration/test_p9_shared.py`, FI-17 |
| D | Slow-loris | Caddy and uvicorn timeouts; per-workspace concurrency cap (429) | FI-17 |
| E | Reaching `/docs`, `/openapi.json`, metrics | off in production; metrics bound to the internal network only | `test_p9_hardening.py`, `make prod-smoke` |

### B3 Postgres
| | Threat | Control | Evidence |
|---|---|---|---|
| S | Application connects as a privileged role | runtime role `kg_app` is `NOBYPASSRLS`; `kg_owner` is used for migrations only | `tests/integration/test_db*.py` |
| T | Tampering with ledger, audit, deletion records | append-only triggers; the **purge gate** allows a DELETE only for rows listed in a purging request of the same workspace | `test_db_p8.py`, `ST/test_deletion_residuals.py` |
| I | Cross-tenant read | forced RLS on every tenant table + `workspace_id` predicates in code; route walkers per phase | `ST/test_isolation.py`, `ST/test_p2..p7_isolation.py` (**SEC-01, AT-11**) |
| I | Pooled-connection reuse leaks a previous tenant | session variables set per transaction (`SET LOCAL`); reuse test | `tests/integration/test_db.py` |
| I | Secrets at rest | interaction payloads encrypted (keyring), content HMACs keyed, API keys peppered | `tests/integration/test_p3_*`, `test_p9_rotation.py` |
| D | Unbounded growth | retention schedules, quotas, `ops.rate_sweep` | `test_p8_settings.py`, `test_p9_shared.py` |
| E | SECURITY DEFINER abuse | every definer function pins `search_path`; hardening test enumerates them | `tests/integration/test_db_p8.py` |

### B4 Neo4j
| | Threat | Control | Evidence |
|---|---|---|---|
| I | Cross-tenant graph read | `group_id` derived from the authenticated workspace only; no client-supplied namespace; graph results re-checked against Postgres ACLs | `ST/test_p4_isolation.py`, `ST/test_fact_acl_leaks.py` |
| T | A rebuilt namespace overwrites live data | Flow F builds under parked uuids, one-transaction cutover, rollback | `tests/integration/test_p9_reindex.py`, FI-21..23 |
| I | Deleted data survives in the graph | deletion verifies each derivative; reapply replays the journal after a restore | `ST/test_deletion_residuals.py`, `test_p9_reapply.py` |
| D | Graph down | writes queue in the outbox; reads degrade (status `partial`, never fabricated) | FI-01..14, `docs/runbooks/graph-outage.md` |
| — | **Community Edition has no per-tenant database** | see **F-02** | ADR-015 |

### B5 Model router (egress)
| | Threat | Control | Evidence |
|---|---|---|---|
| I | Sensitive content leaves trusted infrastructure | classification × purpose × workspace egress policy evaluated before the gateway sends (**SEC-06**); `local_only` and `denied` never reach the transport | `ST/test_egress_matrix.py`, `test_p8_settings.py` |
| T | Prompt injection in sources | content is data: extraction output is validated, can at most create `proposed` facts/memories; no policy, role or ontology change | `ST/test_injection_corpus.py`, `ST/test_context_injection.py`, `eval-injection` (**SEC-04, AT-15**) |
| I | Model output echoes another tenant's text | one tenant per call; no cross-tenant prompt assembly | `ST/test_context_leaks.py` |
| D | Router slow or down | timeouts, retry with backoff, circuit; jobs wait; contexts degrade | FI-04..12, FI-20 (emulated) |
| S | A rogue router | only the configured URL is reachable from the `egress` network; mTLS or VPN is the deployment's job (**F-07**) | `docker-compose.prod.yml` |

### B6 Console
| | Threat | Control | Evidence |
|---|---|---|---|
| S | Session theft | access token in memory only; refresh cookie httpOnly, SameSite=Strict, Secure; CSRF-resistant (no cookie auth on mutating API routes) | `test_p9_oidc.py` |
| I | XSS | React escaping, no `dangerouslySetInnerHTML`, CSP without inline scripts, admin detail screens render text only | `make prod-smoke`, admin e2e |
| E | Open redirect on login | redirect URIs matched exactly against `OIDC_REDIRECT_URIS` | `test_p9_oidc.py` |

### B7 SDK / MCP
| | Threat | Control | Evidence |
|---|---|---|---|
| E | The MCP server becomes a confused deputy | it holds **no** platform credentials; each tool call forwards the caller's own key | `tests/contract/test_mcp.py`, ADR-040 |
| T | Tool input widens a request | tool schemas are the SDK request models with `additionalProperties: false` | `tests/contract/test_mcp.py` |
| R | Anonymous agent activity | every call is attributed to a principal; agents are registered, scoped, revocable (**SEC-03**) | `ST/test_revocation.py`, `test_p7_revocation.py` |

### B8 Backups
| | Threat | Control | Evidence |
|---|---|---|---|
| I | Backup theft | age-encrypted to recovery recipients; the recovery identity is not on the backup host; escrow encrypted to different recipients | `tools/backup/tests`, `make backup` |
| T | Tampered backup | manifest with per-file SHA-256, verified before restore | `make backup-verify` |
| I | A restored backup resurrects deleted data | the journal (outside the backups) is replayed (`kg deletions reapply`); rehearsal checks 0 residuals (**AT-18**) | `test_p9_reapply.py`, `make restore-rehearsal` |
| D | Backup missing when needed | nightly timer + weekly rehearsal; RTO/RPO measured and reported | `eval/reports/restore-*.md` |

### B9 Deletion journal
| | Threat | Control | Evidence |
|---|---|---|---|
| T | Line removed, edited, reordered | HMAC chain; `kg deletions journal` verifies; a removed key version breaks verification loudly | `tests/integration/test_p9_reapply.py`, `packages/governance/tests/test_rotation.py` |
| I | Journal discloses deleted content | ids and HMACs only, no text | `ST/test_deletion_residuals.py` |

### B10 Operators and secrets
| | Threat | Control | Evidence |
|---|---|---|---|
| S | Break-glass key abuse | human API keys work in production only while `ALLOW_BREAKGLASS_KEYS=true`; each use audited | `test_p9_oidc.py`, `make prod-smoke` |
| I | Secrets in env / images / logs | `*_FILE` secrets, scanners (gitleaks history + tree), log redaction and metadata-only telemetry | `make scan`, `ST/` sentinels |
| E | Long-lived secrets | rings with versions: JWT `kid`, pepper dual-verify with forced expiry, content-HMAC ring; runbook | `test_p9_rotation.py`, `docs/runbooks/rotate-secrets.md` |

## 3. Blueprint threat table, re-checked

| Threat (blueprint) | Protection built | Evidence | Status |
|---|---|---|---|
| Cross-workspace read | auth-derived namespace; RLS + predicates; graph `group_id` from the workspace | SEC-01, AT-11 (route walkers P2–P7, leak suites) | covered |
| Cross-agent memory leak | scope-level ACLs, no inheritance; ontology-driven subject ACLs | SEC-02, AT-10, `ST/test_memory_leaks.py`, `test_fact_acl_leaks.py` | covered |
| Prompt injection | content treated as data; validators; at most `proposed` | SEC-04, AT-15, injection corpus + eval | covered |
| Memory poisoning | authority classes, trusted-source model, review queue, consolidation validator | ADR-029/036/041/043, `eval-conflicts` | covered |
| Incorrect identity merge | strong identifiers, review, split/compensation | AT-03, ADR-028/042 | covered |
| Sensitive model egress | classification × purpose × workspace policy, decision log | SEC-06, egress matrix | covered; live router behaviour pending the live session (**F-07**) |
| Unbounded retained data | retention schedules, deletion lifecycle, backup windows | SEC-05, AT-16, ADR-044/047; backup window is an owner decision (DEC-07) | covered |
| Exposed audit payload | metadata-only telemetry, masked admin views, redacted traces | telemetry sentinels, `test_p6_traces.py` | covered |

## 4. Findings

Severity: Critical / High / Medium / Low. *Fixed* = fixed in phase 9 with the named change and test; *Accepted* = owner decision recorded in `docs/release/decisions.md`; *Open* = tracked with an owner.

| ID | Sev | Finding | Disposition |
|---|---|---|---|
| F-01 | High | **No rotation path for the JWT secret, the key pepper, or the content-HMAC secret.** A leaked secret was permanent. | **Fixed (WP6).** Rings, dual-verify, forced expiry, journal `kid`; `kg secrets …`; `test_p9_rotation.py`; runbook. |
| F-02 | Medium | **Graph tenancy is `group_id` on one Neo4j database** (Community Edition has no per-tenant database). A defect in the graph layer could cross tenants. | **Accepted for release; DEC-06 stays an *open production decision*** with options (per-tenant Neo4j instances, Enterprise multi-database), triggers (first regulated tenant, any cross-tenant graph finding) and an owner. Mitigated by double enforcement (graph results are re-checked against Postgres ACLs) and by the route walkers. |
| F-03 | High | **Per-process caches and limiters let a revocation, a settings change or a quota lag behind on other replicas, and gave N× the rate budget.** | **Fixed (WP7).** Postgres-backed limiter; LISTEN/NOTIFY invalidation with TTL fallback; `test_p9_shared.py` (sub-second propagation). |
| F-04 | Medium | **Containers ran with a writable root filesystem and default capabilities; no production deployment definition existed.** | **Fixed (WP10).** `docker-compose.prod.yml`: read-only root filesystems (one documented exception: Neo4j's vendor entrypoint writes its generated config; it still runs non-root with no capabilities), `no-new-privileges`, `cap_drop: ALL`, internal networks; secret files are 0444 in a 0700 directory so the non-root services can read the bind mount; proved by `make prod-smoke`. |
| F-05 | Medium | **No first-boot path in production** (`kg seed demo` refuses, correctly), so an operator would have been tempted to use the database directly. | **Fixed (WP10).** `kg workspaces create` (administrator + one break-glass key, printed once); test in `test_p9_hardening.py`. |
| F-06 | Medium | **CORS accepted a wildcard in production.** | **Fixed (WP6).** Production requires exact `https://` origins; `packages/common/tests/test_common.py`. |
| F-07 | Medium | **Trust in the model router's own behaviour is unverified until the live session runs** (the mock cannot prove classification handling, alias catalog, real latency). The transport to the router is HTTPS to the public hostname `models.oso.group` (decision 2026-10-19): calls and their data cross the public internet via Cloudflare, so the egress policy is the control on what may be sent. | **Open — owner action.** `make live-session` (WP1) is the evidence; put the router on a VPN or terminate TLS in front of it. Release is blocked on the live record for any model-affecting change (the gate). |
| F-08 | Low | **Deletion reapply cannot re-raise a text-residual exception** (the journal carries no names, by design). | **Accepted.** After a restore, text-residual exceptions on legal-hold subjects must be re-entered; documented in `docs/runbooks/backup-restore.md`. |
| F-09 | Low | **JWT validation depends on host clocks** (the mock cannot reproduce skew). | **Accepted.** NTP is a deployment prerequisite (runbook); leeway is 60 s. |
| F-10 | Low | **Per-principal RLS and a separate content-reader database role were considered and not adopted.** | **Accepted (ADR-048).** Workspace-level RLS plus application ACLs; the cost of per-principal session state on pooled connections outweighs the gain. Revisit with DEC-06. |
| F-11 | Low | **Backups need ~20 s of graph downtime** (offline dump). | **Accepted.** Scheduled at 02:30; read paths degrade, writes queue. A hot-backup option needs Enterprise. |
| F-12 | Low | **Third-party image findings (Postgres, Neo4j) are the vendors'**; they are not part of the blocking scan. | **Accepted.** `make scan-third-party` reports them; refresh runbook `upgrade-pin-dependencies`. |

No Critical findings. No High finding is open.

## 5. Residual risk the owner is asked to accept at sign-off

1. DEC-06 (graph tenancy) stays an open production decision (F-02).
2. The router transport and behaviour are verified only after the live session (F-07); a release candidate is not a production release until that record exists.
3. The accepted Low findings (F-08…F-12).
