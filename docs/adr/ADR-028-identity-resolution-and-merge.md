# ADR-028: Identity resolution and merge

**Status:** Accepted  **Decision refs:** ADR-013 (embeddings), ADR-015 (graph tenancy), ADR-027  **Evidence:** `packages/extraction/tests` (identity), `tests/integration/test_p4_reviews.py`, `tests/security/test_p4_isolation.py`, demo-p4 steps 5, 6, 8

## Context
Two people can share a name ("Sarah Johnson" the colleague and the cyclist). Graphiti dedupes nodes by exact name, so left alone it would silently merge them: the worst identity error, because it is invisible. The opposite error (one person, two entities) is annoying but visible and fixable.

## Decision
1. **Deterministic links only where the caller vouched.** A mention links to an existing entity without review when it is (a) a `subject_ref`/`actor` of the interaction, or (b) a *name variant anchored to a unique same-type subject* (`names_compatible`: "S. Johnson" with Sarah as subject_ref adds an alias, `name_variants` counted on the stage result).
2. **Everything else forks.** An unvouched mention that Graphiti resolved onto an existing node is split back out into a **candidate entity** (`origin=extracted`, `status=candidate`, never visible as a verified directory entry), and an `identity_match` review is opened against the best existing candidate (trigram `IDENTITY_TRGM_MIN` 0.45 and E5 cosine `IDENTITY_COS_MIN` 0.92, both in workspace scope only). Facts that touch the candidate are held (`identity_pending`) until the review is decided.
3. **Reviewer decisions:** `same_entity` → merge; `different_entity` → the candidate becomes a normal active entity and a row in `identity_non_matches` stops it being proposed again; `unsure` keeps it open.
4. **Merge is a ledger command (`entity.merge`).** The canonical id stays stable; the merged row is kept (`status=merged`, `merged_into_id`, `merged_at`); aliases, external ids, mentions, attribute claims and facts move to the canonical entity; extracted facts are re-assessed by the pure policy; Graphiti's nodes are merged (`merge_nodes`, `move_mentions`) and the projector restores. A merge **refuses** when the two entities carry conflicting strong ids (`MergeRefused`), and a merged entity still answers `GET /v1/entities/{id}` with 200 and `merged_into_id`, so an id is never lost (KGR-01); the console shows a banner linking to the canonical entity.
5. **Workspace scope is absolute:** candidates are generated under RLS, so another workspace's entity can never be proposed, linked or merged (SEC-01, tested in `test_p4_isolation`).
6. **Sensitive attributes** are never sent to the model and never used for matching.

## Consequences
- Precision first: a wrong split costs one review click; a wrong merge needs the correction runbook and (P8) a compensating split.
- `IDENTITY_COS_MIN` is a placeholder until `make eval-extraction-live` produces real related/unrelated cosine distributions; the mock run reports `None` because the mock embeddings carry no signal.
- Reviewers with `REVIEW_DECIDE` see a narrow, audited slice of the evidence (`REVIEW_READ_EVIDENCE`), not the interaction content in general.
