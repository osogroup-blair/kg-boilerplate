# Runbook: simulating a draft ontology before publishing

Use it when a draft adds or changes types or rules and you want to see what the extraction would do with *your* interactions before anything changes.

1. Open the draft (Ontology → the version). Save it (**Simulate…** is disabled while there are unsaved changes). Only a **draft** can be simulated.
2. **Simulate… → Estimate the cost.** Choose how many recent interactions (default 3). The estimate shows how many can be sent, how many were skipped by policy (classification `local_only`, consent withheld, workspace egress narrowing) and the expected calls, tokens and cost. **No model is called.**
3. **Run the simulation** once the estimate is acceptable (API: add `budget_usd` to refuse an estimate above it). The run reads the samples with the draft's extraction in a scratch graph group and removes it when it ends; a delayed job removes it again if the worker died.
4. Read the report: facts by type, entities by type (already known vs new), **new relationship types** in use, facts the active version produced that the draft would not (`dropped_by_draft`), the diff against the active version (changes, breaking), the actual cost. Counts and type keys only; open the diff for the YAML view.
5. Decide: edit the draft and simulate again, or **Publish** (new interactions pin the new version; everything already recorded keeps the version it was made under; `replay --repin` moves one interaction deliberately).

Checks and limits
- Nothing is written to the workspace except the simulation record. `GET /v1/ontologies/simulations/{id}` shows the state; a simulation that ended `failed` still removed its scratch group.
- The scratch group shares Neo4j capacity: keep samples small. Roll back a bad publish by activating the previous version.
- Offline self-test: `python -m eval.simulation.run_eval --mode mock`; live sanity: `make eval-simulation-live` (user action).
