# Phase 8: correct, consolidate, forget and govern

Decisions: [ADR-041](../adr/ADR-041-corrections-and-authoritative-overrides.md) (corrections), [ADR-042](../adr/ADR-042-identity-split.md) (identity split), [ADR-043](../adr/ADR-043-memory-consolidation.md) (consolidation), [ADR-044](../adr/ADR-044-deletion-lifecycle-purge-gate-and-verification.md) (deletion lifecycle, the purge gate, verification), [ADR-045](../adr/ADR-045-suppression-tombstones-and-the-deletion-journal.md) (suppression, tombstones, the journal), [ADR-046](../adr/ADR-046-ontology-simulation-in-staging-namespaces.md) (simulation), [ADR-047](../adr/ADR-047-workspace-settings-and-retention.md) (settings and retention). Plan: [phase-8-governance.md](../plans/phase-8-governance.md). Run it: `make reset up seed demo-p1 demo-p2 demo-p3 demo-p4 demo-p5 demo-p6 demo-p7 demo-p8` (`demo-p8` is re-runnable: it makes its own people). Runbooks: [subject-deletion](../runbooks/subject-deletion.md), [legal-hold](../runbooks/legal-hold.md), [identity-correction](../runbooks/identity-correction.md), [ontology-simulation](../runbooks/ontology-simulation.md), [retention](../runbooks/retention.md).

## Packages
| Package | P8 |
|---|---|
| `kg_governance` | `deletion/` (`plan` the closure and the item keys, `purge` the per-store steps, `verify` id and text scans, `requests` the lifecycle service, `holds` legal holds and suppressions, `journal` the out-of-band file, `faults` the failure-injection hook, `ports`, `models`), `settings` (versioned workspace settings), `privacy` (the audit timeline), new actions `correction.*`, `deletion.*`, `governance.hold`, `workspace.admin` |
| `kg_knowledge` | `corrections` (`Corrections`: fact, entity, memory entry point, split, impact previews, history), `graph_purge` (`GraphPurger`), `projection` (re-projection of survivors, frozen rows skipped), reviews for the four new item types |
| `kg_memory` | `corrections` (the memory half of a correction, dependants disputed), `consolidate*` (reinforce and synthesize), `deletion` (`build_deletion`, the exception handler, `RetentionSweeper`), `deletion_jobs` (the three stage jobs) |
| `kg_graph` | `GroupId.for_simulation`, `GraphService.staging`, scoped removal / listing / scan / blank / sweep methods |
| `kg_extraction` | `simulation` (`SimulationService`, the run and cleanup jobs) |
| `kg_ontology` | `MemoryPolicy` consolidation fields; the v1.5.0 example (`MANAGES`, consolidation) |
| `kg_modelgw` | `WorkspaceEgress`: the transport also refuses what the workspace narrowed |
| `kg_ingestion`, `kg_context` | frozen rows are invisible; a suppressed identifier is refused; the context trace hides frozen ids |
| `apps/api` | `/v1/…/corrections[/preview]`, `/v1/deletion-requests`, `/v1/legal-holds`, `/v1/suppressions`, `/v1/workspaces/current/settings[/history]`, `/v1/governance/privacy-audit`, `/v1/ontologies/versions/{id}/simulate`, `/v1/ontologies/simulations/{id}` |
| `apps/worker` | the deletion, consolidation and simulation stages; the retention sweep and deletion gauges on the beat |
| `apps/cli` | `kg deletions list|show|cancel|retry|journal|retention-sweep` |
| `apps/admin` | correction and split dialogs with an impact preview, Review Queue cards for the four new types, Deletions (requests, holds, suppressions, receipt), Settings, Privacy audit, Simulate |

## Corrections (Flow D, AT-13)
A correction is a ledger command (`correction.fact_reject|supersede|end|restore`, `correction.attribute`, `correction.rename`, `correction.memory`, `entity.split`) with an `If-Match` on the record's revision, an `Idempotency-Key`, a reason **code** (free text stays on the `corrections` row, never in the audit log) and a preview (`GET …/corrections/preview?kind=`) that counts what it would change: graph operations, predecessor facts that would reopen, dependent memories, open reviews, readers, recent contexts. Postgres is authoritative; the graph follows by projection; contexts re-materialise (nothing is cached). Evidence is never rewritten: a correction appends.
- **Authority.** A correction is a person's decision, so it is not ranked against the source's authority; if the fact was `authoritative_system` it still applies, and an `authoritative_override` review item tells the source's owner (`correction.authoritative_override` in the audit).
- **Dependants.** Shared memories that rest on a rejected fact become `disputed` with a `memory_conflict` item; a restore reverses it.
- **Split** is the compensating command for a merge. It moves chosen mentions, identifiers, facts and memories (with their subject and subject scope, through a lineage-aware trigger) to a new entity, records the pair in `identity_non_matches`, and can replay the moved conversations.

## Consolidation (MEM-06)
Per scope set and type, after a debounce: **reinforce** folds exact and near duplicates (Jaccard and cosine thresholds from the policy) into the highest-authority, earliest memory, merging evidence by reference and writing no new text; **synthesize** (episodic groups within a window) asks the model for a condensed statement set that **cites its sources**, validates every sentence (`consolidate_rules.validate`: no invented number, date or name; no merged distinct claims; no hedge turned into a decision; no echoed instruction) and always opens a `consolidation_proposal` review. Approving supersedes the sources, whose evidence is kept by reference. Runs are idempotent per input set (`consolidation_runs`).

## Deletion (Flow E, SEC-05, AT-16)
```
received ─► frozen ─► planned ─► purging ─► verifying ─► completed
               │          │          │           └──────► completed_with_exceptions ─(a person decides)─► verifying
               │          └── failed ◄┘  (retry resumes where it stopped)
               └─► blocked_legal_hold ─(hold released)─► frozen ─► …            cancelled (workspace cooling-off only)
```
1. **Freeze** (in the request's own transaction): `status = deletion_pending` on the closure's entities, facts, memories and interactions; every read, projection and job treats them as absent from that commit. The request also records suppression hashes and an encrypted **manifest** (names, identifiers) used only by the planner and the verifier.
2. **Plan** (`deletion_plan` job): the `deletion_items` list every derivative, by store: `postgres` (every table in `plan.KEYS`), `vector` (memory embeddings), `graph` (episodes incl. those a half-finished extraction left, nodes, edges, summaries to clear, survivors to re-project), `trace` (context items, snapshots; request ids redacted), `job` (outbox rows and attempts), `idempotency`. Memories that merely *name* the subject are not deleted: they become `deletion_exception` reviews.
3. **Purge** (`deletion_purge` job): fixed per-store steps, each its own transaction, each idempotent (items carry `done`). The database enforces the plan: every gated table has a trigger calling `public.purge_gate`, which allows a DELETE or a redaction only for a row listed in a `purging` request of the same workspace (or `kind = workspace`). A bug cannot delete outside the plan. The graph step removes episodes, nodes and edges, clears summaries that cited the subject, prunes dangling references, sweeps orphans, and **re-projects survivors from Postgres**.
4. **Verify** (`deletion_verify` job): id scans over every store, residual-text scans for the manifest's names and identifiers (names a surviving entity also carries are not scanned), a graph scan. Any residual → an exception item and `completed_with_exceptions`.
5. **Receipt**: counts per store (deleted / redacted / retained), the classes kept and why (`audit_log` ids and codes, the ledger rows with redacted payloads, tombstones), the verification result and `backup_copies_expire_by`; the manifest is deleted; a journal line is appended; tombstones (ids and hashes only) are written. A workspace deletion writes its receipt to `platform_deletion_receipts` (it survives the workspace).

The ledger keeps its rows (a command's payload and `source_ref` are redacted to a hash marker), `audit_log` is never altered, and `suppression` (an HMAC of each strong identifier) refuses re-creation (`409 subject_suppressed`) until an admin lifts it. **Legal holds** (subject, interaction, workspace) freeze but do not purge; releasing resumes. **Workspace deletion** suspends the workspace at once and waits out `WORKSPACE_DELETION_COOLING_HOURS`.

## Ontology simulation (ONT-05) and versioning (AT-17)
`POST …/versions/{draft}/simulate` previews (tokens, calls, cost; no model call), and with `confirm` runs the draft on sampled interactions in the staging group `ws-<uuid>-sim-<run>` on the same Neo4j database. Nothing is written to the workspace's tables; the report holds counts and type keys; a run job and a delayed cleanup job both remove the group. `local_only` or consent-withheld samples are skipped. Publishing makes new interactions pin the new version; facts, commands, episodes and memories keep the version they were made under; `replay --repin` moves one interaction (audited).

## Settings and retention
`workspace_settings` is versioned (`If-Match`, idempotent, a `settings.update` command). Retention windows (raw and normalized content, episode text, traces, model calls, unconfirmed memories) run as a periodic sweep: content expiry is a `retention`-kind deletion request through the same gate with its own receipt; an interaction shows `raw_content_expired` / `normalized_content_expired`. The egress setting is a **deny list unioned with the platform's**: it can only narrow what leaves (`WorkspaceEgress` in the transport, cached `WORKSPACE_EGRESS_TTL_SECONDS`).

## The data (migration `0008_governance`)
New: `deletion_requests`, `deletion_items`, `deletion_manifests`, `deletion_tombstones`, `deletion_suppressions`, `legal_holds`, `corrections`, `attribute_corrections`, `entity_lineage` (backfilled from P4 merges), `consolidation_runs`, `ontology_simulations`, `workspace_settings`, `platform_deletion_receipts`. Changed: status / origin / source-type checks, new command and review types, `review_items.deletion_request_id`. Functions: `public.purge_gate`, `public.purge_guard`, the redaction guards, `ops.*` (receipts, `purge_workspace_rows`, retention candidates, egress denials, stats). Downgrade refuses while a request is unfinished, a correction-made row exists or a workspace is deleted, unless `--force-drop-governance`.

## Deviations from the plan
- Simulation lives in `kg_extraction` (it needs the extraction pipeline), and the deletion wiring in `kg_memory/deletion.py` (the packages above `kg_governance` own the adapters). The failure-injection hook is installed by the builder.
- `POST /v1/deletion-requests` is open to any principal and the service authorises by kind (admin; a trusted app for what it may see; a reviewer for a memory). Cancelling a workspace deletion in its cooling-off is an operator action (`kg deletions cancel`) because the workspace is suspended.
- A split moves memory subjects through the trigger; the subject inside a memory's assertion JSON is not rewritten.
- `kg deletions reapply` (P9) is not built; `kg deletions journal --since` lists what a restore must re-apply.
- Commit of P5-P8 is pending the owner's approval (WP0).
