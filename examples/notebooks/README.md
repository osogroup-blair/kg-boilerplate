# Notebooks

Seven walkthroughs that run top to bottom against the local stack. Each starts a **fresh workspace** and loads the mock router's answers, so every run gives the same results and nothing leaks between notebooks. Every code cell asserts what it shows, so a notebook that runs is a notebook that is true.

| # | Notebook | You will see |
|---|---|---|
| 1 | `01_ingest_and_ledger` | the event envelope, `Idempotency-Key` (replay, and a 409 for a changed body), the stage timeline, who may read raw content, a model outage and an audited replay |
| 2 | `02_temporal_graph_as_of` | commands vs extracted facts, `as_of` and `known_at`, a 2027 job change, the graph drawn at two moments, a fact walked back to its evidence |
| 3 | `03_curated_memory` | proposals and policy, a person approving guidance, an agent's finding, hybrid search and its score parts, the distractor |
| 4 | `04_context_build_and_budget` | one task at three budgets, `insufficient_budget`, deterministic unknowns, the trace, a package re-read after a correction (`withdrawn`) |
| 5 | `05_multi_agent_sharing` | grants, private notes vs published findings, the same items over SDK and MCP, an agent with no grants, revocation |
| 6 | `06_corrections_and_deletion` | impact preview, a correction with `If-Match`, immediate read-blocking on deletion, the purge receipt |
| 7 | `07_second_domain` | the CRM domain from configuration only, a renewal-prep package, unknowns, customer isolation, the benchmark |

## Run them
```bash
make up seed mcp-up examples-install   # the stack, the MCP server (notebook 5), and jupyterlab/nbclient/networkx/matplotlib
make notebooks                         # execute all seven; executed copies with outputs land in examples/notebooks/out/ (git-ignored)
make test-examples                     # the same, as tests, plus the other examples
uv run --group examples jupyter lab examples/notebooks   # to work in them interactively
```

## How they are made
The `.ipynb` files are **generated** from `src/NN_name.py` (cells separated by `# %%`, text cells by `# %% [markdown]`) and committed **without outputs**, so diffs stay readable and nothing stale is checked in. Edit the source, then:

```bash
uv run --group examples python -m examples.notebooks.build           # rewrite the notebooks
uv run --group examples python -m examples.notebooks.build --check   # fail if they are out of date (the tests do this)
```
The first cell of each notebook calls `examples._kit.notebook.start(...)`; the last calls `lab.close()` (the mock router's fixtures and strict mode are also restored when the kernel exits).

Offline, the model behind extraction is the mock router, so numbers such as tokens are exact and repeatable; with a real router (owner action) the wording of extracted memories varies and some assertions would need loosening.
