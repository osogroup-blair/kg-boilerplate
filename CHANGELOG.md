# Changelog

All notable changes to this project are recorded here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); the project is pre-1.0 and has not been tagged. Each phase below is one stretch of the build, in order. The detailed plans are in [`docs/plans/`](docs/plans/) and the status of each phase is in [ROADMAP.md](ROADMAP.md).

## [Unreleased]

### Added
- **Phase 11, public site and playgrounds:** an Astro Starlight site (`apps/site`) with a landing page, six replay playgrounds (the Sarah scenario, the context explorer, time travel, the permission lens, the graph viewer, the write path), the documentation (concepts, tutorials, guides, a generated reference, operations), an interactive API reference, a sandbox launcher, and the tools behind them (`tools/replay_capture`, `scripts/site`, `apps/sandbox`, `deploy/sandbox`).
- A third example domain, a support desk (`make demo-support`), written for the "Model your own domain" tutorial, and a minimal agent (`make demo-agent`).
- Examples leave their keys and ids in `.local/examples/<name>-latest.json` so a reader can explore the workspace they built.
- `LICENSE` (Apache-2.0), `NOTICE`, `CONTRIBUTING.md`, `SECURITY.md`, `CODE_OF_CONDUCT.md`, issue and pull request templates.

## Phase 10: close-out and working examples (2026-10-18)
- The example kit, the Sarah scenario (`make demo-sarah`), a second domain (CRM, `make demo-crm`) with a nine-case benchmark, agents over the SDK and MCP, seven notebooks, `make examples`, and the CI `examples` job.
- Docs: architecture overview and the extend and add-a-domain guides.
- CI is green on `main`; three fixes were in test and CI setup rather than the platform.

## Phase 9: evaluation, recoverability and release hardening (2026-10-17)
- Hash-locked evaluation suites and a release gate; a load baseline and sizing; encrypted backups and a restore rehearsal; a shared limiter and cache invalidation; OIDC for people; secret rotation; dependency and image scanning with SBOMs; the production reference deployment; the live-session tooling for the owner; reconcile and reindex with atomic cut-over; the failure matrix.
- **Fixed:** a memory-search plan that turned two left joins into a nested loop with billions of comparisons on a 50,000-memory workspace.

## Phase 8: governance (2026-10-17)
- Corrections with an impact preview, identity split, memory consolidation, deletion with a purge gate, receipts, suppression and a deletion journal, legal holds, workspace settings and retention, ontology simulation, and the privacy audit.

## Phase 7: multi-agent, SDK and MCP (2026-10-17)
- Agent registration, ontology-driven subject ACLs, the publish model and labels, revocation, quotas, the Python SDK and the MCP server.

## Phase 6: the context engine (2026-10-17)
- `POST /v1/context/build`: planning, authorisation, ranking, packing under a hard budget, citations, deterministic unknowns, traces, re-materialisation, and the console's Context Lab.

## Phase 5: curated memory (2026-10-17)
- Episodic, semantic, procedural and working memory with scopes, policy, extraction, hybrid scoped search, review and the Memory Explorer.

## Phase 4: Graphiti processing, identity and temporal facts (2026-10-12)
- Extraction behind one adapter, identity resolution and merge, the conflict policy, review, and the Graph Explorer.

## Phases 0 to 3: spike, foundation, ontology and commands, ledger and outbox (2026-10-11)
- The compatibility spike and version pins; the API, auth, row-level security and the console shell; the ontology registry and deterministic commands; the interaction ledger, encryption and the outbox with effectively-once processing.
