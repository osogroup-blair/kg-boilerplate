# Phase 11: Public Site, Documentation and Playgrounds — Detailed Implementation Plan

**Roadmap:** [ROADMAP.md](../../ROADMAP.md). This is a new phase after Phase 10 ("Fully Working Examples"). Add it to the roadmap when this plan is accepted.

**Goal in one line:** an open-source developer who has never seen this repository can land on one URL, understand what the platform is for in two minutes, *see it work* without installing anything, try it against a real API in a sandbox, and then clone, run, extend and deploy it with the docs as their only guide.

**Decisions already made by the owner (2026-10-18):**
- **Audience:** open-source developers who evaluate the boilerplate and fork it to build their own agent-memory or knowledge-graph product. Operators and internal teams are served by the existing runbooks, linked from the site but not written for them.
- **Site stack:** Astro + Starlight in the repository (`apps/site`), static output, React islands for the interactive parts.
- **Playgrounds:** (1) static replay demos that need no backend, (2) a hosted live sandbox on the mock router, (3) an interactive API reference.
- **Deliverable:** this plan in `docs/plans/`, plus a shareable copy.

**Binding inputs:**
- [README.md](../../README.md), [examples/README.md](../../examples/README.md), [docs/architecture/overview.md](../architecture/overview.md), [docs/guides/extend.md](../guides/extend.md), [docs/guides/add-a-domain.md](../guides/add-a-domain.md), [docs/agent-integration.md](../agent-integration.md), [docs/runbooks/](../runbooks/), ADR-011..053, [docs/release/handoff.md](../release/handoff.md).
- `contracts/openapi.json` (109 paths, `bearerAuth`: `kgk_…` key or short-lived JWT, one error envelope).
- The Phase 10 examples: `examples/_kit`, `examples/sarah` (11 steps), `examples/domains/crm` (9-case benchmark), `examples/agents` (SDK + MCP + eight denials), the seven notebooks (`examples/notebooks/src/*.py`).
- The mock router (`tools/mock_router`): fixture replay, then the schema faker for `json_schema` requests, then echo; strict mode (`POST /__mock/strict`) fails an unmatched structured request with `fixture_missing`.
- The admin console (`apps/admin`): React 18 + Vite + Tailwind + Cytoscape. It already has a Graph Explorer, a Context Lab with act-as, Memory Explorer, Reviews, Deletions, Ontology editor and Simulate.
- The production reference (`deploy/compose/docker-compose.prod.yml`, `Caddyfile`, ADR-053) and the observability rules (`deploy/observability`).

**State at the time of writing (2026-10-18):**
- The repository is **public** on GitHub (`osogroup-blair/kg-boilerplate`), with no description, no homepage URL and **no LICENSE file**. That blocks an open-source launch (WP-A0).
- CI is green on `main` (`check`, `examples`, `scan`, `observability`). The release candidate and the owner actions in the handoff are still open; they do not block this phase's early work, but the public launch (WP-D4) waits for `v1.0.0-rc1` or states the pre-release status plainly.
- The examples run fully offline on the strict mock router, so everything the static demos need can be recorded deterministically.

**Status:** ready for a coding agent once the owner answers the WP-A0 decisions. Owner actions are marked **(owner)**: an agent prepares them but never fakes them.

---

## 1. Objective

1. **Landing page** that explains the problem, the approach and the proof in under two minutes, and routes each reader to their next step.
2. **Documentation site** built from the repository's existing Markdown, so nothing is written twice: Get started, Concepts, Tutorials, Guides, Reference, Operations, Project.
3. **Static playgrounds** (no backend, zero cost, zero risk), replaying recorded runs of the real platform: the Sarah scenario player, the context explorer, a time-travel slider, the permission lens, a graph viewer and the write-path timeline.
4. **Hosted sandbox:** a public, rate-limited instance on the mock router where a visitor gets a private, pre-seeded workspace for two hours, with keys for the API, the SDK, MCP and a sandbox build of the admin console.
5. **Interactive API reference** generated from `contracts/openapi.json`, with "try it" pointed at the sandbox or at `localhost:8000`, plus tested recipes for curl, the Python SDK and MCP.
6. **Open-source readiness:** licence, contribution and security policies, templates, repository metadata, changelog.
7. **Gate:** `make site-check` (build, link check, snippet tests, tape drift, accessibility, Lighthouse budgets) passes in CI. The sandbox passes the release leak suites before it is opened to the public.

---

## 2. Audience and user journeys

The site is designed backwards from four journeys. Every page belongs to at least one of them; a page that belongs to none is cut.

| # | Who | Time they give | Arrives with | Must leave with | Path |
|---|---|---|---|---|---|
| J1 | **Evaluator** (senior engineer, tech lead) | 2–5 min | "Is this better than rolling my own vector store plus a graph?" | A clear "what / why / how is it different", and proof that it works | Landing → *See it* (Sarah player) → Proof numbers → "Why not just RAG?" |
| J2 | **Tinkerer** | 15–30 min | Wants to poke at it without installing | Has built a context package in the sandbox and seen permissions and time work | Landing → Sandbox → guided tour → API reference "try it" |
| J3 | **Builder** (forks the repo) | 1 h – 1 day | Has a domain in mind (CRM, support, research) | Stack running locally, own ontology published, an agent reading context | Quickstart → Tutorial "your first domain" → Concepts as needed → Guides |
| J4 | **Integrator** (agent developer) | 30 min | Already has an agent (Claude Code, a LangGraph app, a custom loop) | The agent calls `context/build` and writes memories back, over the SDK or MCP | "Connect an agent" → SDK/MCP reference → `agent-integration` contract |

Operators (deploy, backups, rotation, incidents) get an **Operations** section that links the existing runbooks with short introductions; it is not rewritten for this phase.

---

## 3. Lessons and constraints that bind this phase (Y1–Y16)

| # | Constraint (source) | How P11 applies it |
|---|---|---|
| Y1 | The non-strict mock router answers an unknown structured request with the **smallest schema-valid instance** (`schema_faker.py`): free text yields empty or junk extraction | The sandbox **never** sends visitor free text through the LLM path. Ingestion in the sandbox is a menu of **scripted events** whose fixtures are loaded (strict mode on). Visitors can still write directly through deterministic paths: commands, explicit memories, corrections, deletions |
| Y2 | `/__mock/*` is an unauthenticated test control plane (faults, fixtures, strict mode, request log) | The sandbox edge **blocks** `/__mock/*`, and the mock router listens only on the internal network. A test in WP-C3 asserts a 404 from the public host |
| Y3 | Examples are strictly black-box; `tests/examples` enforces their imports (P10) | The replay capture (WP-C1) and the sandbox broker (WP-C3) reuse `examples/_kit` and call only HTTP, the SDK, MCP and the CLI. They never read the database |
| Y4 | Each example creates its own workspace with unique markers, and fixtures are global to the mock router (P10 X1) | Sandbox scripted events carry a per-session marker in the event text, so one visitor's fixture never answers another's prompt. Fixture packs are loaded once per pack, not per session |
| Y5 | A hidden id returns the same 404 as a missing one (P7) | The permission lens shows this on purpose. Sandbox error pages never reveal more than the API does |
| Y6 | The act-as header works only on `POST /v1/context/build` (P6) | The sandbox console's Context Lab keeps act-as; nothing else in the playgrounds relies on it |
| Y7 | Model-affecting changes need a recorded live gate (CLAUDE.md §5) | This phase changes no alias, price, prompt, ranker or Graphiti pin. Fixture packs for the sandbox are dev tooling. If a step seems to need a platform change, it goes in its own commit with its own test, and the gate rule is checked |
| Y8 | After any API change run `make openapi` (CLAUDE.md §4) | The API reference reads `contracts/openapi.json` at build time; a site build after `make openapi` picks it up. CI fails if the reference page and the spec disagree |
| Y9 | Search is the capacity limit; exact scan p95 is 2.6–3.6 s at 50 000 memories (DEC-11) | Sandbox workspaces are small (hundreds of memories at most), there is a cap on live sessions, and per-principal quotas are set low |
| Y10 | DEC-06 (graph tenancy) is recorded, not resolved: every workspace shares one Neo4j, isolated by group scoping | The sandbox runs on **its own host with its own databases**, never next to real data. Before launch, `make test-release` (the leak suites against two replicas) runs against the sandbox deployment |
| Y11 | Workspace deletion has a cooling-off period (`workspace_deletion_cooling_hours`, default 24) | The sandbox sets cooling to 0 in its own env file and deletes expired workspaces through the **governed** deletion path; a nightly full wipe is the backstop |
| Y12 | `make release-candidate` drops the dev volumes; never `make down`/`reset` mid-run (handoff) | Recording tapes (WP-C1) uses its own compose project (`kg-tapes`), not the dev stack, so it can run while someone else tests |
| Y13 | A full disk wedges Docker (handoff) | The tapes project is torn down after recording; the sandbox host has disk alerts (WP-C3) |
| Y14 | Generated notebooks are committed without outputs; a test fails when they drift (P10) | The site links the executed copies from CI artifacts (or renders them at build time from `examples/notebooks/out/`) and never commits outputs into `examples/` |
| Y15 | Do not commit secrets; `.local/`, `.env`, `deploy/compose/secrets/` are ignored (CLAUDE.md §5) | Recorded tapes are scrubbed: `kgk_…` keys, JWTs and request ids are replaced with stable placeholders. A test greps every tape for key prefixes |
| Y16 | Claims must be true and reproducible (the project's tone everywhere) | Every number on the landing page (leakage 0, budget 100 %, test counts, latency) is **generated** from `eval/reports/` and `eval/baselines/` at build time and links to its source; no hand-typed metrics |

---

## 4. Scope

**In scope:** everything in §1, the repository hygiene that an open-source launch needs, the CI and deployment for the static site, and the sandbox deployment files and broker.

**Out of scope:**
- Any change to platform behaviour. Small platform fixes the work uncovers (a CORS setting, a missing OpenAPI description) go in their own commit with a test.
- A live-router sandbox or "bring your own key". It costs money and sends visitor data to a model provider. Revisit after launch.
- Versioned documentation (one version, `main`, until `v1.0.0` ships; WP-A1 leaves the hook for it).
- Translations, a blog, a community forum (link GitHub Discussions instead).
- Marketing analytics beyond privacy-friendly page counts (D7).

---

## 5. Information architecture

```
/                               Landing page (WP-B1)
/see-it/                        Static playgrounds hub (WP-C2)
  /see-it/sarah/                  Scenario player: the 11 Sarah steps
  /see-it/context/                Context explorer: task × profile × budget
  /see-it/time/                   Time travel: as_of and known_at
  /see-it/permissions/            Permission lens: one question, four callers
  /see-it/graph/                  Graph viewer
  /see-it/write-path/             An event's journey through the stages
/sandbox/                       Hosted sandbox: start a session, guided tour (WP-C3)
/docs/
  get-started/
    introduction                  What it is, what it is not, when to use it
    quickstart                    10 minutes: clone → make up seed → make examples
    tour-of-the-console           Screens of the admin console with what each is for
    faq
  concepts/                       (WP-B3) one page each, each with a diagram
    architecture                  write path, read path, the services
    ledger-and-graph              Postgres ledger as truth, Graphiti/Neo4j as projection
    commands-vs-extraction        deterministic writes vs model-extracted proposals
    time                          valid time, record time, as_of, known_at, succession vs supersession
    identity                      resolution, merge, split, review
    curated-memory                types, scopes, visibility, proposals, TTL, consolidation
    context-engine                profiles, ranking, packing, budget, unknowns, conflicts, citations, traces
    multi-agent                   principals, roles, subject ACLs, publish model and labels, revocation
    governance                    corrections, deletion with receipts, suppression, legal hold, privacy audit
    ontology                      the YAML spec, the rule catalog, simulation, publish and rollback
    models-and-router             the gateway, the router contract, mock vs live, embeddings
    security-model                tenancy (RLS), keys and OIDC, encryption, egress, injection defences
  tutorials/                      (WP-B2) end to end, each runnable and tested
    first-domain                  model a support-desk domain from an empty ontology to a context package
    connect-an-agent-sdk          a Python agent that reads context and writes back
    connect-an-agent-mcp          Claude Code / Claude Desktop / any MCP client
    ingest-a-source               a custom ingestion source (webhook or batch)
    notebooks                     the seven notebooks, rendered with outputs
  guides/                         (WP-B5) task-oriented
    extend                        (existing docs/guides/extend.md)
    add-a-domain                  (existing)
    swap-the-router               point the gateway at another OpenAI-compatible router
    swap-the-embedding-model
    write-context-profiles
    evaluation-and-the-release-gate
    going-live-with-a-real-router
    deploy-to-production          (from docs/runbooks/deploy-production.md)
  reference/                      (WP-B4) generated wherever possible
    api                           interactive (Scalar) from contracts/openapi.json
    api-recipes                   tested curl / SDK / MCP snippets per task
    python-sdk                    generated from kg_platform_sdk docstrings
    mcp-tools                     generated from apps/mcp tool schemas
    cli                           generated from the Typer app (`kg --help` tree)
    make-targets                  generated from `make help`
    ontology-spec                 generated JSON Schema + annotated example
    rule-catalog                  generated from GET /v1/ontologies/rule-catalog (recorded)
    errors                        the error envelope and every code
    configuration                 every setting in kg_common/settings.py with default and range
    adrs                          ADR-011..053, rendered, with status badges
  operations/                     (WP-B5) short intros + links to docs/runbooks/*
  project/
    roadmap                       (ROADMAP.md, condensed) and status
    changelog
    contributing, security, code-of-conduct, license
```

**Navigation rules:** the top bar has *See it*, *Sandbox*, *Docs*, *API*, *GitHub*. Every docs page ends with "Next" links that follow its journey (§2). Every concept page links to the playground that shows it and the notebook that runs it.

---

## 6. Layout in the repository

```
apps/site/                        Astro + Starlight (WP-A1)
  astro.config.mjs  package.json  package-lock.json  tsconfig.json
  src/
    content/docs/                 hand-written pages that exist only on the site (landing copy, concepts, tutorials)
    content/generated/            written by scripts/site/*.py at build time; git-ignored
    components/                   Astro components (Hero, FeatureGrid, ProofNumbers, Diagram, …)
    islands/                      React islands: ScenarioPlayer, ContextExplorer, TimeSlider, PermissionLens, GraphView, StageTimeline, SandboxLauncher
    data/tapes/                   recorded replays (WP-C1), committed, JSON, schema-validated
    data/proof.json               generated from eval/ (WP-B1); git-ignored
    styles/
  public/                         favicons, social card, diagrams (SVG)
  tests/                          Vitest (islands), Playwright (pages, a11y), tape schema tests
scripts/site/                     Python, run through uv (WP-A2, WP-B4)
  import_docs.py                  docs/ + examples READMEs + ADRs → content/generated with frontmatter and rewritten links
  gen_reference.py                OpenAPI, SDK, MCP, CLI, make targets, settings, ontology JSON Schema, errors
  gen_proof.py                    eval reports → data/proof.json
  snippets.py                     extract and run tagged code blocks (WP-D1)
tools/replay_capture/             WP-C1: records tapes against a dedicated compose project
apps/sandbox/                     WP-C3: the sandbox broker (FastAPI), depends on kg_platform_sdk + httpx + examples/_kit only
deploy/sandbox/                   WP-C3: compose overlay, Caddyfile, env example, systemd timers, alert rules
.github/workflows/site.yml        WP-D2: build, test, deploy the static site
.github/ISSUE_TEMPLATE/, PULL_REQUEST_TEMPLATE.md   WP-B6
LICENSE  CONTRIBUTING.md  SECURITY.md  CODE_OF_CONDUCT.md  CHANGELOG.md   WP-B6
```

**Why the docs stay in `docs/`:** agents and engineers working in the repository read `docs/` directly (CLAUDE.md points there). The site *imports* them at build time rather than moving them, so there is one source and no drift. Pages that exist only for the site live in `apps/site/src/content/docs/`.

---

## 7. Work Packages

### Part A: Foundations

#### WP-A0: Owner decisions and prerequisites (owner)
The agent prepares a one-page decision sheet (`docs/release/decisions.md`, new entries DEC-13..DEC-19) with a recommendation for each; the owner decides. Work that does not depend on a decision starts in parallel.

| Id | Decision | Recommendation | Blocks |
|---|---|---|---|
| DEC-13 | **Licence** | Apache-2.0: permissive, explicit patent grant, the usual choice for infrastructure boilerplates. (MIT is simpler; AGPL protects a hosted product but deters forks.) | WP-B6, launch |
| DEC-14 | **Public name and domain** | A neutral product name rather than "kg-boilerplate", e.g. `kg.oso.group` for the site and `sandbox.kg.oso.group` for the sandbox (two hosts: the static site and the API never share an origin) | WP-A1 config, WP-C3, WP-D2 |
| DEC-15 | **Static hosting** | Cloudflare Pages (preview per PR, free, same provider as the router's edge). GitHub Pages is the fallback (no PR previews without extra work) | WP-D2 |
| DEC-16 | **Sandbox host and budget** | One dedicated VM, size S from the P9 sizing table (4 vCPU / 16 GB / 80 GB), its own Postgres and Neo4j; about €40–60 per month. Cap 40 concurrent sessions | WP-C3 |
| DEC-17 | **Sandbox abuse control** | Cloudflare Turnstile on "Start a session" (a site-owner check, invisible to most visitors), per-IP session limit (3 per hour), per-principal quotas | WP-C3 |
| DEC-18 | **Analytics** | None, or a cookieless counter (Plausible or Cloudflare Web Analytics). No cookie banner needed in either case | WP-D3 |
| DEC-19 | **Launch status wording** | Launch as "pre-1.0" with a status box linking the release checklist, unless `v1.0.0-rc1` is tagged first | WP-B1, WP-D4 |

Also (owner): set the GitHub repository description, homepage URL and topics; enable GitHub Discussions; decide who is the security contact for `SECURITY.md`.

**Done when:** each DEC has an owner answer recorded with a date.

#### WP-A1: Site scaffold (`apps/site`)
- Astro (current stable) + `@astrojs/starlight` + `@astrojs/react` + Tailwind (to share tokens with the admin console's `tailwind.config.js`). Pin exact versions in `package.json`, commit `package-lock.json`, Node 20 like the console.
- Starlight features to turn on: Pagefind search (built in, static), sidebar from the IA in §5, "Edit this page" links pointing at the **source** file (for imported pages, the file under `docs/`, not the generated copy), last-updated from git, dark/light theme.
- Theme: one accent colour, system font stack plus a monospace for code, the same graph colours as `apps/admin/src/graph/styles.ts` (import the file, do not copy it) so the graph viewer and the console look the same.
- Makefile targets (with `##` descriptions so `make help` lists them):
  - `site-install`: `npm ci` in `apps/site`
  - `site-gen`: runs `scripts/site/import_docs.py`, `gen_reference.py`, `gen_proof.py` through uv
  - `site-dev`: `site-gen` then `astro dev` on port **4321** (check it is free on this machine, like the console's 5273)
  - `site-build`: `site-gen` then `astro build` into `apps/site/dist`
  - `site-check`: the gate in WP-D1
- Versioning hook: put the docs under a `docs/` route segment and keep the sidebar config in one file, so a future `v1/` copy is a config change, not a restructure.
- `lint` and `typecheck` grow the site: eslint and `astro check`.
- **Done when:** `make site-install site-build` produces a site with the IA skeleton (every page exists, most say "coming in WP-…"), search works, and `make lint typecheck` stays green.

#### WP-A2: Docs import pipeline (`scripts/site/import_docs.py`)
- Inputs: `docs/architecture/overview.md`, `docs/guides/*.md`, `docs/agent-integration.md`, `docs/runbooks/*.md`, `docs/security/threat-model.md` (public sections only; see below), `docs/router-contract.md`, `docs/adr/*.md`, `examples/**/README.md`, `ROADMAP.md` (condensed: phase titles and status only), `CHANGELOG.md`.
- A mapping file `apps/site/import-map.yaml` lists each source → site path, title, sidebar group and order. A source that is not in the map is not published (an allowlist: `docs/release/handoff.md`, `docs/plans/`, `solution_inputs/` and anything internal stay off the site).
- Transformations: add Starlight frontmatter; rewrite relative links between imported files to site URLs; rewrite links to files that are not imported into GitHub URLs on `main`; rewrite `[file.py:42](...)` code links to GitHub blob links with line anchors; turn ADR headers into a status badge; strip the internal "for the next agent" asides marked with `<!-- internal -->`…`<!-- /internal -->` (add those markers to the sources where needed).
- **Security review of what goes public:** the repository is already public, so nothing here is secret, but the site makes it prominent. Read each imported file once for hostnames, machine paths (`/Users/…`, `/srv/lab/…`) and personal details; fix them in the source.
- Tests (`tests/site/test_import_docs.py`, pytest, no stack): every map entry exists; no published page has a broken internal link; no page contains `kgk_`, `/Users/`, `/srv/lab/` or an e-mail address other than the published security contact; re-running the import is byte-identical.
- **Done when:** the imported pages render with working links and the tests pass.

### Part B: Landing page and written content

#### WP-B1: Landing page (`/`)
Sections, in order. Copy rules: plain language, short sentences, no superlatives, every claim either demonstrated on the page or linked to its proof.

1. **Hero.** One-line value proposition, e.g. *"Memory and knowledge for agents that you can trust: sourced, time-aware, permissioned, and correctable."* One-sentence subline naming the stack (Postgres ledger, Graphiti/Neo4j graph, hybrid memory search, a context engine). Three buttons: **See it work** (`/see-it/sarah/`), **Try the sandbox**, **Quickstart**. Beside it, an auto-playing (muted, pausable, `prefers-reduced-motion` respected) loop of the Scenario Player showing step 6 (the 600-token context package).
2. **The problem.** Three short cards: agents forget or hallucinate history; vector stores have no notion of *when* something was true or *who* may see it; nothing lets you correct or delete a fact everywhere it spread.
3. **How it works.** One diagram (SVG, both themes): events and commands → ledger → graph projection and memory extraction → context engine → agents, with governance across the bottom. Each box links to its concept page.
4. **What you get.** A feature grid of 8–9 tiles, each with a one-line claim and a link to the playground that shows it: deterministic commands (zero model calls), extracted facts as reviewable proposals, bitemporal time (`as_of`, `known_at`), curated memory with scopes, the bounded and cited context package, multi-agent ACLs and publishing, corrections and deletion with receipts, configuration-only domains (the CRM proof), SDK + MCP + admin console.
5. **Proof.** Numbers generated by `gen_proof.py` (Y16): context leakage 0 across the frozen suite, budget compliance 100 %, attribution 100 %, ~900 authorization probes with 0 mismatches, the test counts (unit, integration, security, contract, console), CI status badge, the latency baseline p50/p95 with the "modelled router" caveat. Each number links to its report.
6. **Is this for you?** Two columns: *Use it when* (agents that act on people, customers or projects; facts change over time; several agents or apps share memory; you need audit and deletion) and *It is overkill when* (a single-user chatbot; static document Q&A; no need for time or permissions). Link to "Why not just RAG?" in the FAQ.
7. **Ten-minute quickstart.** The four commands from the README in a tabbed code block (macOS/Linux), with "no model key needed: the examples run on a deterministic mock router".
8. **Build your own.** Three cards for J3/J4: *Model your domain* (tutorial), *Connect your agent* (SDK/MCP), *Deploy it* (production guide).
9. **Status and roadmap.** The DEC-19 status box, a short list of what is done (P0–P10) and what is open (ANN search, DEC-06), link to the roadmap.
10. **Footer.** Licence, GitHub, security contact, changelog, "built by Oso Group".

Deliverables: `apps/site/src/pages/index.astro`, components under `src/components/landing/`, `scripts/site/gen_proof.py` with a test that fails when a referenced report is missing, the social card image (`public/og.png`, 1200×630) and favicons.
**Done when:** the page meets the Lighthouse budgets in WP-D1, reads well at 360 px wide, and every number traces to a file.

#### WP-B2: Get started and tutorials
- **Introduction:** what the platform is, the vocabulary in one table (workspace, principal, interaction, command, entity, relationship/fact, memory, context package, profile, ontology), what it is not.
- **Quickstart:** the README flow, expanded with expected output after each command, what to look at next (`make admin-dev`, sign in with a key from `.local/dev-credentials.json`), and a troubleshooting box (Docker memory, disk, ports 8000/5544/7475/7688/8782, `make reset`).
- **Tour of the console:** one screenshot per main screen (Dashboard, Ontology, Entities, Graph, Memories, Context Lab, Reviews, Deletions, Agents & Access, Operations), taken by a Playwright script (`apps/site/tests/screenshots.spec.ts`) against the seeded demo workspace so they can be regenerated; light and dark.
- **Tutorial: your first domain (support desk).** A new, small domain that is *not* Sarah or CRM, so the reader sees the general method: write the ontology YAML (Customer, Ticket, Product, Agent; `OPENED`, `ABOUT`, `ASSIGNED_TO`, `ESCALATED_TO`), validate and simulate it, publish it, send two commands and one conversation, define a profile, build a context. Ship it as `examples/domains/support/` (ontology, events, mock fixture pack, `run.py`) so it is runnable and covered by `make test-examples`. Follow `docs/guides/add-a-domain.md` and the P10 rule that a domain needs no platform code.
- **Tutorial: connect an agent (SDK):** a ~60-line agent loop using `KGClient`: build context for the task, put it in the prompt (the `agent-integration` placement rules), call the model, write back a memory proposal with provenance. Runs on the mock router; a `--live` note for owners.
- **Tutorial: connect an agent (MCP):** `make mcp-up`, then the Claude Code and Claude Desktop configs already in `examples/agents/mcp/`, the tool list, one worked conversation, the denial an outside agent sees.
- **Tutorial: ingest a source:** a webhook-style ingestion source posting interactions with `Idempotency-Key`, consent and classification fields; how to watch the stage timeline.
- **Notebooks page:** the seven notebooks rendered with outputs (convert the CI-executed copies with `nbconvert --to markdown` at build time; Y14), each with a "run it locally" box.
- Every tutorial's commands are tagged for the snippet runner (WP-D1).
- **Done when:** a person who has not seen the repository can follow the quickstart and the first-domain tutorial from the site alone (verified in WP-D4's fresh-reader test).

#### WP-B3: Concept pages
One page per concept in §5. Each page has the same shape so readers learn where to look:
1. *In one paragraph* (what and why),
2. *A diagram* (hand-made SVG, both themes, stored in `apps/site/public/diagrams/`; Mermaid only where a flowchart is enough),
3. *How it behaves*, with a short real request and response taken from a tape (not invented),
4. *Guarantees and limits* (what is enforced, what is not; link the ADRs),
5. *See it* (playground link), *Run it* (notebook link), *Configure it* (reference link).

Sources: `docs/architecture/overview.md` and `p1..p9-*.md`, the ADRs. Summarise; do not copy the phase documents (they are history, the concept page is the present).
**Done when:** each concept page exists, has its diagram, and its request/response examples come from tapes.

#### WP-B4: Reference (generated)
`scripts/site/gen_reference.py` writes Markdown or JSON into `content/generated/reference/`. Nothing here is hand-written except short intros.

| Page | Source | Method |
|---|---|---|
| API (interactive) | `contracts/openapi.json` | Scalar API Reference (static bundle, pinned version) on `/docs/reference/api/`. Servers: `https://sandbox.<domain>` and `http://localhost:8000`. Auth: bearer; the sandbox launcher can fill the key (WP-C4). Group operations by tag; if the spec lacks tags or summaries on some routes, add them in the API source (own commit, `make openapi`) |
| API recipes | hand-written, tested | 15–20 tasks (create an entity, end a relationship, save a memory, search, build context at a budget, `as_of`, correct a fact, request a deletion, register an agent, grant subject access, revoke). Each in three tabs: curl, Python SDK, MCP tool call |
| Python SDK | `sdk/python/src/kg_platform_sdk` | `griffe` (or `pdoc`) → Markdown: `KGClient`, `KGClientSync`, models, errors |
| MCP tools | `apps/mcp` tool definitions and `input_schema()` | a script imports the tool table and writes one section per tool with its JSON Schema and an example |
| CLI | the Typer app in `apps/cli` | walk the command tree, render `--help` for each command |
| Make targets | `Makefile` | parse the `target: ## description` lines (the same rule as `make help`) and group them |
| Configuration | `kg_common/settings.py` | introspect the pydantic settings: env var name, type, default, bounds, description |
| Ontology spec | the ontology models | export JSON Schema from the pydantic models, render it, plus an annotated example (`examples/ontologies/personal-knowledge*.yaml`) |
| Rule catalog | `GET /v1/ontologies/rule-catalog` | recorded by WP-C1 into a tape, rendered as a table |
| Errors | the error-code registry | the envelope shape, every code with HTTP status and meaning |
| ADRs | `docs/adr/` | through WP-A2, plus an index table with status |

Tests: the generator runs without the stack; the output is deterministic; a test fails if a route in the spec has no reference entry or a make target has no description.
**Done when:** every reference page is generated and `site-check` fails on drift.

#### WP-B5: Guides, operations, FAQ, glossary
- Guides: import `extend.md` and `add-a-domain.md`; write the missing short guides from §5 (swap the router, swap the embedding model, write context profiles, evaluation and the release gate, going live with a real router, deploy to production). Each one is mostly links into the existing runbooks and ADRs with the steps in order.
- Operations: one intro page plus the imported runbooks (startup, deploy-production, backup-restore, rotate-secrets, retention, subject-deletion, legal-hold, the outage and quarantine runbooks, upgrade-pin-dependencies).
- **FAQ:** Why not just RAG / a vector DB? Why both Postgres and Neo4j? Do I need Graphiti? Which models does it need, and can I use my own provider? What does it cost to run? How big can a workspace get (DEC-11, the ANN note)? How is tenant data isolated (RLS; DEC-06 stated honestly)? Can I delete someone completely? Is it production-ready (DEC-19)?
- **Troubleshooting:** the gotchas from the handoff in reader language (strict mock and missing fixtures, `make mcp-up` recreating services, disk, ports, the state-sensitive graph test).
- **Glossary:** every term used on the site, linked from first use on each page (Starlight's abbreviation or a small remark plugin).
- **Done when:** every IA entry under guides/operations/FAQ has content.

#### WP-B6: Open-source repository hygiene
- `LICENSE` per DEC-13, and SPDX headers only if the licence calls for them (Apache-2.0 does not require per-file headers; do not add them).
- `CONTRIBUTING.md`: how to set up (the handoff's machine setup), the Makefile targets to run before a PR, commit style, the live-gate rule for model-affecting changes, how to add an example or domain.
- `SECURITY.md`: private reporting (GitHub security advisories), the contact from WP-A0, scope (the sandbox is in scope; `/__mock` on a local stack is not).
- `CODE_OF_CONDUCT.md` (Contributor Covenant 2.1).
- `.github/ISSUE_TEMPLATE/` (bug, feature, question → Discussions) and `PULL_REQUEST_TEMPLATE.md` with the check list.
- `CHANGELOG.md` (Keep a Changelog) starting from the phase history: one entry per phase P0–P10 and the unreleased section.
- README: badges (CI, licence), a link to the site at the top, keep the rest.
- **(owner)** repository description, homepage, topics (`knowledge-graph`, `agent-memory`, `graphiti`, `neo4j`, `postgres`, `mcp`, `rag`, `llm`), social preview image (the same `og.png`).
- **Done when:** GitHub's "community standards" page is complete.

### Part C: Playgrounds

#### WP-C1: Replay capture (`tools/replay_capture`)
The static playgrounds replay **real** platform responses. This tool records them.

- **Runs against its own compose project** `kg-tapes` (Y12): `make tapes` brings up postgres, neo4j, mock router, migrate, api, worker under that project name on shifted ports, seeds, records, and tears down (`docker compose -p kg-tapes down -v`). It never touches the dev stack.
- **Scenarios recorded,** each driven through `examples/_kit` and the existing example code (Y3):
  - `sarah`: all 11 steps of `examples/sarah/steps.py`, each step's requests and responses, plus snapshots after each step: the entity list, Sarah's subgraph (`GET /v1/entities/{id}/graph`), and the interaction stage timelines.
  - `context-matrix`: for Sarah's world after step 7, `POST /v1/context/build` for every combination of 3 tasks × 3 profiles × 4 budgets (64, 250, 600, 1200) × 3 callers (planning agent, research agent, admin), and the trace of each (`/trace`). About 100 builds; small.
  - `time`: the facts about Sarah at `as_of` monthly from 2026-06 to 2027-06, and at two `known_at` points.
  - `permissions`: the same five questions asked by four callers (admin, planning agent, research agent with no grant on Michael, an agent from `other_demo`), including the byte-identical 404 bodies.
  - `crm`: the CRM benchmark's context builds and the graph snapshot.
  - `write-path`: one conversation event's interaction record polled through every stage, with timestamps normalised to offsets.
  - `rule-catalog`, `profiles`: single GETs for the reference.
- **Tape format** (`apps/site/src/data/tapes/<scenario>.json`), validated by a JSON Schema in `tools/replay_capture/tape.schema.json`:
  `{ "tape_version": 1, "scenario": "...", "recorded_with": {"git_sha": "...", "ontology": "...", "mock_fixtures": "..."}, "steps": [ { "id": "6", "title": "...", "narration": "...", "calls": [ {"method": "POST", "path": "/v1/context/build", "caller": "planning-agent", "request": {...}, "status": 200, "response": {...}} ], "snapshots": {"graph": {...}} } ] }`
- **Normalisation** so tapes are deterministic and diff cleanly: replace UUIDs with stable aliases in order of first appearance (`ent_1`, `mem_3`, …) consistently across a tape; replace timestamps that are not domain data with offsets; replace keys and JWTs with `kgk_example…` placeholders; replace `request_id`s; sort unordered collections the API documents as unordered.
- **Narration** (the human text shown beside each step) lives in a separate YAML per scenario (`tools/replay_capture/narration/*.yaml`) so it can be edited without re-recording.
- Tests: tape schema; the secret grep (Y15); recording twice produces identical tapes (run in CI's `examples` job as `make tapes-check`, which records into a temporary directory and diffs with the committed tapes).
- **Done when:** `make tapes` writes all tapes, `make tapes-check` is clean on a second run, and CI runs the check.

#### WP-C2: Static playgrounds (React islands under `/see-it/`)
All of them load a tape, run entirely in the browser, and show the **actual request and response** behind every view in a collapsible panel ("This is what the API returned"), with a "Run this yourself" button that copies the equivalent curl or SDK call.

| Island | What the visitor does | What it teaches | Tape |
|---|---|---|---|
| **ScenarioPlayer** (`/see-it/sarah/`) | Steps through Sarah's 11 steps with Next/Back; each step shows narration, the calls, and a before/after graph diff | The whole story: commands vs extracted facts, sharing between agents, a bounded context, a job change in 2027, denial, correction, deletion with a receipt, the outage variant | `sarah` |
| **ContextExplorer** (`/see-it/context/`) | Picks a task, a profile, a budget and a caller; sees the package: facts, memories with provenance, unknowns, conflicts, citations, a token bar against the budget, and the items that were left out and why (from the trace) | Ranking, packing, budgets, permission filtering | `context-matrix` |
| **TimeSlider** (`/see-it/time/`) | Drags `as_of` across 2026–2027 and toggles `known_at`; the facts list and the graph update | Valid time vs record time; succession | `time` |
| **PermissionLens** (`/see-it/permissions/`) | Asks one question as four callers side by side; differences are highlighted; the two 404 bodies are shown identical | Subject ACLs, the hidden-equals-missing rule, workspace isolation | `permissions` |
| **GraphView** (`/see-it/graph/`) | Pans and clicks a Sarah or CRM graph; clicking a node shows its facts, evidence and validity | The projection and provenance | `sarah`, `crm` snapshots |
| **StageTimeline** (`/see-it/write-path/`) | Plays one event through normalize → graph → memory → done, with what each stage wrote | The write path and the outbox | `write-path` |

- Reuse: the console's graph styling (`apps/admin/src/graph/styles.ts`) and, where they are presentational and free of API calls, components from `apps/admin/src/pages/context/ContextPanels.tsx` and `interactions/StageTimeline.tsx`. If they are coupled to the console's data layer, extract a small shared package (`packages-js/ui`, workspace-linked by both apps) in a separate commit; do not fork copies.
- Accessibility: every island is keyboard-operable, has text equivalents for the graph (a table toggle, as the console's `GraphTable.tsx` does), and honours `prefers-reduced-motion`.
- Performance: islands hydrate on visibility (`client:visible`); Cytoscape loads only on pages that show a graph; tapes are fetched, not inlined, and each is under 500 KB gzipped.
- Tests: Vitest for each island with a fixture tape (renders each step; budget bar sums match; permission diff highlights); Playwright walks every `/see-it/` page and runs axe.
- **Done when:** all six islands work with the committed tapes and pass their tests.

#### WP-C3: Hosted sandbox
**What a visitor gets:** "Start a sandbox" (Turnstile, DEC-17) → within ~10 s a private workspace `sbx-<random>` seeded with Sarah's world through step 7 (or, on request, the CRM world), valid for **2 hours**, and a session page with:
- three keys shown once: *admin-lite* (reviewer + reader roles in that workspace), *planning-agent*, *research-agent*; plus copy-paste snippets for curl, the SDK (`KG_BASE_URL`, `KG_API_KEY`) and an MCP client config pointing at the sandbox's MCP endpoint;
- a link to the **sandbox console** (the admin console built in sandbox mode) signed in with the admin-lite key;
- a **guided tour** of 8 tasks with checkmarks the page verifies through the API: build a context as the planning agent; change the budget; query `as_of` 2027-03; add an entity and a relationship by command (deterministic); save a memory and find it by search; correct a fact and see the next context change; request a deletion and get the receipt; ask as an agent without a grant and see the denial;
- a menu of **scripted events** (Y1) the visitor can "send", e.g. "Sarah mentions she started cycling", "A board meeting moved to March", each backed by a fixture pack, carrying the session's marker (Y4), and processed through the real extraction path on the mock router.

**Architecture:**
```
visitor ── site (static) ──► sandbox.<domain> (Caddy)
                                ├─ /sandbox/*  → broker (apps/sandbox)
                                ├─ /v1/*, /health → api ×2   (allowlisted routes only)
                                ├─ /mcp        → mcp
                                ├─ /console/*  → console static build (sandbox mode)
                                └─ /__mock/*, /v1/operations/*, metrics → 404
                   internal network: postgres, neo4j, worker, mock-router (strict, fixture packs preloaded)
```

**Steps:**
1. **Route × role matrix.** Generate it from the API's policy registry (every route declares a policy) and decide, route by route, what the visitor's roles may call. Expected: visitor keys get `reviewer` + `reader` (+ `agent` keys for the two agents); commands, memory saves, corrections and deletion requests are allowed in the visitor's own workspace if the matrix permits them for those roles. If a needed write requires `app_writer` or `workspace_admin`, the broker exposes a narrow proxy endpoint for exactly that operation rather than handing out the broader role. Record the matrix in `deploy/sandbox/README.md`.
2. **Broker (`apps/sandbox`, FastAPI, ~400 lines).** Endpoints: `POST /sandbox/sessions` (verifies Turnstile, checks per-IP and global caps, creates the workspace with `kg workspaces create`, publishes the ontology, seeds through the kit, registers principals with low quotas, returns keys and expiry), `GET /sandbox/sessions/{id}` (status, expiry, tour progress), `POST /sandbox/sessions/{id}/events/{script}` (sends a scripted event with the session marker, using a broker-held `app_writer` key scoped to that workspace), `DELETE /sandbox/sessions/{id}` (ends early). It stores sessions in its own small Postgres schema (`sandbox.sessions`: id, workspace slug, created, expires, ip hash, scenario). It imports only `kg_platform_sdk`, `httpx`, the stdlib and `examples/_kit` (import-linter contract, Y3).
3. **Seeding speed.** Seeding Sarah through step 7 takes about 15–20 s on the mock router. Keep a **warm pool** of N pre-seeded workspaces (N = 5, refilled in the background) so a session starts instantly; a pooled workspace's age counts toward nothing until it is handed out.
4. **Reaper.** A systemd timer every 5 min: sessions past expiry → revoke their keys at once, then governed workspace deletion (cooling 0 in the sandbox env, Y11), then mark the session gone. A nightly timer at 04:00 UTC wipes and rebuilds the whole sandbox (`down -v`, `up`, migrate, reload fixture packs, refill the pool) as the backstop. Both log to the usual observability stack.
5. **Deployment (`deploy/sandbox/`).** A compose overlay on `docker-compose.prod.yml`: adds the broker and the mock router (strict, fixture packs mounted read-only, internal network only, Y2), sets `cors_allow_origins` to the site origin, `workspace_deletion_cooling_hours=0`, low default quotas (e.g. 60 requests/min, 50 000 context tokens/day, 30 interactions/day per principal), `LIMITER_BACKEND=postgres`. A sandbox Caddyfile: the route allowlist above, request body max 256 KB, per-IP rate limit on `/sandbox/sessions`, the existing security headers, and a `Content-Security-Policy` for the console. Secrets from `make prod-secrets` into `deploy/sandbox/secrets/` (git-ignored, Y15).
6. **Sandbox console.** A build flag `VITE_SANDBOX=1` in `apps/admin`: a banner ("Public sandbox. Do not enter personal data. Deleted at HH:MM."), sign-in from a session token handed over in the URL fragment (never a query string) and kept in `sessionStorage`, Operations and Settings hidden, Agents & Access read-only. Served at `/console/`. Vitest covers the flag; the e2e suite gets one sandbox-mode smoke.
7. **Privacy and abuse.** The start page says the data is public-sandbox data, deleted within 2 hours, and must not be personal. Logs keep IP hashes, not IPs, for 7 days. The interaction content is already encrypted at rest. No analytics inside the sandbox.
8. **Observability.** Reuse `deploy/observability`, add sandbox rules: sessions active vs cap, pool size, reaper lag, 5xx rate, disk free under 20 %, search p95. Alerts go to the owner's channel.
9. **Pre-launch verification (owner reviews):** run `make test-release` (two replicas, leak suites, route walkers, injection, egress, revocation) against the sandbox deployment with sandbox settings; an external check that `/__mock/*`, `/v1/operations/*` and metrics answer 404 from the internet; a 30-minute soak with 40 concurrent synthetic sessions doing the guided tour (a small Locust or k6 script under `deploy/sandbox/load/`); a deliberate reaper test (a session expires, its key is refused within 5 min, its workspace is gone).

**Tests in the repository:** broker unit tests (caps, expiry, marker injection, refusal paths), an integration test that runs the broker against the local stack and completes the guided tour, the import contract, and a config test that the sandbox Caddyfile blocks the denied paths (`caddy validate` plus a request test in `make sandbox-smoke`, modelled on `make prod-smoke`).
**Done when:** `make sandbox-smoke` passes locally, the pre-launch verification is recorded in `docs/release/sandbox-<date>.md`, and the owner has deployed it (owner).

#### WP-C4: Interactive API reference and recipes
- Scalar on `/docs/reference/api/` (WP-B4), with a **"Use my sandbox key"** button: if the visitor has a live sandbox session (the site stores only the session id and expiry in `sessionStorage`), it fills the bearer token and selects the sandbox server.
- A **"Try these first"** strip above the reference: five calls that work on a fresh Sarah sandbox without any ids to look up (`GET /v1/me`, `GET /v1/context/profiles`, `POST /v1/context/build` with a ready body, `POST /v1/memories/search`, `POST /v1/graph/search`).
- **Recipes** (WP-B4) are the copy-paste counterpart for local use.
- The SDK and MCP references link each method or tool to its API operation.
- **Done when:** a visitor with a sandbox session can execute the five starter calls from the reference page.

### Part D: Quality, deployment and launch

#### WP-D1: The site gate (`make site-check`)
Runs in this order, each failing the target:
1. `site-gen` is deterministic (a second run leaves `content/generated` unchanged).
2. `astro check` and eslint.
3. `astro build`.
4. **Snippet tests** (`scripts/site/snippets.py`): every fenced block tagged ` ```bash test ` or ` ```python test ` in the site's own pages and the imported docs is extracted; offline-safe ones run in `make test`; the ones marked `stack` run in CI's `examples` job against the compose stack (quickstart commands, recipes, tutorials).
5. **Link check** with `lychee` on `dist/` (internal links strict; external links with retries, and a weekly scheduled run so a flaky external site never blocks a PR).
6. **Tape checks:** schema validation and the secret grep (the full re-record is `make tapes-check` in the `examples` job).
7. **Playwright:** every page loads with no console errors; axe reports no serious or critical issues; the islands' smoke paths.
8. **Lighthouse CI** budgets on `/`, `/see-it/sarah/`, `/docs/get-started/quickstart/`: performance ≥ 90, accessibility ≥ 95, best practices ≥ 95, SEO ≥ 95; landing page JavaScript under 150 KB gzipped before islands hydrate.

#### WP-D2: CI and deployment
- `.github/workflows/site.yml`, triggered on changes to `apps/site/**`, `docs/**`, `examples/**/README.md`, `contracts/openapi.json`, `sdk/**`, `apps/mcp/**`, `apps/cli/**`, `Makefile`, `eval/reports/**`, `scripts/site/**`, `tools/replay_capture/**`: install, `make site-check` (without the stack parts), upload `dist/`.
- The existing `examples` job grows `make tapes-check` and the `stack`-tagged snippet tests.
- Deploy per DEC-15: preview deployments on PRs, production on `main`. Deployment credentials live in GitHub repository secrets (owner sets them).
- The sandbox is deployed by hand by the owner (`deploy/sandbox/README.md` has the steps); there is no automatic deploy of anything that serves a public API.

#### WP-D3: Search, SEO, feedback
- Search: Pagefind (Starlight's default), with the reference pages indexed.
- SEO: titles and descriptions per page, `sitemap.xml`, canonical URLs, Open Graph and Twitter card from `og.png`, `robots.txt` that disallows `/sandbox/` session pages.
- Feedback: a "Was this page useful? / Suggest an edit" footer linking a prefilled GitHub issue or Discussion and the source file. No third-party widget.
- Analytics per DEC-18.

#### WP-D4: Launch
1. **Fresh-reader test:** someone who has not worked on the repository follows Quickstart → first-domain tutorial → connect-an-agent from the site only, on a clean machine, and notes every place they got stuck. Fix those before launch.
2. The sandbox's pre-launch verification (WP-C3 step 9) is recorded.
3. `make site-check` green in CI; Lighthouse budgets met.
4. LICENSE and the community files merged; repository metadata set **(owner)**.
5. DEC-19 status wording is on the landing page and the README.
6. Announcement draft (README "news" line, a short post for the owner to publish) **(owner publishes)**.
7. After launch: watch sandbox alerts for a week; triage issues labelled `docs` weekly.

---

## 8. Milestones and order

| Milestone | Contains | Can start | Visible result |
|---|---|---|---|
| **M1: Docs site live** | WP-A0 (decisions), A1, A2, B2 (quickstart and intro), B4 (generated reference without "try it"), B6, D2 (static deploy) | now | A real site with imported docs, generated reference and search |
| **M2: See it** | WP-C1, C2, B1 (landing page), B3 (concepts) | after A1 | The landing page and the six static playgrounds, no backend |
| **M3: Learn it** | the rest of B2 (tutorials, new support domain), B5, D1 in full, D3 | after M1 | Complete docs, tested snippets |
| **M4: Try it** | WP-C3, C4 | after DEC-16/18 and the M2 tapes (the guided tour reuses them) | Public sandbox in beta, "try it" in the API reference |
| **M5: Launch** | WP-D4 | after M1–M4 | Announced |

M1 and the tape recorder (WP-C1) are independent and can be worked in parallel sessions; WP-C2 depends on C1's tape format (agree the schema first, then both proceed with a fixture tape). WP-C3's broker and WP-B2's support-domain tutorial are independent of each other.

**Rough effort for one agent with owner review:** M1 3–4 days · M2 5–7 days · M3 4–5 days · M4 5–7 days plus the owner's deployment · M5 1–2 days. Roughly four working weeks.

---

## 9. Risks and mitigations

| Risk | Mitigation |
|---|---|
| The sandbox leaks one visitor's data to another (shared Neo4j, DEC-06) | Its own host and databases (Y10); the release leak suites against it before launch; 2-hour lifetime; "no personal data" notice; nightly wipe |
| Abuse: scripted load, storage filling, crypto-mining-style misuse of compute | No model calls cost money (mock router); Turnstile; per-IP and global caps; low quotas; body size limits; disk alert; nightly wipe |
| Visitors expect real LLM extraction from free text and find the mock underwhelming | Say so plainly on the sandbox page: extraction answers come from recorded fixtures; the platform runs against a real router when you deploy it; the scripted events show the real pipeline end to end |
| Tapes drift from the platform as it changes | `make tapes-check` in CI fails on any difference; re-record with `make tapes` in the same PR |
| Docs drift (the site says one thing, the code another) | Generated reference; snippet tests; imported docs instead of copies; link check |
| The landing page over-claims | Proof numbers generated from reports (Y16); "Is this for you?" states the limits; status box (DEC-19) |
| The console's components are too coupled to reuse in islands | Extract a small shared UI package in its own commit; do not fork |
| Port, disk or volume collisions with the dev stack while recording | Separate `kg-tapes` compose project on shifted ports, torn down after use (Y12, Y13) |
| No licence at launch makes the code legally unusable | DEC-13 blocks the launch milestone |

---

## 10. Acceptance (the phase is done when)

- [ ] DEC-13..19 are answered and recorded (owner).
- [ ] `make site-install site-check` passes locally and in CI; previews deploy on PRs, production on `main`.
- [ ] The landing page meets the Lighthouse budgets and every number on it links to a report.
- [ ] Every page in §5 exists with real content; every concept page has a diagram and tape-based examples; every reference page is generated.
- [ ] The six static playgrounds work from committed tapes; `make tapes-check` is clean in CI.
- [ ] The support-desk domain runs in `make test-examples` with no platform code changes.
- [ ] `make sandbox-smoke` passes; the sandbox pre-launch verification is recorded in `docs/release/sandbox-<date>.md`; the owner has deployed it.
- [ ] A visitor can go from the landing page to a working context build in the sandbox in under five minutes, and run the five starter calls from the API reference.
- [ ] LICENSE, CONTRIBUTING, SECURITY, CODE_OF_CONDUCT, templates and CHANGELOG are in place; GitHub's community-standards check is complete.
- [ ] The fresh-reader test is done and its findings are fixed.
- [ ] `ROADMAP.md` has a Phase 11 entry with its status; `docs/release/handoff.md` is updated.

---

## 11. Commits (the owner approves each)

One commit per work-package group, messages ending with the repository's Co-Authored-By line:
1. "Phase 11 (1/n): site scaffold, docs import, generated reference" (A1, A2, B4)
2. "Phase 11 (2/n): open-source repository files" (B6, after DEC-13)
3. "Phase 11 (3/n): replay capture and tapes" (C1)
4. "Phase 11 (4/n): landing page, concepts and static playgrounds" (B1, B3, C2)
5. "Phase 11 (5/n): tutorials, support domain, guides, site gate" (B2, B5, D1, D3)
6. "Phase 11 (6/n): sandbox broker, deployment and API try-it" (C3, C4)
7. "Phase 11 (7/n): CI and deploy, launch checklist" (D2, D4)

Platform fixes found along the way get their own commits with their own tests (§4).
