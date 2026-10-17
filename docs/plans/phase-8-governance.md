# Phase 8: Governance (Corrections, Consolidation, Deletion) — Detailed Implementation Plan

**Roadmap:** [ROADMAP.md](../../ROADMAP.md), Phase 8 (blueprint Slice 7).

**Blueprint inputs:**
- `01-architecture/03` Flows D, E and F: correction or identity merge, deletion, rebuild and ontology change.
- `02-domain/02`: merge and split, the fact state machine, the impact query pattern.
- `02-domain/03`: corrections vs rewriting history, retention policy.
- `02-domain/04`: consolidation policy, MEM-06 and MEM-07.
- `02-domain/01`: schema lifecycle, ONT-05.
- `04-administration/01`: Review Queue decisions, Memory Explorer actions, Settings, ADM-03.
- `04-administration/02`: deletion and correction, prompt and instruction hierarchy, audit, SEC-04, SEC-05, SEC-06.
- `03-runtime/05`: backups and the deletion lifecycle.
- `05-delivery/02`: AT-13, AT-15, AT-16, AT-17, plus reference scenario steps 9 and 10.
- `06-reference/01`: the "Michael manages Sarah" mistaken extraction.

**Binding inputs from P0–P7:**

| Source | What it settles |
|---|---|
| ADR-018 | Correction projection; the deletion procedure (a)–(g) |
| ADR-021 | State-based projector; Postgres-only `local_only` |
| ADR-023 | Succession vs supersession |
| ADR-024 | Encrypted contents; key rotation |
| ADR-025 | Outbox, `concurrency_key`, effectively-once |
| ADR-026/027/028 | Graphiti behaviour; reconcile-then-restore; identity and merge |
| ADR-029..031 | Memory scopes and search |
| ADR-032..035 | Context: no stored packages; re-materialization; `context_items` keyed rows |
| ADR-036..040 | Registration, subject ACLs (`SubjectFilter` required), publish, revocation |
| Architecture notes | `docs/architecture/p1..p7-*.md`, including every "Deviations" section and the P7 hooks |

**Status:** ready for a coding agent. Build and test offline against the mock router, with strict scenarios for the consolidation and simulation prompts.

> **Release still blocked** on the single live session (P6 WP11). P8 adds two LLM-backed paths, consolidation synthesis and ontology simulation. Both are evaluated offline here, and their live checks are folded into the same session (§7 WP12).

---

## 1. Objective

Make the platform **correctable, forgettable and governable** without breaking provenance or isolation.

1. **Corrections (Flow D, AT-13, ADM-03)** are audited ledger commands for facts, entities, attributes and memories:
   - reject, supersede, end or expire, correct an attribute, rename;
   - **split** an identity, the compensating command for a P4 merge;
   - each has an **impact preview** listing the dependent memories, facts, review items, recent contexts and grants.

   A correction updates Postgres, which is authoritative. It then re-projects the graph, flags dependent memories, and leaves context to re-materialize, since there is no context cache. Original evidence is never rewritten.
2. **Memory consolidation (MEM-06)** is an idempotent, policy-gated worker. It has two modes:
   - **reinforce** (deterministic): exact and near duplicates within the same scope set are folded into one memory. No new text is written, and evidence is merged.
   - **synthesize** (LLM): a summary that cites its sources, validated like P6's `summarized` mode, which **always goes to review**.

   Distinct claims are never collapsed, and a consideration is never escalated into a decision.
3. **Deletion orchestration (Flow E, SEC-05, AT-16, MEM-07)** is `POST /v1/deletion-requests` for a subject, an interaction, a memory, a principal's data or a whole workspace.
   - **Freeze**: reads are blocked in the request transaction.
   - **Enumerate** derivatives across Postgres, pgvector, Neo4j/Graphiti, traces, jobs and idempotency records.
   - **Purge or redact**, with resumable, idempotent per-store steps.
   - **Verify**: id scans plus residual-text scans.
   - **Receipt**: counts per store, retained items with their reasons, and the backup expiry date.
   - **Legal holds**, **re-collection suppression** and an **out-of-band deletion journal** so that P9 restores can re-apply deletions.
4. **Ontology simulation and versioning (ONT-05, AT-17, Flow F preview).**
   - A draft runs against sample interactions in an isolated **staging graph namespace**, which is cleaned up automatically. It produces an extraction diff against the active version, plus impact counts and a cost estimate.
   - Publishing a second version pins new jobs to it while old provenance stays on the first version, already true since P3 and now tested end to end.
5. **Retention and settings.** Workspace Settings cover:
   - retention windows for raw content, Neo4j episode content, traces, memories and model-call logs;
   - an **egress narrowing** override (it can only be stricter than the platform policy);
   - default quotas.

   Each is enforced by retention jobs.
6. **Containment suites:**
   - **SEC-04 / AT-15**: an injection corpus through every ingress path.
   - **SEC-06**: an egress matrix over classification, consent, disclosure and purpose.
7. **Admin:**
   - Review Queue v2: corrections, splits, deletion exceptions, consolidation proposals, and quarantine (from P7);
   - correction flows from the Graph and Memory Explorers;
   - deletion request tracking with receipts;
   - a **privacy audit view** (reveals, evidence reads, private reads, act-as, debug reads, deletions, egress decisions);
   - Settings.

**Demo (§8):**
1. A reviewer rejects the mistaken "Michael manages Sarah" fact, and the next context reflects it.
2. An admin corrects a mistaken employer (AT-13).
3. A wrongly linked identity is split.
4. Near-duplicate memories are consolidated; distinct claims stay separate.
5. A deletion request for Michael hides him everywhere immediately. The purge then completes, verification passes, and a receipt is issued.
6. A legal hold blocks a deletion, with a visible exception.
7. The ontology v1.5.0 simulation previews MANAGES before publishing.
8. The injection and egress suites pass.

**Gate:**
- MEM-06, MEM-07; ADM-03; AT-13, AT-15, AT-16, AT-17; SEC-04, SEC-05, SEC-06; ONT-05 completed.
- Every P1–P7 gate stays green.
- **Strict:** an immediate read block, and verification of each derivative with zero residuals.

---

## 2. What Phases 0–7 Taught Us (and how P8 applies it)

| # | Finding (source) | P8 decision |
|---|---|---|
| U1 | `remove_episode` leaves derived summaries, dangling `episodes`/`entity_edges` refs and orphans, and can drop edges a later episode supports (GR-08, ADR-018) | The **graph purger** implements ADR-018 (a)–(g) exactly, using **platform knowledge** (Postgres `fact_evidence`, `entity_mentions`, `graph_episodes`) to decide what to delete or re-attach, not Graphiti's owner rule. Summaries of surviving neighbours that may cite the subject are **cleared** (deterministic). Graphiti regenerates them on the next episode, and the API already labels them derived. |
| U2 | Graphiti MERGEs on uuid; uniqueness constraints and the per-workspace graph lock serialize writers (P3/P4) | Graph purge and correction projection run under `concurrency_key="graph:{ws}"` plus the advisory `GraphLock`. A purge never races extraction or projection. |
| U3 | Postgres is authoritative; every read filters by status; re-materialization never resurfaces changed records (ADR-018/034) | **Freeze = status changes inside one transaction**: `deletion_pending` on entities, facts, memories and interactions. Existing filters then hide everything at once (SEC-05). One new rule is added to `SubjectFilter`: endpoints must not be `deletion_pending`. |
| U4 | `SubjectFilter` is a required argument; a hidden id gives the same 404 as a missing one (P7) | Deletion and correction requests are authorized **through** `SubjectFilter`: you can't correct or delete what you can't see. After the freeze, a deleted id gives the same 404 as a never-existing id. |
| U5 | Interactions, commands, activations and audit rows are immutable via triggers and column grants (P2/P3) | One **controlled exception**. The immutability triggers allow `DELETE`, or redaction of named columns, **only** when the row is listed in `deletion_items` for a request in state `purging`, and the transaction has `set_config('app.purge_request_id', <id>, true)`. The app role can't delete anything without an approved, frozen deletion plan. **`audit_log` stays fully immutable**: it holds only ids and codes (P1). |
| U6 | Raw and normalized interaction content is encrypted (AAD-bound); Neo4j keeps episode content when `GRAPH_STORE_EPISODE_CONTENT=true` (P3/P4) | Content rows are **deleted**, and ciphertext copies in backups expire with the backup window (no crypto-shredding in v1, §13). Neo4j episode content goes with `remove_episode` and the verification scan. |
| U7 | Contexts are never stored; `context_items` rows name records (P6 note: "P8 must purge `context_items` rows that name a deleted record") | Purge does a **keyed delete** of `context_items` by `(item_kind, item_id)`. Debug snapshots for traces that included a deleted item are deleted. Traces keep metadata only and have their subject ids redacted. |
| U8 | Memory dedupe hash includes scopes; distinct claims must stay distinct; the escalation heuristic exists (P5) | Consolidation never crosses scope sets, reuses the P5 escalation and unmapped-subject checks, and validates synthesis like P6 `summarized` (no new names or dates, every claim cites sources). |
| U9 | A compromised revoke quarantines what came from submissions, not app commands; P7 deferred erasure of a revoked principal's data | Deletion kind **`principal_data`**: the principal's working and agent-private memories, its unpublished proposals, and optionally (`include_submissions`) its submitted interactions and their sole-sourced derivatives. Pairs naturally with `compromised`. |
| U10 | Review decisions are ledger commands; the decision set is closed and extended explicitly; `CommandSummary` lists every type (P4/P5/P7) | New commands and decisions are added explicitly to the literal, its test and the decision enums. |
| U11 | Tests share `kg_test` and Neo4j; don't recreate compose mid-run; perf tests are slow; Playwright leaves demo state changed (P2–P7) | P8 suites use their own tenants (`tenant_governance` fixture). Deletion tests assert on their own group. `make test-e2e` re-seeds after deletion specs. The demo deletes **Michael** (re-creatable) and leaves Sarah intact for later demos. |
| U12 | Mock scenario fixtures in strict mode keep LLM paths deterministic (P4–P6) | New strict scenarios: consolidation synthesis (good, invented-date, merged-distinct-claims, escalation) and simulation extractions under the draft ontology. |
| U13 | Retention and expiry run through delayed outbox jobs or `ops.*` purge functions returning counts (P5/P6) | Retention is a periodic worker task calling `ops.retention_candidates()`, which returns workspace ids and counts only. Purges run as per-workspace tenant jobs. |
| U14 | P5–P7 are not committed; git is now initialised with P0–P4 (memory, git log) | **WP0:** with the user's approval, commit P5, P6 and P7 as three commits before starting P8, so P8's large migration has a clean base. |

---

## 3. Scope

**In scope:**
- Correction commands and impact preview; identity split; memory corrections from the Explorer.
- The consolidation worker (reinforce and synthesize).
- Deletion requests (all five kinds), legal holds, suppressions, tombstones, the deletion journal, purgers, verifier and receipt.
- Ontology simulation (staging namespace) and AT-17 tests.
- Workspace settings and retention jobs.
- Injection and egress suites.
- Review Queue v2, deletion and privacy audit console pages, Settings.
- `demo-p8`.

**Out of scope:**
- Full re-ingest of history into a staging namespace with atomic cutover (Flow F). Simulation covers the preview; the full rebuild becomes a P9 runbook built on the same staging machinery.
- Backup and restore, including re-applying the journal (P9, AT-18).
- Crypto-shredding.
- Automated ontology migrations (blueprint non-goal).

---

## 4. Package Layout and Boundaries

```text
packages/governance/  kg_governance
├── deletion/        # NEW
│   ├── requests.py      # DeletionService: request (authorize + legal-hold check + freeze), cancel, status, receipt
│   ├── planner.py       # enumerate derivatives -> deletion_items (pure rules over loaded rows + store adapters' listings)
│   ├── purgers.py       # PostgresPurger, VectorPurger, TracePurger, JobPurger, IdempotencyPurger (SQL through ops/trigger gate)
│   ├── verify.py        # id scans + residual-text scans (Postgres, Neo4j via kg_knowledge port)
│   ├── journal.py       # out-of-band deletion journal writer (ids + HMACs only)
│   └── holds.py         # legal holds + suppressions
├── settings.py      # NEW workspace settings (retention, egress narrowing, quota defaults), versioned + audited
packages/knowledge/   kg_knowledge
├── corrections.py   # NEW correction commands + impact preview; split
├── graph_purge.py   # NEW GraphPurger (ScopedGraph ops for ADR-018 a–g) — the only deletion code that touches the graph
├── simulation.py    # NEW ontology simulation in a staging namespace
packages/memory/      kg_memory
├── consolidate.py   # NEW MemoryConsolidateJob (reinforce | synthesize) + validation
├── corrections.py   # NEW memory correction helpers (redact/supersede from deletion + correction paths)
packages/graph/       kg_graph   # ScopedGraph: remove_episode_scoped, delete_nodes/edges_by_uuid, clear_summaries, prune_dangling_refs,
                                 # orphan_sweep, fulltext_scan, staging namespaces (StagingGraph built from WorkspaceContext + run id)
```

**import-linter rules:**
- `kg_governance.deletion` reaches the graph and memory stores only through **ports**. `kg_knowledge` provides `GraphPurgePort` and `KnowledgePurgePort`; `kg_memory` provides `MemoryPurgePort`. The ports are registered by the composition root, so `kg_governance` never imports `kg_knowledge`, `kg_memory` or `kg_graph`.
- `graphiti_core` stays inside `kg_graph`.

---

## 5. Domain Design

### 5.1 Corrections (Flow D)

Endpoint: `POST /v1/entities/{id}/corrections` (blueprint), plus `POST /v1/relationships/{fact_id}/corrections` and `POST /v1/memories/{id}/corrections`. Each correction is a ledger command (`correction.*`) that needs a **reason code** (closed enum), optional reason text (stored on the correction row, never in audit), `If-Match` and an Idempotency-Key.

| Correction | Effect (Postgres, authoritative) | Propagation |
|---|---|---|
| `fact.reject` | Fact → `rejected` (`decided_at`, `decision_code`); evidence kept | Projector expires the edge. Dependent memories (`related_fact_ids` or evidence → this fact) → `disputed` + `memory_conflict` review (`source_fact_rejected`). Context re-materializes it as `withdrawn` |
| `fact.supersede` | A new fact with authority `admin_asserted` and `source_type=correction` (new value or endpoints); old fact → `superseded` (`superseded_at`=now, `succeeded_by`) | Projector expires the old edge and creates the new one. Dependents as above |
| `fact.end` | Sets `valid_to` (relationship expiry; succession, not supersession, ADR-023) | Re-projection |
| `fact.restore` | `rejected` / `disputed` → `active` (reviewer), with reason | Re-projection; reopens dependents' reviews |
| `entity.correct_attribute` | New attribute value with lineage (entity revision + `attribute_corrections` row); sensitive attributes stay Postgres-only | Projector restores node attributes |
| `entity.rename` | `display_name` change; old name kept as alias (KGR-01: id stable) | Node name re-projected |
| `entity.split` | **Compensating command for merge** (§5.2) | §5.2 |
| `memory.correct` | Supersede with new content (P5 supersede), or `reject` / `expire` / `dispute`, with a reason | Embeddings: the new row is embedded and the old one leaves search (status). Context: re-materialize |

**Corrections are not governed by the authority ranking.** A correction by a reviewer or admin may supersede an `authoritative_system` fact. It is a deliberate, audited override, unlike a conflict (ADR-027). Every override of an authoritative fact also creates an `authoritative_override` notice in the review log, so the source system can be fixed.

**Impact preview:** `GET /v1/{entities|relationships|memories}/{id}/corrections/preview?kind=…` is read-only and authorized through `SubjectFilter`. It returns:
- dependent memories (counts by status, ids visible to the caller);
- facts derived from the same episodes;
- open review items;
- contexts in the last `CONTEXT_TRACE_RETENTION_DAYS` that included the item (count, admin only);
- grants that reference the entity (split and rename);
- the expected graph operations.

Flow D's "sees sources and dependents" step is this preview.

**Original evidence is never rewritten** (blueprint). A correction appends; deletion (§5.4) is the only way to remove.

### 5.2 Identity split (ADR-028 compensation)

`POST /v1/entities/{id}/corrections` `{kind: "split", new_entity: {display_name, type?, aliases?}, move: {mention_ids[], fact_ids[], memory_subject_ids?[], external_ids?[]}, reason_code}`:

1. Create the new entity (`origin=correction`).
2. Re-point the selected mentions, facts (the endpoint on this entity), memory subjects and external ids.
3. Remove the merged alias if it belonged to the split-off identity.
4. Record the lineage `split_from`.
5. Add an `identity_non_matches` pair so the two are not re-suggested.

**Grants** on the original entity stay where they are. The new entity has no grants: it starts conservative (P7). The response lists the principals that held grants on the original, so an admin can decide.

**Graph:** the projector re-creates moved edges, keeping their uuids, between the new endpoints (P4 endpoint-change path). Moved `MENTIONS` edges are re-created by `merge_nodes`' inverse (`move_mentions`).

Optional `reextract: true` re-runs `graph_ingest` (replay with a new generation) for interactions whose mentions moved.

### 5.3 Memory consolidation (MEM-06)

**Trigger:** a delayed outbox job `memory_consolidate` per `(workspace, subject, scope-set hash)`. It is enqueued when a memory write leaves at least `consolidation_min_group` active memories of a type for that key. Dedupe by key, with a delay of `consolidation_debounce_minutes`. The ontology `MemoryPolicy` gains:
- `consolidation: disabled|reinforce|synthesize` (default `reinforce`);
- `consolidation_min_group`: 3;
- `consolidation_auto_supersede`: true for reinforce, and always false for synthesize;
- `near_duplicate_jaccard`: 0.9;
- `near_duplicate_cosine`: data-set by live evaluation.

**Grouping** follows the blueprint:
1. Same workspace, **identical scope set**, same type, same subject set.
2. Optional topic.
3. Time buckets for episodic memories (`occurred_at` within `episodic_window_days`).
4. Never across classifications above the group's minimum ceiling.

**Reinforce** (deterministic, auto):
- Exact duplicates (same normalized content) and near duplicates (token Jaccard ≥ 0.9 **and** cosine ≥ threshold) with **no assertion conflict**.
- The survivor is the highest authority, then the earliest `recorded_at`. Others → `superseded` with a `consolidated_from` lineage.
- Evidence rows are merged into the survivor (the independent evidence count rises). The survivor's text is unchanged, so no re-embedding is needed.

**Synthesize** (LLM, proposals only):
- Purpose `memory_consolidation`, alias `oso/general` (structured). The schema is `ConsolidationProposal{groups:[{source_memory_ids[], summary, statements:[{text, source_memory_ids[]}], keep_separate:[{memory_ids[], reason}]}]}`.
- **Validation:**
  - every statement cites at least one source memory in the group;
  - no names, dates or numbers that are not in the sources (vocabulary check);
  - the P5 escalation heuristic finds no new decision verbs;
  - sources with **different assertion objects** (or contradicting polarity) may not share a statement (*"dislikes commute" and "wants remote work" stay separate*, blueprint).
- A valid result becomes a `proposed` consolidated memory (`origin=consolidated`) with lineage, plus a `consolidation_proposal` review item. Approving it supersedes the sources. **Evidence of the sources is kept by reference**, and event evidence is never deleted.
- An invalid output is dropped with reason codes (metric), with no write.

**Idempotency:** the job stores its output under `hash(sorted source ids + revisions + policy version)`. A re-run with the same inputs does nothing.

### 5.4 Deletion (Flow E)

**Kinds and scope:**

| Kind | Target | What is removed |
|---|---|---|
| `subject` | An entity (any type; `subject_scoped` types are typical) | The entity, every fact touching it, its mentions, attribute claims and grants, memories whose **only** subjects are it, and **interactions linked to it** (subject_refs, accepted mentions) together with their contents, episodes and their **sole-sourced** derivatives. Memories with other subjects that also name it → `deletion_exception` review (redact or delete; §5.4.4) |
| `interaction` | One event | Envelope (to a tombstone), contents, stages and episode; derivatives whose **only** evidence is this interaction; evidence rows elsewhere |
| `memory` | One memory and its lineage descendants created only from it | Memory, scopes, subjects, evidence, embeddings, lineage rows |
| `principal_data` | A principal (usually revoked) | Its working and agent-private memories, unpublished proposals, and optionally its submissions (+ sole-sourced derivatives) |
| `workspace` | The whole tenant | Everything in Postgres for the workspace, `clear_data([group])` in Neo4j (ADR-015 §5), keys revoked. Workspace → `deleted` (a new status). Needs a **cooling-off** period (§5.4.6) |

**Who may request:**
- `workspace_admin` for every kind.
- **Trusted-source apps** (`trusted_source=true`, `deletion.request_subject`) for `subject` and `interaction`, attesting an end-subject request. A trusted app may only target subjects it can see and interactions it submitted.
- Reviewers may request `memory` deletion.

#### 5.4.1 Lifecycle

```
received ─► frozen ─► planned ─► purging ─► verifying ─► completed | completed_with_exceptions
   │           └─► blocked_legal_hold (no purge; frozen stays; exception review)       └─► failed (retryable; operator)
   └─► cancelled (workspace kind during cooling-off only)
```

**1. Request (synchronous, one tenant transaction):**
- Authorize through `SubjectFilter` (U4). An invisible target gives a 404 that looks like a missing one.
- Check legal holds. A hold → `blocked_legal_hold`, still **frozen** (reads blocked), plus a `deletion_exception` review item.
- **Freeze:**
  - the target and its first-order derivatives become `deletion_pending` (entity; facts touching it; memories with it as their only subject or from its interactions; linked interactions);
  - grants on the target are disabled;
  - pending jobs for those subjects are cancelled (`cancelled_by_deletion`);
  - an encrypted **deletion manifest** is written (§5.4.3);
  - the audit entry is `deletion.requested`;
  - the response is `202 {request_id, status: frozen, status_url}`.
- **SEC-05:** from this commit on, every read surface treats the items as non-existent (U3). The next context build excludes them.

**2. Plan** (job `deletion_plan`):
- Enumerate every derivative into `deletion_items(store, item_kind, item_id, action: delete|redact|retain, reason)`, using:
  - Postgres links (`fact_evidence`, `memory_evidence`, `memory_subjects`, `entity_mentions`, `graph_episodes`, `review_items`, `attribute_claims`, `context_items`, `context_debug_snapshots`, `idempotency_records` by response references, `outbox_jobs` / `interaction_stages`, `principal_scopes`, `commands` with payloads naming the subject, `identity_non_matches`, `entity_external_ids`);
  - Neo4j listings (episodes, nodes and edges by uuid; neighbour nodes whose summaries may cite the subject);
  - **residual-text candidates**: the manifest names, aliases and external ids are searched in memory content, fact text, review rationale text, other entities' display names and aliases, and Neo4j summaries and fact text. Hits that are not already scheduled → `redact` (structured fields) or a `deletion_exception` review (free text such as other subjects' memories).
- **Multi-source rule:** a fact or memory about **another** subject is retained if it has independent evidence after the removal. If it is left with **no** evidence, it is deleted as unsupported (invariant 2).

**3. Purge** (jobs per store, `concurrency_key` per store; the graph uses `graph:{ws}`):
- **PostgresPurger:** within `set_config('app.purge_request_id')` (U5), deletes or redacts the listed rows in dependency order:
  - context items, then debug snapshots;
  - idempotency records;
  - memory embeddings, then memory rows;
  - facts and evidence, then mentions and claims, then entities (to tombstones);
  - interaction contents, then stages, then envelopes (to tombstones);
  - **command payloads redacted** (payload replaced by `{"redacted": true, "hash": <original hash>}`; command row kept for ledger continuity);
  - review rationale text redacted;
  - grants removed;
  - outbox job payloads cleared, then status `cancelled`.

  Each item is marked `done` as it goes. A re-run skips `done` items.
- **VectorPurger:** `memory_embeddings` for the listed memories (all versions).
- **GraphPurger** (ADR-018, under the lock):
  - (b) `remove_episode` for each listed episode uuid;
  - delete listed entity nodes and fact edges by uuid;
  - (c) **clear summaries** of neighbour nodes listed for redaction;
  - (d) re-project the surviving facts that the platform still holds, which repairs collateral edge loss (state-based projector);
  - (e) prune dangling `episodes` / `entity_edges` uuids;
  - (f) orphan sweep scoped to the group.
- **TracePurger:** redacts listed subject ids from `context_traces.request` (replaced with `"redacted"`); `context_items` rows were already deleted by the Postgres purger.

**4. Verify** (job `deletion_verify`):
- **Id scans:** no row in any Postgres table, and no node, edge or episode in Neo4j, references a deleted id, except tombstones and the audit ids (allowed: ids carry no content).
- **Residual-text scans:** manifest names, aliases and external ids are absent from Postgres text columns (memory content and summary, `fact_text`, entity names and aliases, review text, command payloads) and from Neo4j full-text indices (node name and summary, edge fact, episode content), within the workspace and group.
- Any residual → status `completed_with_exceptions`, with each residual listed by location (no content), plus a `deletion_exception` review item.

**5. Receipt:**
- Stored in `deletion_requests.receipt` and returned by the status endpoint. It has:
  - counts per store (deleted, redacted, retained) and a reason per retained class;
  - verification results;
  - `backup_copies_expire_by` (= completion + `BACKUP_RETENTION_DAYS`, from settings);
  - legal-hold notes;
  - journal reference.
- The manifest is **deleted** at completion.
- Audit: `deletion.completed`, with counts only.

#### 5.4.2 Read block (SEC-05)

The statuses `deletion_pending` (entities, facts, memories, interactions) and `deleted` (workspace) are invisible to every read:
- the existing status filters, where `deletion_pending` is not in any include set;
- the new `SubjectFilter` endpoint rule;
- `MemorySearch` status predicate;
- context sources;
- graph search joins (a Postgres join drops frozen facts);
- the console;
- the SDK and MCP (through the API).

The blocked-read test suite (WP7) runs **between freeze and purge**, with the purge jobs paused using a fault point.

#### 5.4.3 Manifest, tombstones, suppression, journal

- **Manifest** (`deletion_manifests`): names, aliases, external ids and episode uuids needed by the planner and verifier. It is encrypted with the payload cipher (AAD `workspace|request|manifest`) and deleted at completion.
- **Tombstones** (`deletion_tombstones`): `(workspace_id, kind, id, request_id, deleted_at)`, with no content. Used for:
  - 404 parity (a deleted id answers like a missing one);
  - idempotency replays of old requests (`410 gone_deleted` for the requester's own replay);
  - P9 restore re-application.
- **Suppression** (`deletion_suppressions`): HMACs (`CONTENT_HMAC_SECRET`) of the deleted subject's **external ids**. A later `entity.create` or interaction subject ref with a suppressed external id → `409 subject_suppressed`. Admins can lift a suppression (audited), for example when the subject returns with consent. Names are **not** suppressed, because names are not identities (KGR-02).
- **Journal:** an append-only JSON-lines file, outside the database and its backups (`DELETION_JOURNAL_DIR`), with `{request_id, workspace_id, kind, completed_at, ids[], suppression_hmacs[]}`. It holds no content. **P9's restore runbook replays it** (`kg deletions reapply --since <backup time>`), so a restored older backup forgets again (AT-18 groundwork). Each line has an HMAC chain so tampering is detectable.

#### 5.4.4 Deletion exceptions (review)

Item type `deletion_exception`, with reasons:
- `legal_hold`;
- `shared_memory_mentions_subject` (another subject's memory names the deleted subject);
- `residual_text`;
- `verification_failed`.

| Decision | Applies to | Effect |
|---|---|---|
| `redact_text` | Memory or text mentions | The reviewer supplies the replacement text; the memory is superseded through a redaction lineage, and the original is deleted by the request |
| `delete_item` | Any listed item | The item is deleted under the request |
| `retain_with_reason` | Allowed only for `legal_hold` | The item is retained; the receipt records it |
| `retry_verification` | `verification_failed` | Verification is re-run |

#### 5.4.5 Legal holds

`legal_holds(workspace_id, scope_kind: subject|interaction|workspace, target_id, reason_code, note, created_by, released_at)`. Only workspace admins can create or release one, and both are audited.

A held target can be frozen but not purged:
- the deletion request is `blocked_legal_hold`;
- the receipt explains the restriction (blueprint: "clearly communicate restriction");
- releasing the hold resumes the request.

#### 5.4.6 Workspace deletion

- **Request:** requires typing the workspace slug, and puts the workspace in `suspended` at once, so its authentication stops (P1).
- **Cooling-off:** `WORKSPACE_DELETION_COOLING_HOURS`, default 24. The request can be cancelled during this period, and cancelling restores the workspace to `active`.
- **Purge:** all tenant rows go through the purge gate (the trigger checks `kind=workspace` for that workspace), and Neo4j runs `clear_data([group])`. The receipt goes to `platform_deletion_receipts` (no RLS, written and read only through `ops` functions), so it survives the tenant's removal.
- `other_demo` isolation stays intact (tested).

### 5.5 Ontology simulation (ONT-05) and versioning (AT-17)

- `POST /v1/ontologies/versions/{draft_id}/simulate` `{interaction_ids[] ≤ 20 | sample: {n, since}, budget_usd?}`:
  - **Preview first:** the token and cost estimate from stored `token_estimate` and P4 per-episode costs. `confirm=true` is required to run.
  - A **staging namespace** `group_id = "ws-<uuid>-sim-<run>"` (a `StagingGraph` built only from a `WorkspaceContext` plus a run id, so the adapter rules hold) is created on the shared Neo4j database.
  - Each sampled interaction is decrypted and extracted under the **draft's** compiled types, with egress per the interaction's classification.
  - Results are reconciled into a **report**: facts by type and status, new types used, facts the draft drops compared with the active version (from Postgres), identity outcomes, plus the P2 diff and impact counts.
  - The staging group is `clear_data`-ed at the end, or by a delayed job after 1 h if the run dies. Nothing is written to the workspace's real tables. The report is stored in `ontology_simulations` with counts and ids only.
- **AT-17:** publishing v1.5.0 means new interactions pin v1.5.0, while existing facts, commands, episodes and memories keep `ontology_version_id` = v1.4.0. Reads mix both correctly, and a replay with `--repin` moves one interaction (audited). This is tested end to end.

### 5.6 Workspace settings and retention

The `workspace_settings` table is versioned (each change is a ledger command `settings.update`, audited). Settings:

| Setting | Default | Enforcement |
|---|---|---|
| `retention.raw_content_days` | null (keep) | Retention job deletes the `raw` content rows after N days (the normalized form stays for replay unless `normalized_content_days` is set). Provenance fidelity loss is shown on the interaction ("raw content expired") |
| `retention.normalized_content_days` | null | Same, for the normalized form. Replay of affected interactions then becomes impossible (flagged) |
| `retention.episode_content_days` | null | Sets Neo4j `EpisodicNode.content` to "" after N days |
| `retention.context_trace_days` | 30 (P6) | Existing purge |
| `retention.model_calls_days` | 90 | Purge |
| `retention.memory_archive_after_days` | From the ontology `MemoryPolicy` | Expires memories not confirmed for N days (status `expired`, evidence kept) |
| `egress.denied_classifications` | ∅ | **Narrows** the platform `compat/model-purposes.yaml` egress (union of denials), and can never widen it. Enforced in `RouterTransport` via the egress context (workspace id → cached settings) |
| `quotas.defaults` | Platform | Defaults for new principals |
| `backup_retention_days` | 30 | Used by receipts (P9 makes backups honour it) |

- **Retention runs** as a periodic worker task (`ops.retention_candidates()` returns workspace ids and counts) followed by per-workspace tenant jobs.
- Retention deletions go through the same purge gate, as a system-generated deletion request of kind `retention` with a receipt. Expiry is therefore audited and verifiable like any deletion.

### 5.7 Containment suites

- **SEC-04 / AT-15:** a **corpus** (`tests/fixtures/injection/*.yaml`, about 40 payloads covering instruction override, role spoofing, delimiter and envelope breaking, tool-call bait, exfiltration requests, policy- or scope-changing text, fake citations, unicode tricks) is pushed through **every ingress**:
  - interactions (each kind), entity attributes (commands);
  - memory saves and proposals, agent findings;
  - review rationale, ontology descriptions;
  - context tasks;
  - MCP tool arguments.

  Assertions:
  - no change to policy, roles, grants, scopes, ontology, settings or classification;
  - context output keeps every payload inside an item boundary;
  - summarized and consolidation outputs that echo instructions fail validation;
  - no data from another subject or workspace appears in any output;
  - the sentinel and telemetry checks still pass.
- **SEC-06 egress matrix:** classification (5) × consent (2) × disclosure (2) × workspace narrowing (2) × purpose (extraction, memory, compression, consolidation, simulation, embedding). A spy on the router transport asserts **zero** calls wherever any layer denies. The `model_calls` egress-denied rows are visible in the privacy audit view.

### 5.8 Privacy audit view

`GET /v1/governance/privacy-audit` (admin) returns a filtered timeline from `audit_log` plus `model_calls` aggregates. Every row holds ids and codes only:
- content reveals; evidence reads; private memory reads; debug reads; act-as builds;
- deletion requests and receipts; legal holds; suppressions lifted;
- grants changed;
- egress decisions (counts by purpose, classification and outcome);
- authoritative overrides (corrections).

The console renders it with filters and CSV export.

---

## 6. Data, API, Authorization

### 6.1 Migration `0008_governance` (`down_revision="0007"`)

| Change | Details |
|---|---|
| Status enums | `entities.status`, `facts.status`, `curated_memories.status` add `deletion_pending`. `interactions.processing_status` already has it. `workspaces.status` adds `deleted` |
| **Immutability triggers** | `interactions`, `interaction_contents`, `commands`, `ontology_*`, `job_attempts`, `facts` (content columns), `curated_memories` (content), `review_items` (rationale): allow DELETE or redaction **only** when `current_setting('app.purge_request_id', true)` names a `deletion_requests` row in state `purging` for this workspace **and** a matching `deletion_items` row exists (`store='postgres'`, item id, action). `audit_log` is unchanged (always immutable) |
| `deletion_requests` | `id`, `workspace_id`, `kind`, `target_id`, `requested_by`, `on_behalf_of_hmac`, `reason_code`, `legal_basis`, `status`, `include_submissions`, timestamps per state, `receipt jsonb`, `journal_ref`, `revision` |
| `deletion_items` | `request_id`, `workspace_id`, `store`, `item_kind`, `item_id`, `action`, `reason`, `status` (`pending\|done\|failed\|retained`), `attempts`; PK `(request_id, store, item_kind, item_id)` |
| `deletion_manifests` | `request_id` PK, `kid`, `nonce`, `ciphertext`, `created_at` (deleted at completion) |
| `deletion_tombstones` | PK `(workspace_id, kind, id)`, `request_id`, `deleted_at` |
| `deletion_suppressions` | `(workspace_id, kind, value_hmac)` unique, `request_id`, `lifted_by`, `lifted_at` |
| `legal_holds` | §5.4.5 |
| `platform_deletion_receipts` | No RLS. `kg_app` has no grants; access only through `ops.write_platform_receipt` / `ops.read_platform_receipt` |
| `corrections` | `id`, `workspace_id`, `command_id`, `target_kind`, `target_id`, `kind`, `reason_code`, `reason_text` (tenant data), `created_by`, `created_at` |
| `attribute_corrections` | Entity attribute lineage |
| `entity_lineage` | `split_from`, `merged_into` (backfilled from P4 merges) |
| `consolidation_runs` | `id`, `workspace_id`, `key_hash`, `mode`, `input_hash`, `outcome`, `proposal_memory_id`, `created_at` |
| `ontology_simulations` | `id`, `workspace_id`, `draft_version_id`, `staging_group`, `status`, `sample_ids`, `report jsonb` (counts and ids), `cost_estimate`, `cost_actual`, timestamps |
| `workspace_settings` | `workspace_id`, `version`, `settings jsonb` (schema-validated), `updated_by`, `updated_at`; the latest version wins |
| `review_items` | `item_type` adds `deletion_exception`, `consolidation_proposal`, `correction_request`, `authoritative_override` |
| Functions | `ops.retention_candidates()`, `ops.write_platform_receipt()`, `ops.read_platform_receipt()`, `ops.purge_workspace_rows(request)` (workspace kind; checks the state) |

- RLS (`ENABLE`/`FORCE`) on every tenant table; the pooled-reuse test is extended.
- **Trigger tests are release blockers:** without a purging request the app role cannot delete; a purging request cannot delete unlisted rows; another workspace's request cannot unlock this workspace's rows.
- **Downgrade:** guarded. It refuses while requests are not terminal.

### 6.2 Endpoints

| Method | Path | Action | Notes |
|---|---|---|---|
| POST | `/v1/entities/{id}/corrections` | `correction.apply` | Kinds per §5.1 (including split). `If-Match`, Idempotency-Key |
| POST | `/v1/relationships/{id}/corrections` | `correction.apply` | `fact.reject`, `fact.supersede`, `fact.end`, `fact.restore` |
| POST | `/v1/memories/{id}/corrections` | `memory.curate` | `memory.correct` (supersede, reject, expire, dispute) |
| GET | `/v1/{entities\|relationships\|memories}/{id}/corrections/preview` | `correction.apply` (memories: `memory.curate`) | Impact preview |
| GET | `/v1/corrections` | `correction.read` | History (ids, kinds, reasons; text only for reviewers and admins) |
| POST | `/v1/deletion-requests` | `deletion.request` / `deletion.request_subject` | §5.4 |
| GET | `/v1/deletion-requests` / `/{id}` | `deletion.read` | Status and receipt. Requesters see their own; admins see all |
| POST | `/v1/deletion-requests/{id}/cancel` | `deletion.request` | Workspace kind during cooling-off only |
| POST | `/v1/deletion-requests/{id}/retry` | `operations.repair` | Failed steps |
| GET/POST/DELETE | `/v1/legal-holds` | `governance.hold` | Admin |
| POST | `/v1/suppressions/{id}/lift` | `governance.hold` | Admin |
| POST | `/v1/ontologies/versions/{id}/simulate` | `ontology.write` | Preview, then `confirm=true` |
| GET | `/v1/ontologies/simulations/{id}` | `ontology.read` | Report |
| GET/PUT | `/v1/workspaces/current/settings` | `workspace.admin` (from P7) | Versioned; `If-Match` |
| GET | `/v1/governance/privacy-audit` | `audit.read` | §5.8 |

- **Foreign or invisible ids** → 404 identical to a missing id. **Deleted ids** → 404 identical too, except the requester's own idempotent replay (410).
- The route walker covers the new routes plus **frozen ids**.

### 6.3 Authorization additions (explicit; no existing cell changes)

| Action | workspace_admin | operator | reviewer | app_writer | agent | reader |
|---|---|---|---|---|---|---|
| `correction.apply` | ✓ | | ✓ | | | |
| `correction.read` | ✓ | ✓ | ✓ | | | |
| `deletion.request` (all kinds) | ✓ | | | | | |
| `deletion.request_subject` (subject, interaction; **trusted_source apps only**) | | | | ✓ (trusted) | | |
| `deletion.read` | ✓ | ✓ | | ✓ (own) | | |
| `governance.hold` | ✓ | | | | | |

Reviewers may request `memory` deletion through `memory.curate` plus a reviewer check in the service.

### 6.4 Audit, metrics, logs

- **Audit actions:**
  - `correction.applied` (kind, reason code, target id), `correction.authoritative_override`;
  - `entity.split`, `consolidation.applied`, `consolidation.proposed`;
  - `deletion.requested|frozen|planned|purged|verified|completed|failed|cancelled`, `legal_hold.created|released`, `suppression.lifted`;
  - `settings.updated`, `ontology.simulated`.
- **Metrics** (no ids):
  - `corrections_total{kind}`
  - `consolidation_runs_total{mode,outcome}`, `consolidation_drops_total{reason}`
  - `deletion_requests_total{kind,outcome}`, `deletion_items_total{store,action}`
  - `deletion_freeze_seconds`, `deletion_completion_seconds`, `deletion_residuals_total{store}` (alert when > 0)
  - `retention_deleted_total{class}`
  - `simulations_total{outcome}`
- **Logs:** request ids, kinds, counts. The manifest and residual text are never logged.

---

## 7. Work Packages

### WP0: Commit P5–P7, housekeeping
- **With the user's approval**, commit P5, P6 and P7 as three commits (U14).
- Then add the P8 package skeletons, the ports and the import-linter contracts (§4).

### WP1: Migration 0008 and the purge gate
- The SQL of §6.1, including the trigger gate and the `ops` functions.
- **Tests (release blockers):**
  - every immutable table refuses DELETE and redaction without a purging request;
  - a purging request can delete only rows listed for it, and only in its workspace;
  - `audit_log` refuses always;
  - `platform_deletion_receipts` is invisible to `kg_app` except through `ops`;
  - RLS and pooled reuse;
  - up/down/up with guards.

### WP2: Corrections and impact preview
- `kg_knowledge.corrections` and `kg_memory.corrections`; the projector integration; dependent-memory flagging; the preview.
- **Tests:**
  - **AT-13:** an erroneous WORKS_AT (`hr` sets "Acme Labs") is superseded by an admin to Acme. Context current fixes it; the audit and correction rows are linked; an `authoritative_override` notice is created.
  - `fact.reject` of an extracted fact → graph edge expired (P4 filter); a dependent memory → `disputed` + review; context re-materialization → `withdrawn`.
  - `fact.restore` reverses the reject.
  - Rename keeps the id and adds an alias (KGR-01).
  - Attribute correction with a sensitive attribute stays Postgres-only.
  - Preview counts match the actual effects.
  - A correction on a hidden entity (`agent`/`app` path) → 404; corrections by agents → 403.

### WP3: Identity split
- §5.2, including graph re-projection and mention moves.
- **Tests:**
  - split the P4 "S. Johnson" link → new entity with moved mentions and facts;
  - a non-match pair recorded; lineage `split_from`;
  - the response lists grant holders;
  - the graph equals Postgres after the split (`graph reconcile` clean);
  - the optional `reextract` replays with a new generation.

### WP4: Consolidation
- `memory_consolidate` job, grouping, reinforce, synthesize with validation, the review item, and `MemoryPolicy` fields (backward compatible).
- Strict mock scenarios:
  - a good synthesis;
  - an invented date;
  - merging "dislikes commute" with "wants remote";
  - escalation ("decided to leave");
  - missing citations.
- **Tests (MEM-06):**
  - three near-duplicate preferences → reinforced into one with merged evidence; the others superseded with lineage; search returns one;
  - the episodic group synthesis → proposal → approve → sources superseded with evidence by reference;
  - each invalid synthesis → dropped with its reason and no write;
  - distinct claims stay separate;
  - consolidation never crosses scope sets (a private note and a shared memory are never combined);
  - idempotent re-run;
  - `consolidation: disabled` → no job runs.

### WP5: Deletion core (request, freeze, plan, Postgres, vector and trace purge, verify, receipt)
- `DeletionService`, planner, purgers, verifier, manifest, tombstones, suppression, journal (with HMAC chain), legal holds, workspace cooling-off.
- **Tests:**
  - **Freeze is atomic:** a crash between freeze steps is impossible (one transaction).
  - The **planner multi-source rule**: another subject's fact with independent evidence is retained, while one with no remaining evidence is deleted.
  - Command payloads are redacted with the hash kept.
  - Review rationale text is redacted.
  - Purge is resumable after a crash (fault points per store; FI-13).
  - The receipt has correct counts and the backup expiry date.
  - The manifest is gone at completion.
  - A suppressed external id → `409 subject_suppressed`; the lift works and is audited.
  - The journal line is written, and the chain verifies.
  - Legal hold → `blocked_legal_hold` plus a review item, still frozen; release resumes the request.
  - **Workspace deletion:** suspended at request, cancellable during cooling-off, then fully purged, with the receipt readable through `ops`, and `other_demo` untouched.

### WP6: Graph purge (ADR-018 a–g)
- `GraphPurger` through new `ScopedGraph` operations, under the lock.
- **Tests:**
  - delete Michael → episodes removed;
  - his node and edges gone;
  - neighbours' summaries cleared;
  - surviving facts re-projected (no collateral loss of edges that Postgres still holds, a test for the GR-08 hazard);
  - dangling refs pruned;
  - the orphan sweep removes extraction-created orphans only in this group;
  - a full-text scan finds no residual name;
  - **cross-group safety:** an identical name in `other_demo` is untouched.

### WP7: Read-block and residual suites (SEC-05, AT-16, MEM-07)
- `tests/security/test_deletion_read_block.py`:
  - with purge jobs paused after the freeze, probe **every** surface as admin, reviewer, app and agent (entity, facts, relationships, neighbourhood, graph search, mentions, evidence, interactions, memories (search, list, get), context build, context re-materialization, traces, review items, the SDK and MCP);
  - the deleted subject gives the same 404, never appears, and counts exclude it.
- `tests/security/test_deletion_residuals.py`: after completion, a sentinel name and external id appear in no Postgres text column, Neo4j index, log, metric, audit metadata or journal.
- **MEM-07:** a corrected or deleted memory is gone from embeddings (`memory_embeddings` rows), graph references and context re-materialization.

### WP8: Ontology simulation and AT-17
- `StagingGraph`, `simulate`, the report, auto-cleanup, cost preview, the strict mock scenario under draft v1.5.0 (MANAGES Person→Person).
- **Tests:**
  - the preview requires `confirm`;
  - a run produces a report with MANAGES facts and leaves **no** rows in the workspace tables and no staging group afterwards (including after a crash, via the delayed cleanup);
  - egress per interaction classification (`local_only` sample skipped);
  - **AT-17:** publishing v1.5.0 makes new jobs pin it while old provenance stays on v1.4.0, and `--repin` works.

### WP9: Settings, retention, containment suites, privacy audit
- `workspace_settings` with versioning and validation; retention jobs; egress narrowing in `RouterTransport` (settings cache with a TTL plus invalidation on update).
- **Tests:**
  - raw content expiry: the interaction shows "raw content expired", and replay of the normalized form still works;
  - `episode_content_days` blanks Neo4j episodes;
  - memory archive expiry;
  - egress narrowing denies a classification with zero router calls, and can never widen.
- **Suites:** SEC-04/AT-15 (§5.7) and SEC-06 (the matrix).
- Privacy audit endpoint and filters.

### WP10: Admin console
- **Review Queue v2:**
  - cards for `deletion_exception` (redact editor, delete item, retain with reason under a hold), `consolidation_proposal` (sources side by side, statements with source chips), `correction_request`, `authoritative_override` and `source_compromised` (from P7);
  - split and merge actions from identity items.
- **Graph Explorer:** "Correct…" on a fact (reject, supersede, end, restore) and "Split…" on an entity, both with the impact preview dialog and a required reason.
- **Memory Explorer:** "Correct…" (supersede, reject, expire, dispute), "Request deletion", and the consolidation lineage view.
- **Deletions:** new page listing requests with a state timeline, the receipt view (counts per store, retained items with reasons, backup expiry), legal holds and suppressions management.
- **Settings:** new page for retention, egress narrowing (it shows the effective policy and refuses to widen), and quota defaults; versioned with a history.
- **Privacy audit:** new page with timeline filters and CSV export.
- **Ontology:** a "Simulate" button on drafts, with the cost preview and the report view.
- **Tests:**
  - **Playwright ADM-03:** the reviewer corrects a disputed memory, then a Context Lab build shows the change.
  - A Deletions flow: request Michael's deletion → status → receipt.
  - The legal hold path.
  - Settings cannot widen egress.
  - `admin@other` sees nothing from demo.
  - Agents and readers see none of these pages' actions.

### WP11: Demo and docs
- `make demo-p8` (§8).
- **Docs:**
  - `docs/architecture/p8-governance.md`: correction semantics, deletion lifecycle diagram, store map, purge gate, verification, journal, consolidation, simulation, deviations.
  - **ADRs:**
    - **ADR-041** corrections and authoritative overrides;
    - **ADR-042** identity split;
    - **ADR-043** consolidation (reinforce vs synthesize);
    - **ADR-044** deletion lifecycle, the purge gate and verification;
    - **ADR-045** suppression, tombstones and the deletion journal (backup re-application contract for P9);
    - **ADR-046** ontology simulation with staging namespaces;
    - **ADR-047** workspace settings and retention.
  - **Runbooks:** `subject-deletion.md`, `legal-hold.md`, `identity-correction.md` (split added), `ontology-simulation.md`, `retention.md`.

### WP12: Live checks (folded into the single live session)
- `make eval-consolidation-live`: synthesis validity rate, zero merged-distinct and zero escalations on the gold set.
- `make eval-simulation-live`: report sanity on the sample.
- Both are added to the P6 WP11 live-session command list.

---

## 8. Demo (`make demo-p8`; re-runnable; builds on demo-p7)

1. **Simulation (ONT-05):** draft v1.5.0 adds `MANAGES` (Person→Person). Preview cost → simulate on 3 interactions → the report shows the new MANAGES facts and impact counts. The staging group is gone afterwards.
2. Publish v1.5.0. New interactions pin v1.5.0, and older facts and episodes keep v1.4.0 (AT-17).
3. Ingest "Michael is Sarah's manager" → MANAGES active and unverified. The planning context shows it as unverified.
4. **Reviewer correction:**
   - the preview lists 1 dependent memory and the recent contexts;
   - `fact.reject` → the edge expires, the dependent memory becomes disputed with a review item;
   - the next context no longer shows it, and the old context link re-materializes it as `withdrawn` (roadmap demo, Flow D).
5. **AT-13:** the HR app records Sarah at "Acme Labs" (an error). The admin supersedes it to Acme with a reason. Current context fixed, audit linked, an `authoritative_override` notice raised.
6. **Split:** the "S. Johnson" link from P4 is split into a separate person. Mentions move, and the two are recorded as a non-match.
7. **ADM-03:** a reviewer corrects the disputed memory from step 4 in the Memory Explorer. The Context Lab build reflects it.
8. **Consolidation (MEM-06):**
   - three near-duplicate preference memories are reinforced into one;
   - an episodic synthesis proposal is approved, and its sources are superseded with evidence kept;
   - "dislikes commute" and "wants remote work" stay separate.
9. **Deletion of Michael (AT-16, SEC-05, MEM-07):** `conversation_app` (trusted) requests subject deletion.
   - **Immediately:** Michael's entity → 404, the Sarah→Michael KNOWS fact is gone, memory search has no hits, graph search has no hits, and context has no Michael.
   - The purge completes, and verification reports zero residuals in Postgres, pgvector, Neo4j, traces and logs.
   - The receipt shows per-store counts and `backup_copies_expire_by`.
   - Re-creating Michael with the same external id → `409 subject_suppressed`.
10. **Interaction deletion:** deleting the cycling event removes its memory. Facts with other evidence remain.
11. **Legal hold:** a hold on Sarah, then a deletion request → `blocked_legal_hold` with a visible exception. The request is then cancelled, and the hold released.
12. **Containment:** the AT-15 corpus summary (0 policy changes, 0 envelope escapes, 0 cross-subject leaks) and the SEC-06 matrix summary (0 calls where denied).
13. The privacy audit view lists the reveals, deletions, holds, overrides and egress decisions of this demo.

---

## 9. Test and Gate Mapping

| Gate / requirement | Evidence |
|---|---|
| AT-13 admin corrects a mistaken employer; context fixed; audit linked | WP2; demo 5 |
| ADM-03 reviewer corrects a disputed memory; context changed | WP10 Playwright; demo 7 |
| MEM-06 consolidation reduces redundancy, keeps distinct claims and sources | WP4; demo 8 |
| MEM-07 correction and deletion propagate to embeddings, graph and context | WP2, WP6, WP7 |
| AT-16 / SEC-05 immediate read block; verified purge of each derivative | WP5–WP7 (freeze-paused probes; residual scans); demo 9 |
| AT-15 / SEC-04 injection stays data | WP9 corpus suite; demo 12 |
| SEC-06 no egress without policy or consent | WP9 matrix; demo 12 |
| AT-17 version pinning | WP8; demo 2 |
| ONT-05 migration preview | P2 diff + WP8 simulation; demo 1 |
| Flow D/E/F | WP2/WP3, WP5–WP7, WP8 |
| Immutability except via an approved plan | WP1 trigger tests |
| Isolation and telemetry | Route walker (frozen and deleted ids), RLS and pooled reuse, sentinels, metrics label audit |

## 10. Exit Criteria

- [ ] `make reset up seed demo-p1 … demo-p8` passes, and each demo re-runs. demo-p8 deletes only Michael-related data, so it can be re-created by re-running.
- [ ] `make check`, `make test-resilience` (+ FI-13 purge crash per store, FI-14 crash between freeze and plan), `make test-e2e`, `make eval-context` (leakage 0, now including deleted-subject cases) and `make eval-consolidation` (mock) are green.
- [ ] **Strict:** zero residuals in the deletion verification suites; every immutability trigger holds outside an approved plan.
- [ ] ADR-041..047, the architecture note and runbooks are written. `p8_additions` lists no new third-party packages (expected).
- [ ] **Release blockers carried forward:** the live session (P6 WP11, extended by WP12).

## 11. Risks and Mitigations

| Risk | Mitigation |
|---|---|
| A missed derivative leaves personal data behind | The planner uses explicit link tables plus residual-text scans. Verification blocks `completed`. Exceptions go to review. A per-store residual metric alerts |
| The purge gate becomes a bypass | The trigger requires a `purging` request in the same workspace **and** a listed item. A request can only be created by authorized principals through `SubjectFilter`. Tests probe forging attempts |
| Graph purge damages surviving knowledge | Postgres-driven re-projection after removal (ADR-018 d), `graph reconcile` in tests, and cross-group checks |
| Consolidation loses nuance | Synthesis always goes to review; distinct-claim and escalation validators; reinforce never writes new text |
| Simulation leaks into real data | `StagingGraph` namespaces, no workspace table writes, auto-cleanup jobs, a test for "nothing left" |
| Backups resurrect deleted data | The receipt states the backup expiry. The out-of-band journal lets P9 restores re-apply deletions |
| Retention removes replayability | Shown on each interaction; normalized retention is separate from raw; defaults keep both |

## 12. Rollback

- Disable deletion intake with `DELETION_API_ENABLED=false`. Frozen requests stay frozen, which keeps reads safe.
- Turn off consolidation with ontology policy `disabled`.
- Simulation is opt-in.
- `kg db downgrade 0007` is guarded and refuses while requests are not terminal.
- Settings revert by re-applying a previous version.

## 13. Decisions (adopted per the user's instruction to go with the recommendations)

1. **Corrections are not ranked by authority.** A reviewer or admin correction may supersede an authoritative fact, and every such override is flagged for the source system.
2. **Freeze is a status change in one transaction** (`deletion_pending`), and existing filters hide it everywhere at once. There is no separate block list.
3. **Immutable tables can be deleted or redacted only through a listed, purging deletion plan**, enforced in triggers. `audit_log` is never altered; it holds ids and codes only.
4. **Command payloads are redacted, not deleted**, with their hash kept, so the ledger stays continuous.
5. **A subject deletion removes the interactions linked to that subject**, plus their sole-sourced derivatives about others (privacy over recall). Other subjects' memories that only *mention* the subject go to a `deletion_exception` review for redaction.
6. **No crypto-shredding in v1.** The receipt states when backup copies expire, and an out-of-band deletion journal lets P9 restores re-apply deletions.
7. **Re-collection suppression uses external-id HMACs only.** Names are not identities. Admins can lift it.
8. **Trusted-source apps may request subject and interaction deletion on behalf of an end subject.** Workspace deletion is admin-only, with a 24 h cooling-off period.
9. **Consolidation:** deterministic *reinforce* runs automatically; LLM *synthesize* always goes to review.
10. **Ontology simulation runs in a temporary staging namespace** on the shared Neo4j database, admin-triggered with a cost preview. The full re-ingest and cutover (Flow F) is a P9 runbook built on the same machinery.
11. **Neighbour summaries that may cite a deleted subject are cleared**, not regenerated by an LLM. Graphiti refreshes them on the next episode.
12. **Workspace egress settings can only narrow** the platform policy, never widen it.
13. **demo-p8 deletes Michael** (re-creatable), not Sarah, so the earlier demos stay re-runnable.
