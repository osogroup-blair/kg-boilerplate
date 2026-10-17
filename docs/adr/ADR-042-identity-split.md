# ADR-042: A wrong identity link is undone by a split, not by editing history

**Status:** Accepted  **Decision refs:** ADR-028 (identity resolution and merge), AT-03, phase-8 plan §5.1.3  **Evidence:** `tests/integration/test_p8_corrections.py` (split tests), `scripts/demo_p8.py` step 6

## Context
A merge (ADR-028) leaves the merged entity as `status = merged` and moves its facts. A wrong merge, or a wrong automatic link, previously had no compensating command (the P4 runbook said so).

## Decision
`entity.split` (via `POST /v1/entities/{id}/corrections {kind: split}`) creates a **new entity** with its own id and moves exactly what the caller names:
- **mentions** (the accepted links from interactions), with their graph node uuid re-pointed through the graph's own mention move;
- **external identifiers** (a strong id belongs to one entity; moving it releases it from the original);
- **facts** whose endpoint is the original but belong to the new person (their evidence stays);
- **memories** about the new person (subject and subject scope follow, through the lineage-aware trigger of migration 0008).

Rules: it refuses a merged entity, anything named that does not belong to the original entity, a move that names nothing, and a type that is not in the active ontology. `entity_lineage` records `split_from`; `identity_non_matches` records the pair so the platform never proposes the merge again. `reextract: true` replays the extraction of the interactions whose mentions moved. The original entity keeps everything not named: nothing is guessed.

## Consequences
- The compensating command is audited like the merge it undoes (`entity.split`, in the privacy audit under overrides).
- Memories keep the old subject inside their stored assertion JSON (the column is the source of truth); this is a known limitation, listed in the architecture deviations.
- A split of an entity with many facts needs the caller to choose them: the dialog lists mentions and identifiers, and facts by id through the API.
