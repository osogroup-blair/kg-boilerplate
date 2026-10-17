# ADR-046: A draft ontology is tried in a scratch graph group that is always removed

**Status:** Accepted  **Decision refs:** ONT-05, AT-17, Flow F preview, ADR-015 (graph tenancy), phase-8 plan §5.5  **Evidence:** `tests/integration/test_p8_simulation.py`, `make eval-simulation-live`, `scripts/demo_p8.py` steps 1, 2

## Context
Publishing an ontology changes what extraction produces. Reviewing the YAML tells you what *can* happen, not what *will*.

## Decision
- A simulation runs the draft's compiled extraction on up to N sampled interactions in a staging group `ws-<workspace>-sim-<run>` on the same Neo4j database (a different `group_id`, so no read of the real group can see it). Nothing is written to the workspace's tables except the `ontology_simulations` row; the report holds counts by type key, the diff against the active version, identity match counts and cost.
- **Preview, then confirm.** The first call estimates tokens, model calls and cost without calling a model; running needs `confirm` (and optionally a `budget_usd`). A preview can be confirmed once.
- **Policy.** Interactions that may not leave (`local_only`, consent withheld, workspace narrowing) are skipped; if all are, the run is refused.
- **Cleanup twice.** The run removes its group in a `finally`; a delayed job removes it again (idempotent) if the worker died. The group name is deterministic, and a workspace deletion clears its staging groups too.
- **Versioning (AT-17).** Publishing pins new interactions to the new version; derived records keep the version they were made under; `replay --repin` moves one interaction.

## Consequences
- The staging group shares Neo4j capacity with production; the sample cap and the budget bound it.
- Report sanity (cost within 0.2×–5× of the estimate, nothing left behind) is checked live by `make eval-simulation-live`.
