# Knowledge and memory platform

A multi-tenant knowledge graph and curated-memory service for agents and applications: a deterministic ledger in Postgres (row-level security), a Graphiti/Neo4j graph projection, curated memory with scoped hybrid search, a task-aware context engine, multi-agent ACLs, governance (corrections, consolidation, deletion with a restore-proof journal), an admin console, an SDK and an MCP server.

| Read | For |
|---|---|
| [docs/release/handoff.md](docs/release/handoff.md) | **Start here if you are picking up the work**: exact state, what is left, how to resume |
| [ROADMAP.md](ROADMAP.md) | the phases P0-P10, what each delivered, surprises worth knowing |
| [docs/runbooks/startup.md](docs/runbooks/startup.md) | run it locally (`make sync up seed`) |
| [docs/architecture/p9-release.md](docs/architecture/p9-release.md) | the release-hardening phase and its deviations |
| [docs/release/checklist.md](docs/release/checklist.md), [decisions.md](docs/release/decisions.md) | what a release needs, and the open decisions |
| [docs/plans/](docs/plans/) | the detailed plan of every phase |
| `solution_inputs/` | the original blueprint the plans implement |

Quick start (Docker Desktop, about 6 GB): `make tools sync admin-install`, then `make up seed demo-p1`. `make help` lists the targets; `make check` runs everything except the browser tests.
