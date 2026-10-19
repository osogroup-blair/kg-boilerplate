# Knowledge and memory platform

**New here?** The public site has interactive playgrounds (no install), a sandbox, the docs and an API reference: build it with `make site-dev` (the deployed address is set when the owner publishes it, see [the site plan](docs/plans/phase-11-public-site-and-playgrounds.md)).

A multi-tenant knowledge graph and curated-memory service for agents and applications. Facts live in a deterministic Postgres ledger (row-level security) and are projected into a Graphiti/Neo4j graph; curated memory has scoped hybrid search; a task-aware **context engine** turns both into a bounded, cited, permission-filtered package (`POST /v1/context/build`). Around it: multi-agent ACLs, governance (corrections, consolidation, deletion with a restore-proof journal), an admin console, a Python SDK and an MCP server.

## Try it in about ten minutes
Prerequisites: Docker (about 6 GB of memory, 20+ GB of free disk), Node 20, git. Python runs through `uv`, which `make tools` installs for you.

```bash
git clone https://github.com/osogroup-blair/kg-boilerplate.git && cd kg-boilerplate
make tools sync admin-install examples-install   # uv, the Python workspace, the console, notebook tooling
make up seed                                     # postgres, neo4j, mock router, migrations, api, worker; demo workspaces
make examples                                    # Sarah, a second domain (CRM), and agents through the SDK and MCP
```
`make examples` runs against a **mock router**, so it needs no model key and gives the same answers every time. It ends with a pass/fail line for each example; what each one shows is in [examples/README.md](examples/README.md). Then:

```bash
make notebooks          # the seven notebooks, executed (copies with outputs in examples/notebooks/out/)
make admin-dev          # the console on http://localhost:5273; sign in with a key from .local/dev-credentials.json (for example demo/admin@demo)
make check              # lint, types, import contracts, unit/integration/security/contract tests, console tests
make help               # every target
```

## What to read
| Read | For |
|---|---|
| [examples/README.md](examples/README.md) | the working examples: the Sarah scenario, the CRM and support-desk domains, agents (SDK and MCP), seven notebooks |
| [apps/site](apps/site) | the public site: landing page, six replay playgrounds, the docs (concepts, tutorials, guides, a generated reference), the API reference. `make site-dev`, `make site-check` |
| [deploy/sandbox](deploy/sandbox/README.md) | the hosted sandbox: a throwaway public instance on a mock router (an owner deploys it) |
| [docs/architecture/overview.md](docs/architecture/overview.md) | how the pieces fit: write path, read path, time, security rules, with links to the ADRs |
| [docs/guides/extend.md](docs/guides/extend.md) | add a domain, a profile, an agent, an ingestion source; swap the router or the embedding model; deploy |
| [docs/agent-integration.md](docs/agent-integration.md) | the contract for an agent that asks for context and writes back |
| [docs/runbooks/startup.md](docs/runbooks/startup.md) | running the stack locally: ports, roles, resets, keys |
| [ROADMAP.md](ROADMAP.md) | the phases P0-P10, what each delivered, surprises worth knowing |
| [docs/release/handoff.md](docs/release/handoff.md) | **picking up the work?** start here: exact state and what is left |
| [docs/release/checklist.md](docs/release/checklist.md), [decisions.md](docs/release/decisions.md) | what a release needs, and the open decisions |
| [docs/plans/](docs/plans/) | the detailed plan of every phase |
| `solution_inputs/` | the original blueprint the plans implement |

## Repository map
`apps/` api, worker, cli, mcp, admin (React) · `packages/` domain logic, one package per concern · `sdk/python` the client · `examples/` runnable examples and notebooks · `eval/` frozen evaluation suites, the release gate and the load harness · `tools/` mock router, backups, live-session and router checks · `deploy/` compose files and observability · `docs/` architecture, ADRs, runbooks, plans, release records.

## Contributing, security, licence
[CONTRIBUTING.md](CONTRIBUTING.md) says how to set up and what to run before a pull request; [SECURITY.md](SECURITY.md) says how to report a vulnerability privately; the code is under the [Apache License 2.0](LICENSE) (the owner confirms this choice: DEC-13 in [docs/release/decisions.md](docs/release/decisions.md)).
