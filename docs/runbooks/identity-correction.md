# Runbook: identity correction (wrong merge, duplicate person, stuck review)

Every identity decision is a ledger command with lineage, so nothing is lost: merged entities keep their id (`status=merged`, `merged_into_id`), and every fact keeps its evidence.

## Two entities that are one person
1. Admin console → **Review Queue**: an `identity_match` item should exist. Open it, read the evidence, choose **same entity**. This runs `entity.merge`.
2. No review exists (the platform did not suspect it)? As an admin: `POST /v1/entities/{canonical}/merge` with the other id, `Idempotency-Key` and a `rationale_code`. It refuses (`merge_refused`) when both carry different strong external ids; resolve that in the source system first.
3. Check: `GET /v1/entities/{merged_id}` answers 200 with `merged_into_id`; `kg graph reconcile --workspace <ws>` reports no drift.

## A merge that was wrong
Split is a compensating command planned for P8. Until then:
1. Stop the damage: tell reviewers to hold decisions for that entity pair.
2. Create the person again with `POST /v1/entities` (new id) with the correct external ids; re-submit the facts that belong to them as commands (their original evidence stays on the merged record).
3. Record a `different_entity`-style note in the review item's rationale; `identity_non_matches` stops the pair from being proposed again.
4. If many entities are affected, `kg interactions replay <event_id> --stage graph_ingest --repin --actor <you> --reason <code> --workspace <ws>` on the interactions involved re-extracts them against the corrected directory.

## A review stuck open
- `uv run kg reviews list --workspace <ws> --status open [--type identity_match]` shows the open items; `kg reviews show` and `kg reviews decide` work the same way as the console. A decision whose `If-Match` is stale answers `412`: re-read the item and decide again.
- An item that was already decided answers `409 already_decided`; a decision that cannot be applied (the facts changed so it no longer fits) answers a correction-required error and leaves the item open.

## Wrong automatic link
A deterministic link (subject_ref or name variant of the subject) added an alias you do not want: P4 has no alias-removal command (planned with the P8 corrections). Record the case in the review rationale and ask the platform owner to correct the row through a compensating command.

Audit: `GET /v1/audit?action=review.decide` lists who decided what and when (rationale codes only; free text stays on the review row).
