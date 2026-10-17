# Runbook: identity correction (wrong merge, duplicate person, stuck review)

Every identity decision is a ledger command with lineage, so nothing is lost: merged entities keep their id (`status=merged`, `merged_into_id`), and every fact keeps its evidence.

## Two entities that are one person
1. Admin console → **Review Queue**: an `identity_match` item should exist. Open it, read the evidence, choose **same entity**. This runs `entity.merge`.
2. No review exists (the platform did not suspect it)? As an admin: `POST /v1/entities/{canonical}/merge` with the other id, `Idempotency-Key` and a `rationale_code`. It refuses (`merge_refused`) when both carry different strong external ids; resolve that in the source system first.
3. Check: `GET /v1/entities/{merged_id}` answers 200 with `merged_into_id`; `kg graph reconcile --workspace <ws>` reports no drift.

## A merge that was wrong, or a wrong link: split
A split creates a **new person** with its own id and moves exactly what you choose; the original keeps the rest. The pair is recorded as a non-match, so the platform will not propose the merge again.
1. Admin console → the entity → **Split…** (reviewers and admins). Give the new person's name, tick the **mentions** (the conversations that were about someone else) and any **identifiers** that belong to them, optionally *read the moved conversations again*, choose a reason, read the **impact preview** (mentions, readers, recent contexts), confirm.
2. API: `POST /v1/entities/{id}/corrections {kind: "split", reason_code, new_entity: {display_name, type}, move: {mention_ids, fact_ids, memory_ids, external_ids}}` with `If-Match` of the entity revision. Preview: `GET /v1/entities/{id}/corrections/preview?kind=split`.
3. It refuses a merged entity, anything that does not belong to the original, and a move that names nothing.
4. Check: both entities answer 200; `GET /v1/entities/{old}/mentions` lost exactly the moved ones; `kg graph reconcile --workspace <ws>` reports no drift. The split is audited (`entity.split`) and listed in the Privacy audit.
Facts that were sole-sourced from the moved conversations stay with whoever they name; move them by `fact_ids` if they belong to the new person.

## A review stuck open
- `uv run kg reviews list --workspace <ws> --status open [--type identity_match]` shows the open items; `kg reviews show` and `kg reviews decide` work the same way as the console. A decision whose `If-Match` is stale answers `412`: re-read the item and decide again.
- An item that was already decided answers `409 already_decided`; a decision that cannot be applied (the facts changed so it no longer fits) answers a correction-required error and leaves the item open.

## Wrong automatic link
A deterministic link (subject_ref or name variant of the subject) added an alias you do not want: correct the name or attribute (`kind: rename` keeps the id and makes the old name an alias; `correct_attribute` fixes a value), or split the entity if the alias belongs to someone else.

Audit: `GET /v1/audit?action=review.decide` lists who decided what and when (rationale codes only; free text stays on the review row).
