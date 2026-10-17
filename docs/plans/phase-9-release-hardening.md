# Phase 9: Evaluation, Recoverability and Release Hardening — Detailed Implementation Plan

**Roadmap:** [ROADMAP.md](../../ROADMAP.md), Phase 9 (blueprint Slice 8).

**Blueprint inputs:**
- `05-delivery/02`: the release test matrix AT-01..AT-18, retrieval evaluation methodology, release gating.
- `05-delivery/03`: open decisions DEC-01..12 and the risk register.
- `03-runtime/05`: production topology, required metrics, reliability and recovery, cost controls, initial service-level targets, runbooks.
- `04-administration/02`: threat model and security tests SEC-01..06.
- `04-administration/01`: the Operations page.
- `01-architecture/03` Flow F: rebuild and ontology change, including re-ingest into a separate namespace and atomic cutover.
- `00-overview/01`: measures to establish in evaluation.

**Binding inputs from P0–P8:**
- Every ADR (011–047).
- `docs/architecture/p1..p8-*.md`, including all their "Deviations" sections.
- Items each phase explicitly handed to P9:

| Hand-over | From |
|---|---|
| Latency/size baselines on a fixed seed; ANN trigger | ADR-019, ADR-031, P5 perf, P6 `context-perf.json` |
| LISTEN/NOTIFY option for the outbox | ADR-025 |
| Content-reader DB role (option) | ADR-024, P3 |
| Per-principal RLS (option) | ADR-029, P5 |
| Shared rate limiter and quota state | P6, P7 |
| OIDC for human users | ADR-020, ADR-036 |
| Neo4j tenancy for production | ADR-015 §6 |
| Communities (`update_communities`) | P4 §13.7 |
| Admin-stack major versions | P1 deviation |
| Flow F full re-ingest and cutover | P8 §13.10 |
| `kg deletions reapply` and the restore contract | ADR-045, P8 |
| Egress-setting cache shared across processes | P8 |
| Resilience suite not re-run in P8 | P8 status |

**Status:** ready for a coding agent. **Two owner actions gate the phase:** WP0, which commits P5–P8, and WP1, the live session.

---

## 1. Objective

Turn a feature-complete, offline-verified platform into a **release candidate**. That means:
- quality is measured, not assumed;
- recovery is rehearsed;
- the security posture is reviewed;
- operations are visible;
- every open production decision is written down.

1. **Close the live gap.** Run the one live session that P4–P8 deferred, through a single orchestrated `make live-session`. It calibrates the token margin (TK-02) and the identity and memory similarity thresholds. It confirms or adjusts every proposed threshold, and moves ADR-012, ADR-013 and ADR-020 out of Provisional.
2. **A frozen evaluation suite** with separate, versioned, hash-locked datasets for:
   - ranking (context);
   - extraction;
   - conflicts and identity;
   - memory;
   - consolidation;
   - authorization (leakage);
   - injection.

   `make eval-gate` compares any model, router, prompt or ranker change with the release baseline and blocks regressions (blueprint: "test model/version changes against the frozen dataset before promotion").
3. **Failure injection and reconciliation.**
   - A complete failure matrix across Postgres, Neo4j, router/LLM, embeddings, the worker, the API, the network and the disk.
   - One `kg reconcile` command plus a scheduled job that compares the ledger with the graph and the memory store and repairs safe drift.
4. **Backup and restore (AT-18).**
   - Encrypted, scheduled backups of Postgres, Neo4j, configuration and keys, with retention.
   - A **restore rehearsal into a separate environment**, which replays the deletion journal (`kg deletions reapply`), reconciles, and verifies against a fixed checklist.
5. **Baselines (DEC-11).**
   - A load harness on fixed synthetic and Sarah datasets measures context p50/p95, ingestion freshness, extraction and memory cost per event, search latency, and storage growth.
   - The **owner** sets SLOs from those measurements.
6. **Security review.**
   - A threat review (STRIDE over the blueprint's threat table and every boundary built).
   - A release sweep of SEC-01..06.
   - Dependency, image and secret scanning.
   - Container hardening, security headers and secret rotation.
   - **OIDC for human users.**
   - A **shared rate limiter and shared cache invalidation**.
7. **Flow F: ontology reindex with atomic cutover.**
   - Re-ingest into a **separate graph namespace** with cost preview, evaluate it, and switch the workspace's active namespace in one step.
   - The old namespace is kept for rollback, then cleared.
8. **Operations console.**
   - Cost (by purpose, alias, day and workspace), latency, freshness, backlog, dead letters, reconciliation status, backup status, live-evaluation status, and alert state.
   - Replay with audit reason (already built), extended to reconcile repairs.
9. **Release readiness.**
   - All runbooks from `03-runtime/05` are complete.
   - Prometheus alert rules.
   - A production reference deployment (TLS, secrets, read-only containers).
   - SBOMs.
   - A release checklist.
   - The **open production decisions register**.

**Gate:**
- Every **strict blocker** passes in a release run:
  - AT-01, AT-08, AT-10, AT-11, AT-15, and the AT-16 read block;
  - leakage = 0, budget adherence = 100%, attribution = 100%;
  - zero deletion residuals;
  - every immutability trigger holds.
- **AT-01..AT-18** all pass, with AT-18 passing for the first time.
- The live session is complete and its thresholds confirmed.
- The DEC-11 baselines are recorded and the owner has set the SLOs.
- No unresolved high or critical findings from the threat review or the scanners.
- Every open production decision is documented with an owner.

---

## 2. What Phases 0–8 Taught Us (and how P9 applies it)

| # | Finding (source) | P9 decision |
|---|---|---|
| V1 | Every phase since P4 deferred its live exit, and they are now a single session (P4–P8 status) | **WP1 is first and blocking.** One `make live-session` target runs the preflight (router reachable, key not admin, scopes, cost estimate), then every live kit in order. It can be resumed (each step writes a result file), writes one consolidated report, and proposes the ADR and config updates as a reviewable diff. |
| V2 | P5–P8 are uncommitted; the working tree carries four phases (P8 memory, git status) | **WP0:** ask the owner to approve four commits (P5, P6, P7, P8). P9 changes go on top, one commit per WP group. This plan cannot finish without a committed base, because restore rehearsals and release tags need it. |
| V3 | Perf tests are slow; never run two pytest sessions against `kg_test`; don't recreate compose mid-run (P5–P7) | Load and baseline runs use a **separate compose project** (`kg-perf`) with its own ports and volumes. The restore rehearsal uses `kg-restore`. The suites never share a database with the dev stack. |
| V4 | Rate limiter, request quotas and the workspace egress cache are per process (P6–P8) | A **Postgres-backed shared limiter** (unlogged counter table with fixed windows) and **LISTEN/NOTIFY cache invalidation** for workspace settings, grants and revocation. No Redis or broker (blueprint: none until throughput warrants). |
| V5 | The deletion journal is the restore contract; `kg deletions reapply` was deferred (ADR-045, P8) | WP5 builds `reapply`. It is **idempotent, verifies the HMAC chain, and only removes data** (it never re-creates anything). The restore rehearsal fails if any journal entry after the backup time is not re-applied and verified. |
| V6 | Neo4j Community offers only an offline `neo4j-admin database dump`; the graph is a projection of Postgres for command facts, but episodes, mentions and summaries live only in Neo4j (P0, P4) | **Neo4j backup = an offline dump in a short maintenance window** (stop, dump, start), scheduled. Restore = load the dump, then reconcile against Postgres (the state-based projector repairs facts and entities), then re-run `graph_ingest` only for interactions newer than the dump (flagged, with a cost preview). **Rebuild from the ledger** (Flow F machinery) is the documented fallback when no usable dump exists. |
| V7 | Encrypted content and keys: the dev keyring is refused in production; payload key rotation exists; secrets must be ≥32 bytes (P1, P3) | Backups are **encrypted with `age`** (pinned image), using keys stored separately from the backups. The key-recovery procedure is part of the backup runbook. Rotation runbooks cover every secret, including the auth pepper (dual-pepper verification window) and the content HMAC secret (versioned, because idempotency and suppression HMACs depend on it). |
| V8 | Tenancy is a `group_id` on one Neo4j database (Community); stronger isolation needs Enterprise or one instance per tenant (ADR-015 §6) | Recorded as an **open production decision** (§11) with options and triggers. The new **graph namespace indirection** (Flow F) also makes a per-tenant instance a configuration change. |
| V9 | P5/P6 perf baselines: memory search p95 ≈ 18 ms at 50k; context build baseline is in `context-perf.json` (P5/P6) | The load harness reuses those datasets and adds ingestion freshness and cost. The **ANN trigger** (≥200k memories per workspace or p95 >100 ms) is checked against the measured curve. |
| V10 | Route walkers, leak suites, injection corpus and egress matrix exist per phase (P1–P8) | `make test-release` aggregates them into one **release sweep**, with a machine-readable report keyed by SEC/AT id, and fails on any strict blocker. |
| V11 | Admin stack majors are behind (React 18, Vite 6, Tailwind 3, …) (P1 deviation) | A bounded **dependency refresh** WP: upgrade to current supported majors only if the e2e suite stays green, otherwise record a dated deferral. Python pins are refreshed the same way, re-running `make spike-offline` first for Graphiti (ADR-011). |
| V12 | Communities are disabled until P9 evaluates them (P4 §13.7) | Evaluated in WP2 on the extraction and context datasets. Enable per workspace only if recall improves at acceptable cost; otherwise keep disabled and document. |
| V13 | Every review decision and setting change is a ledger command with audit (P4–P8) | Reconcile repairs, reindex cutovers, backup and restore actions, and secret rotations are audited operations. Reindex cutover is a ledger command. |

---

## 3. Scope

**In scope:**
- WP0 commits and WP1 live session (owner actions, tooling built here).
- The frozen evaluation suite and `eval-gate`.
- The failure matrix and unified reconciliation.
- Backup, restore and the rehearsal (`reapply`).
- The load harness and baselines.
- The threat review and release security sweep; scanners; hardening; OIDC; the shared limiter and cache invalidation; secret rotation.
- Flow F reindex and cutover.
- The Operations console; alert rules; production reference deployment; SBOMs; runbooks; release checklist; decision register.

**Out of scope:**
- Kubernetes manifests (the reference is Docker Compose plus Caddy, a documented path).
- Managed-cloud specifics beyond documented notes.
- A TypeScript SDK.
- Multi-region.
- Real LLM agent examples, which are P10.

---

## 4. Workstreams and Package Touch Points

```text
eval/                      # datasets/<suite>/<version>/ (manifest.json with sha256), runners, gate.py, baselines/
eval/load/                 # asyncio load driver (httpx), scenarios, synthetic data generator
tools/live_session/        # orchestrator for the single live session (preflight, steps, report, ADR/config diff)
tools/backup/              # backup/restore scripts + age image wrapper; restore rehearsal driver
deploy/compose/            # docker-compose.prod.yml (Caddy TLS, secrets, read-only), compose.perf.yml, compose.restore.yml
deploy/observability/      # prometheus rules, alertmanager example, grafana dashboard JSON
packages/governance/       # oidc.py (JWKS verify, user mapping, console login), limiter_pg.py (shared limiter), notify.py (LISTEN/NOTIFY)
packages/knowledge/        # reconcile.py (unified), reindex.py (namespace build + cutover)
packages/graph/            # namespace indirection (ScopedGraph resolves the active namespace from Postgres, cached + NOTIFY)
packages/governance/deletion/  # reapply.py
apps/admin/                # Operations v2, OIDC login, reindex UI, backups/eval status
docs/                      # security/threat-model.md, release/checklist.md, release/decisions.md, runbooks/*
```

import-linter contracts are unchanged. Tools under `tools/` and `eval/` call the HTTP API or the CLI; only the backup driver uses `pg_dump` and `neo4j-admin` directly.

---

## 5. Work Packages

### WP0: Commit base (owner approval)
- Ask the owner to approve committing P5, P6, P7 and P8 as four commits, with the message format of the existing ones.
- Then tag `v0.8.0-offline`.
- **Exit:** a clean working tree, and the tag exists.
- If approval is withheld, P9 work continues, but WP5 (restore rehearsal from a tagged build) and WP12 (release candidate) are blocked.

### WP1: Live session (owner action; tooling here)
**`tools/live_session`** behind `make live-session`.

1. **Preflight:**
   - the router at `OSO_ROUTER_LIVE_URL` is reachable from host and container;
   - `kg-api` and `kg-worker` keys are present and **not admin** (no `x-gateway-provider` header in responses);
   - the key's `maxClassification` covers the test data;
   - the aliases exist (LV-01);
   - **cost estimate** (sum of the P0–P8 live kits from recorded token counts × `compat/pricing.yaml`), which must be confirmed before running;
   - Neo4j and Postgres are up.
2. **Steps**, each writing `eval/reports/live/<date>/<step>.json`:

   | Step | What it runs |
   |---|---|
   | (a) | `verify-router`: LV-01..10, G-05, GR-11..13, **TK-02** |
   | (b) | `eval-extraction-live` |
   | (c) | `eval-memory-live` |
   | (d) | `eval-context-live` |
   | (e) | `eval-consolidation-live` |
   | (f) | `eval-simulation-live` |
   | (g) | latency profile capture: router p50/p95 per alias, fed to WP4's mock latency model |
   | (h) | fixture recording (LV-10) refresh |

   Every step is resumable with `--from <step>`.
3. **Calibration:**
   - the token margin = the smallest margin at which the platform's count ≥ the router's observed count for 100% of samples, plus 2 percentage points of headroom;
   - `IDENTITY_COS_MIN` and `MEMORY_VEC_MIN_COSINE` from the related vs unrelated cosine distributions (the threshold that best separates them, rounded down);
   - Graphiti `SEMAPHORE_LIMIT` from GR-13.
4. **Output:**
   - one consolidated report, `eval/reports/live/<date>/summary.md`, with a pass/fail per proposed threshold from P4–P8;
   - a **proposed diff** (not auto-applied) to `compat/model-purposes.yaml` (status accepted), `.env.example` thresholds, and ADR-012/013/020 status sections;
   - the owner reviews it, and `tools/live_session apply` writes it.
5. `make eval-context` is re-run with the calibrated margin, and budget adherence must stay at 100%.

**Exit:** the owner has applied or adjusted the diff, ADR-012/013/020 are Accepted, and all P4–P8 live thresholds are confirmed. Thresholds that fail are recorded as release blockers or as documented exceptions the owner accepts.

### WP2: Frozen evaluation suite and the eval gate
**Datasets:** `eval/datasets/<suite>/<version>/`, each with `manifest.json` (case ids, sha256 per file, generator seed, ontology version, data provenance: synthetic only):

| Suite | Content |
|---|---|
| `context` | P6's 16 cases plus new cases: multi-subject, project-only, deleted-subject (P8), corrected-fact (P8), consolidated-memory (P8), quota and ACL denial |
| `extraction` | P4's 7 scenarios plus ~20 synthetic conversations covering dates, aliases, negation, hypotheticals, multilingual, long turns |
| `conflicts` | Authority × overlap matrix cases and identity cases (same-name people, aliases, external-id conflicts) |
| `memory` | P5's 9 plus selection-policy edge cases (trivial, escalation, procedural, unmapped subject) |
| `consolidation` | P8 cases plus distinct-claim pairs |
| `authorization` | Generated probes over every route × principal kind × workspace × ACL mode × frozen and deleted state |
| `injection` | The P8 corpus (45) plus new payloads from the threat review |

- **Metrics per suite** follow the blueprint methodology: recall@k, precision/irrelevant inclusion, temporal correctness, attribution, unsupported-statement rate, leakage, budget adherence, freshness, cost and latency.
- **`make eval-all`** runs every suite against the mock (deterministic) and writes `eval/reports/<date>/all.json`.
- **`make eval-gate`** compares the current run with `eval/baselines/release.json`:
  - strict metrics must equal their required value;
  - proposed metrics may not regress beyond tolerance (default: relevance −0.02, cost +10%, p95 +15%);
  - changes to `compat/model-purposes.yaml`, `compat/pricing.yaml`, prompts (`packages/*/prompts`), ranker or packer versions, or Graphiti/model pins **require a live gate run** (`make eval-gate-live`). A CI check detects such diffs and refuses a merge without a recorded gate report for that commit.
- **Communities evaluation (V12):** run extraction and context suites with `update_communities=True` in a perf tenant. Record the recall and cost deltas, and decide per §12.

### WP3: Failure matrix and unified reconciliation
**`make test-resilience`** runs FI-01..14 (P3–P8), plus:

| ID | Scenario | Expected |
|---|---|---|
| FI-15 | Router fully down during a mixed burst (ingest, extraction, memory, context) | Model stages back off and stay visible. Context is `partial` (vector and summarization unavailable) or 503 when partial results are not allowed. Nothing is lost; all work completes after recovery |
| FI-16 | Embedding alias down, chat up | Memories stay `pending` with lexical search reported; graph extraction retries; recovery |
| FI-17 | Router slow (p95 × 10 latency injection) | Per-source timeouts hold; context stays within the latency envelope as `partial`; no thread or connection exhaustion |
| FI-18 | Postgres failover / restart during a deletion purge | The purge resumes; verification passes; nothing is purged twice |
| FI-19 | Neo4j restart during a reindex build | The build resumes, or is cleaned up and restarted; the active namespace is untouched |
| FI-20 | Disk full on Neo4j | Graph stages fail retryably; alert fires; recovery |
| FI-21 | API instance killed mid-request (two API replicas behind the proxy) | Idempotent clients retry safely; no duplicate events or commands |
| FI-22 | Clock skew (container clock +10 min) | Leases, expiry and quotas still use the DB clock; no premature expiry |
| FI-23 | Router returns a malformed body or unknown error code | Config-terminal handling; operator alert; no infinite retry |

**`kg reconcile --workspace <slug> [--scope all|graph|memory|ledger|deletions] [--repair]`** combines the existing `graph reconcile`, `interactions reconcile` and memory checks with new cross-store checks. Each check lists drift by class with ids only, and `--repair` applies only **safe repairs**:

| Check | Repair |
|---|---|
| Every Postgres entity and fact equals its node or edge (state-based) | Re-projection |
| Every `graph_episodes` row has an episode, and vice versa | Re-enqueue `graph_ingest` (with cost preview) or remove orphan episodes in the group |
| Every interaction has its pinned stages; no stage runs without a live lease | Re-enqueue the stage |
| Every active memory has a current-version embedding | Re-enqueue `memory_embed` |
| Every completed deletion has zero residuals (re-run of the verifier) | Open a `deletion_exception` review |
| No `context_items` row names a tombstoned id | Keyed delete |
| Outbox jobs have no missing subjects | Cancel the job |

- **Scheduled job:** `reconcile_scan` runs nightly per workspace in check-only mode and emits `reconcile_drift{class}` metrics. Any non-zero class raises an alert.
- Repairs are audited operations (`reconcile.repaired`, with class and count).

### WP4: Baselines and load harness (DEC-11)
- **`eval/load`** is an asyncio driver (httpx, no new heavy dependency) with scenarios:
  - **steady:** a mix of 70% context builds, 20% interaction ingests and 10% memory proposals at a target RPS;
  - **burst ingest:** 10× RPS for 2 minutes;
  - **search-heavy;**
  - **long conversations** (token-heavy normalisation and extraction).
- **Datasets:** `eval/load/data` generator (seeded) builds workspaces of size S (1k entities / 5k facts / 10k memories), M (5k/25k/50k) and L (20k/100k/200k) across 20 workspaces, plus the Sarah world.
- **Environment:** the `kg-perf` compose project. A **mock router with a latency model** taken from WP1(g)'s live profile, so baselines reflect realistic model latency without spending money. A short **live baseline** (5 minutes steady, small dataset) is part of the live session when the owner agrees to the cost.
- **Measurements:**
  - context build p50/p95/p99 by profile and size;
  - interaction accepted → `indexed` freshness p50/p95 (blueprint "write-to-search freshness");
  - extraction and memory cost per event (USD, tokens);
  - memory and graph search latency;
  - worker throughput per stage;
  - Postgres and Neo4j storage growth per 1k events;
  - API error rate.

  Output: `eval/reports/baseline-<date>.{json,md}` with charts.
- **Proposed SLOs** derived from the measurements (for example "context p95 ≤ X ms at M size", "freshness p95 ≤ Y s", "99% of events indexed or needs_review", "zero silent loss"). **The owner sets the values** (DEC-11), and they are recorded in `docs/release/decisions.md` and the alert rules.
- **ANN trigger check (V9):** extrapolate memory search p95 to the trigger and document whether ANN is needed before L size. With the L dataset it is expected not to be.

### WP5: Backup, restore and the rehearsal (AT-18)
- **Backups**, run by `tools/backup` in a pinned backup container:
  - **Postgres:** `pg_dump -Fc` per database, nightly. Optional WAL archiving / PITR for managed Postgres is a documented note.
  - **Neo4j:** an offline `neo4j-admin database dump` in a maintenance window (configurable, default nightly, about 1–2 min of graph downtime). Model stages back off during it (FI-15 behaviour); the API stays up, graph search is `partial`, and Postgres current facts are unaffected.
  - **Configuration:** `compat/`, ontology exports, workspace settings export.
  - **Keys:** the payload keyring, `CONTENT_HMAC_SECRET`, auth pepper and JWT secrets, exported **separately** to a key-escrow location (never inside the data backup).
  - Each artifact is encrypted with `age` to configured recipients, gets a SHA-256 manifest, and is written to `BACKUP_DIR` (an object-store path in production).
  - **Retention:** `BACKUP_RETENTION_DAYS` from settings (P8 receipts use it), enforced by pruning with audit.
- **`kg deletions reapply --since <ts> [--dry-run]`:**
  - reads the out-of-band journal and verifies the HMAC chain;
  - for each entry after the backup time, re-runs the deletion purge plan from the journal's ids and HMACs (freeze, purge, verify) on the restored data;
  - is idempotent (tombstones are checked);
  - never re-creates data;
  - writes a reapply receipt.
- **`make restore-rehearsal`** (the `kg-restore` compose project, separate ports and volumes):
  1. restore the latest Postgres and Neo4j backups;
  2. bring the API and worker up read-only (`MAINTENANCE_MODE=read_only`: writes 503);
  3. `kg deletions reapply --since <backup time>` (V5);
  4. `kg reconcile --scope all --repair` for safe classes; newer interactions are listed for re-ingest, with a cost preview, and not executed by default;
  5. **verification checklist:**
     - row counts versus backup manifests;
     - the residual-scan suite for every journal entry (0 residuals);
     - the `eval-context` mock subset on the Sarah world matching its pre-backup report (status and items);
     - pooled-reuse and RLS checks;
     - every trigger present;
     - every ADR-required index present;
     - Graphiti constraints present;
  6. report `eval/reports/restore-<date>.md` with time-to-restore (RTO) and data loss window (RPO).
- **AT-18:** a release run requires a passing rehearsal from **the release candidate's own backups**. The measured RTO and RPO are proposed to the owner alongside the SLOs.

### WP6: Security review and release sweep
- **Threat review** (`docs/security/threat-model.md`):
  - STRIDE over each trust boundary (API, worker, router egress, Neo4j, Postgres, admin console, SDK/MCP, backups, journal, keys);
  - the blueprint threat table, re-checked against what was built, with the controls and tests that cover each row;
  - new findings, each with severity, owner and remediation;
  - **every high or critical finding is fixed in P9 or explicitly accepted by the owner.**
- **`make test-release`** runs the release sweep, aggregating:
  - the P1–P8 route walkers;
  - the leak suites (memory, context, facts/ACL, deletion read-block and residuals);
  - the injection corpus;
  - the egress matrix;
  - revocation;
  - trigger gates;
  - pooled reuse;
  - telemetry sentinels.

  It runs against **two API replicas** (shared-limiter mode) and writes a report keyed by SEC-01..06 and AT ids. Any strict failure fails the run.
- **Scanners** (pinned in `p9_additions`; run by `make scan` and CI):
  - `pip-audit` and `npm audit --omit=dev` for dependencies;
  - **Trivy** for images and filesystem (OS packages, misconfigurations);
  - **gitleaks** for secrets, over the history and working tree;
  - ruff `S` (bandit) rules already on, kept clean;
  - **SBOM** (CycloneDX) for each image and package set.

  High and critical vulnerabilities block unless a documented, dated exception exists.
- **Hardening:**
  - containers non-root and read-only root filesystem (tmpfs where needed), `no-new-privileges`, dropped capabilities;
  - the API sets security headers for the admin (CSP with no inline scripts, `frame-ancestors 'none'`, `X-Content-Type-Options`, `Referrer-Policy`) and refuses insecure cookies;
  - an explicit production CORS allow-list;
  - `/docs` and metrics off on public interfaces in production (P1);
  - a request body limit per route class.
- **Secret rotation** (`docs/runbooks/rotate-secrets.md`, with CLI support):
  - JWT signing keys move to a **kid-based keyring** (old kids verify until their tokens expire);
  - the **auth pepper gets dual-verify** (old and new pepper during a window; keys are re-hashed on next use and expiry is forced after the window);
  - the content HMAC secret is **versioned** (stored HMACs carry their version; the suppression and idempotency checks try every active version);
  - payload keys use the existing re-encrypt;
  - router keys are re-issued through the router admin;
  - the Neo4j and Postgres passwords;
  - each rotation is audited.
- **Two options from earlier phases, decided** (§12, decision 6):
  - per-principal RLS is **not adopted**, with the rationale and the compensating tests;
  - a content-reader DB role is **not adopted** (encryption at rest plus audited reveal).

### WP7: OIDC for human users, shared limiter, shared cache invalidation
- **OIDC** (ADR-020 path; ADR-036 deferred human identity here):
  - **API:** verify RS256/ES256 tokens against the IdP's JWKS (cached, with key rollover), pin algorithms, require `iss`, `aud` and `exp`;
  - **mapping:** a configured claim (`kg_workspace`) or a group → workspace and role table (`oidc_group_mappings`, admin-managed) gives a `user` principal; one is **auto-provisioned on first login** (audited) and never given more than the mapping says;
  - **console:** authorization code + PKCE (no client secret in the browser), with the session as an in-memory access token plus a refresh through the API (httpOnly, `SameSite=strict` cookie for the refresh token only);
  - API-key login stays for development and break-glass (`ENV=production` requires an explicit `ALLOW_BREAKGLASS_KEYS=true` and audits each use);
  - agents and apps keep API keys and JWTs (P7);
  - **tests:** a local test IdP (an in-repo JWKS signer) covering valid, expired, wrong-audience, wrong-issuer, alg-confusion and unmapped-group tokens, and kid rollover; a Keycloak and Entra configuration guide in `docs/runbooks/oidc-setup.md`.
- **Shared limiter (V4):**
  - an unlogged table `rate_counters(key, window_start, count)` with `INSERT … ON CONFLICT DO UPDATE … RETURNING count` per request;
  - keys are per principal per minute, per workspace concurrency (an advisory-lock-based semaphore) and per expensive method;
  - daily quotas keep using the durable tables (P7);
  - old windows are swept;
  - **tests:** two API replicas enforce one shared budget; Postgres being slow fails open or closed by setting (default **closed for context builds, open for health**).
- **Cache invalidation:** `pg_notify('kg_invalidate', <workspace>:<kind>)` on changes to settings, grants, revocations, the active ontology and the active graph namespace. Every API and worker process listens and drops its cache entry. The TTL caches remain as a fallback.
  - **Tests:** an egress-narrowing change takes effect on the other replica within 1 s; a revocation takes effect immediately (already true from DB checks; now cache-consistent too).

### WP8: Flow F reindex with atomic cutover
- **Namespace indirection:**
  - new table `workspace_graph_namespaces(workspace_id, namespace, status: active|building|retired, created_at, activated_at)`;
  - the current namespace (`ws-<uuid>`) is backfilled as active;
  - `ScopedGraph` resolves the active namespace through `GroupId.for_workspace(ctx, namespace)` (cached and NOTIFY-invalidated); a building namespace is reachable only through a `ReindexGraph` handle;
  - ADR-015's adapter rules are unchanged.
- **`POST /v1/ontologies/versions/{id}/reindex`** (admin; preview-then-confirm with a cost estimate over all interactions):
  1. create the namespace `ws-<uuid>-r<n>` (building);
  2. re-project every Postgres entity and command fact (no LLM);
  3. replay `graph_ingest` for every interaction under the **target** ontology version into the building namespace. These are separate reindex jobs with `concurrency_key=graph:{ws}:r<n>`; they do not block the live namespace;
  4. run the **reconciler and the frozen evaluation subset** against the building namespace, for a report that compares it with the active namespace;
  5. **cutover** is a ledger command (`graph.namespace_activate`, audited) that flips the active pointer in one transaction and sends NOTIFY;
  6. the old namespace is retained (status `retired`) for `REINDEX_RETENTION_DAYS`, then cleared with `clear_data`.
- Extracted facts created during the build are written to Postgres with `namespace` provenance and promoted at cutover (`facts.namespace`), so status and evidence stay authoritative. Live extraction continues into the active namespace and is **replayed into the building one** before cutover (a catch-up phase until the backlog is 0).
- **Rollback:** re-activate the retired namespace (one command) within the retention window.
- **Tests:**
  - reindex the Sarah world from v1.4.0 to v1.5.0, then cutover; context results are equal or better per the report;
  - rollback restores the previous results;
  - reads never see a building namespace;
  - FI-19;
  - cross-workspace isolation;
  - the deletion journal applies to both namespaces during the build.

### WP9: Operations console v2 and observability
- **Operations page:**
  - **Cost:** model spend by purpose, alias, workspace and day (`model_calls` estimates), tokens, calls, and the top consumers (principals) with links to usage.
  - **Latency:** context build p50/p95 by profile, stage latencies, router latency by alias (from traces and `model_calls`).
  - **Freshness:** accepted → `indexed` lag p50/p95 per stage; watermarks.
  - **Queues:** backlog, oldest pending, dead letters (existing, with replay and audit reason), lease reclaims.
  - **Reconciliation:** last scan per workspace, drift classes, a repair button (safe classes, audited).
  - **Backups:** last successful backup per store, size, age, last restore rehearsal, and RTO/RPO.
  - **Live evaluation:** last live session date, per-suite status, and threshold drift.
  - **Alerts:** current firing alerts (from Alertmanager, if configured, or the platform's own checks).

  Server-side aggregates come from Postgres (no direct Prometheus access needed from the console).
- **`deploy/observability/`:**
  - **Prometheus rules:**
    - `context_budget_violations_total > 0`;
    - `deletion_residuals_total > 0`;
    - `outbox_dead_letters > 0` for 15 m;
    - `outbox_oldest_pending_age_seconds > SLO`;
    - router error rate;
    - `reconcile_drift > 0`;
    - backup age greater than 26 h;
    - `quota_exceeded` spikes;
    - leakage-test failures in a scheduled synthetic check;
  - an Alertmanager example;
  - a Grafana dashboard JSON;
  - a **synthetic canary** (a scheduled job) builds a context in a canary workspace every 5 minutes and checks status and budget.
- **Tests:** Vitest for the panels; Playwright for operator flows (repair drift, replay a dead letter with a reason, view backup status); a rules unit test (`promtool test rules`).

### WP10: Production reference deployment and dependency refresh
- `deploy/compose/docker-compose.prod.yml`:
  - Caddy TLS in front of the API, admin static files and MCP;
  - Docker secrets for every credential (`*_FILE` support added where missing);
  - read-only containers;
  - Postgres and Neo4j on an internal network only;
  - the router reached at its host or VPN address;
  - backup sidecar schedule;
  - health checks;
  - two API replicas for the shared-limiter path.
- `docs/runbooks/deploy-production.md`: VM sizing from the WP4 baselines, the first-boot checklist (production secrets refused if they are dev defaults), OIDC configuration, backup targets, and alert routing.
- **Dependency refresh (V11):**
  - Python pins refreshed (Graphiti only after `make spike-offline` plus the P4/P8 graph suites pass, per ADR-011);
  - admin majors upgraded if e2e stays green, otherwise a dated deferral;
  - `compat/versions.lock.yaml` gets `p9_additions` and `refreshed_on`.

### WP11: Runbooks, release checklist, decision register
**Runbooks**, all of which `03-runtime/05` requires. Each is validated by a dry run in the rehearsal environment:
- `startup` (exists; updated for production);
- **`upgrade-pin-dependencies`** (new);
- **`graph-outage`** (new);
- **`llm-outage`** (new: router down, budget exceeded, alias missing);
- `stuck-job-replay` (exists);
- `identity-correction` (exists);
- **`reindex-ontology`** (new, WP8);
- `subject-deletion` (exists);
- **`backup-restore`** (new, WP5, including key recovery);
- **`rotate-secrets`** (new, WP6).

**`docs/release/checklist.md`** has one checkbox per strict blocker and AT/SEC id, plus links to the latest reports:
- live session, eval gate, release sweep, resilience, restore rehearsal, baselines with owner-set SLOs, scanners clean, threat review sign-off, decision register;
- the release is tagged `v1.0.0-rc1` when every box is checked.

**`docs/release/decisions.md`**, the **open production decisions register**, holds DEC-01..12 with their final status:

| DEC | Expected status after P9 |
|---|---|
| DEC-01 pins | Closed (ADR-011, refreshed) |
| DEC-02 providers | Closed (ADR-012 accepted) |
| DEC-03 identity provider | Closed (OIDC, WP7) |
| DEC-04 correcting edges | Closed (ADR-018/041) |
| DEC-05 runtime types | Closed (ADR-014) |
| DEC-06 graph tenancy | **Open production decision** (options, triggers, owner) |
| DEC-07 retention/backup windows | Owner-set values recorded |
| DEC-08 embeddings | Closed (ADR-013 accepted) |
| DEC-09 procedural fields | Closed (ADR-030/038) |
| DEC-10 UI framework | Closed (React/Cytoscape, versions) |
| DEC-11 SLOs | Owner-set from WP4 |
| DEC-12 bitemporal guarantee | Valid-time and record-time metadata at MVP; a formal guarantee stays out of scope (documented) |

It also records communities (V12), ANN (V9), the per-principal RLS and content-reader decisions (WP6), and the LISTEN/NOTIFY outbox option (kept as polling unless the WP4 numbers say otherwise).

### WP12: Release candidate run
**`make release-candidate`** runs, from a clean checkout of the tagged commit, in this order:
1. `reset up seed demo-p1…demo-p8`;
2. `check`;
3. `test-release`;
4. `test-resilience` (FI-01..23);
5. `test-e2e`;
6. `eval-all` + `eval-gate`;
7. `scan`;
8. backup → `restore-rehearsal`;
9. load baseline (M size, mock latency model).

It writes `docs/release/rc-report-<date>.md`, linking every report. The owner signs the checklist.

---

## 6. Data and API Changes

| Change | Notes |
|---|---|
| Migration `0009_release` (`down_revision="0008"`) | `rate_counters` (unlogged), `oidc_group_mappings`, `workspace_graph_namespaces`, `facts.namespace`, `reindex_runs`, `reconcile_runs`, `backup_runs` (status metadata only), `secret_versions` (version metadata, never secrets), and NOTIFY triggers on settings, grants, principals, workspace ontology and namespaces. RLS where tenant-scoped; pooled-reuse test extended |
| API | `POST /v1/ontologies/versions/{id}/reindex` (preview/confirm), `GET /v1/reindex-runs/{id}`, `POST /v1/graph/namespaces/{id}/activate` (cutover and rollback), `GET /v1/operations/{cost,latency,freshness,reconcile,backups,live-eval}`, `POST /v1/operations/reconcile` (repair, audited), `/v1/auth/oidc/*` (login, callback, refresh, logout), `GET/POST/DELETE /v1/oidc-group-mappings` |
| Actions (explicit matrix additions) | `operations.cost_read` (admin, operator), `reindex.run` (admin), `oidc.manage` (admin). `operations.repair` is reused for reconcile repairs |
| Audit | `reconcile.repaired`, `reindex.started|cutover|rolled_back`, `backup.completed|failed|pruned`, `restore.rehearsed`, `deletions.reapplied`, `secret.rotated`, `oidc.user_provisioned`, `breakglass.used` |

---

## 7. Sequencing

```
WP0 (owner) ─► WP1 (owner run; tooling first) ─────────────────────────────┐
WP2 ─► WP3 ─┬─► WP5 ─────────────────────────┐                            │
            ├─► WP4 (needs WP1(g) latency)    ├─► WP9 ─► WP10 ─► WP11 ─► WP12
WP6 ─► WP7 ─┘   WP8 (after WP7 NOTIFY) ───────┘
```

- **Parallel tracks:**
  - **A:** WP2 → WP3 → WP5.
  - **B:** WP6 → WP7 → WP8.
  - **C:** WP1 tooling (owner runs it when ready).
  - **D:** WP4 after the live latency profile, or with a provisional profile from the P0 docs if WP1 is late (the report is then marked provisional).
- WP12 needs everything, including the owner sign-offs.

## 8. Gate Mapping

| Requirement | Evidence |
|---|---|
| Strict blockers AT-01, AT-08, AT-10, AT-11, AT-15, AT-16 read block | `make test-release` report; eval gate strict metrics |
| AT-02..AT-07, AT-09, AT-12..AT-14, AT-16 (purge), AT-17 | Release sweep plus eval suites (each AT id mapped in the report) |
| **AT-18** backup/restore consistency | WP5 rehearsal report (journal re-applied, residuals 0, reconcile clean) |
| SEC-01..06 | Release sweep (two replicas), scanners, threat review |
| DEC-11 baselines | WP4 report; owner-set SLOs in the decision register |
| Model/version changes tested against frozen data | `eval-gate` + `eval-gate-live` CI rule |
| Reliability: no silent loss; visible actionable states | FI-01..23 + reconcile scans |
| Runbooks complete | WP11 list, each with a rehearsal dry run |
| Open production decisions documented | `docs/release/decisions.md` |

## 9. Exit Criteria

- [ ] WP0 commits exist and the tree is clean.
- [ ] The live session report has been applied, ADR-012/013/020 are Accepted, and every P4–P8 threshold is confirmed or an owner-accepted exception.
- [ ] `make release-candidate` passes end to end. The RC report is written and the checklist signed by the owner.
- [ ] Scanners report no unresolved high or critical findings. The threat review is signed off.
- [ ] SLOs are set by the owner; RTO and RPO are measured and accepted.
- [ ] Tag `v1.0.0-rc1`.

## 10. Risks and Mitigations

| Risk | Mitigation |
|---|---|
| The live session keeps slipping | One command, preflight cost estimate, resumable steps. P9 tracks C/D proceed on provisional data, clearly labelled |
| Neo4j offline dump downtime | Short and scheduled; the API stays up with `partial` graph results; graph rebuild from the ledger is the fallback |
| Restored backups resurrect deleted data | Journal reapply plus residual verification are mandatory rehearsal steps |
| Shared limiter adds Postgres load | Unlogged table, one upsert per request, window sweeps; measured in WP4; fail-closed only for expensive methods |
| Reindex doubles graph size temporarily | Retention window, `clear_data` of retired namespaces, storage alert |
| Dependency refresh breaks Graphiti behaviour | ADR-011 procedure (spike-offline and graph suites first) or a dated deferral |
| OIDC misconfiguration locks out admins | Audited break-glass keys, mapping tests, a runbook |

## 11. Rollback

- Each WP is a separate commit.
- The shared limiter and OIDC are behind settings (`LIMITER_BACKEND=memory|postgres`, `AUTH_OIDC_ENABLED`).
- Namespace indirection falls back to the default namespace if the table is empty.
- `kg db downgrade 0008` is guarded and refuses while a reindex is building.

## 12. Decisions (adopted per the user's instruction to go with the recommendations)

1. **The live session is a hard release gate**, run once through `make live-session`. Calibrations come back as a reviewable diff, never applied automatically.
2. **Neo4j backup is an offline dump in a short maintenance window**, followed by Postgres-driven reconciliation on restore. Rebuild from the ledger is the fallback.
3. **Backups are encrypted with `age`**, with keys escrowed separately from the data. Backup retention follows the P8 settings, so deletion receipts stay truthful.
4. **The shared limiter and cache invalidation use Postgres** (unlogged counters plus LISTEN/NOTIFY), with no Redis or broker.
5. **OIDC is implemented for human users** (API verification plus console PKCE login and group-to-role mapping). API keys stay for agents, apps, development and audited break-glass.
6. **Per-principal RLS and a separate content-reader DB role are not adopted.** In their place: predicate-in-SQL with leak suites, encryption at rest, the audited reveal, and RLS for workspaces.
7. **Flow F is built** (namespace indirection, reindex, catch-up, atomic cutover, rollback), because P8 deferred it and tenant isolation options depend on the same indirection.
8. **SLOs come from measured baselines and are set by the owner.** The plan proposes values; it does not invent guarantees.
9. **Scanners are pip-audit, npm audit, Trivy and gitleaks, plus CycloneDX SBOMs.** High and critical findings block releases unless an exception is documented and dated.
10. **The production reference is Docker Compose with Caddy TLS** and two API replicas. Kubernetes is out of scope.
11. **Communities stay disabled** unless the WP2 evaluation shows a recall gain worth the cost.
12. **Graph tenancy (DEC-06) stays an open production decision**, with documented options and triggers. Namespace indirection keeps the switch cheap.
13. **P5–P8 are committed before P9 work is merged** (owner approval in WP0).
