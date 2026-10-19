# Python 3.12 via uv. If uv is not on PATH, the repo-local copy in .tools/uvenv is used.
# Phase 0 spike targets are at the bottom; Phase 1+ targets (up/seed/test/check) come first.
UV      := $(shell command -v uv 2>/dev/null || echo $(CURDIR)/.tools/uvenv/bin/uv)
SPIKE_COMPOSE := docker compose -f spikes/docker-compose.spike.yml --env-file .env.example
SPIKE   := cd spikes && $(UV) run

ENVFILE := $(if $(wildcard .env),.env,.env.example)
KG_COMPOSE := docker compose -f deploy/compose/docker-compose.yml --env-file $(ENVFILE)
RUN := $(UV) run
# the live-router targets read the router key from .env if it exists (a real environment variable still wins when it is already set)
LIVE_ENV := set -a; [ ! -f .env ] || . ./.env; set +a;
PYSRC := packages apps sdk tests scripts examples

.PHONY: examples-install examples demo-sarah demo-crm eval-crm examples-agents notebooks test-examples help sync up stack-up down reset seed demo-p1 demo-p2 demo-p3 demo-p4 demo-p5 demo-p6 demo-p7 demo-p8 mcp-up sdk-models eval-extraction eval-extraction-live eval-memory eval-memory-live eval-context eval-context-live eval-consolidation eval-consolidation-live eval-simulation-live logs ps admin-dev admin-install \
        test test-int test-security test-contract test-resilience test-all test-admin test-e2e lint typecheck openapi admin-client check \
        backup-init backup backup-verify backup-prune restore-rehearsal reconcile eval-all eval-gate eval-gate-live eval-conflicts eval-authorization eval-injection eval-freeze

sync:                ## install the Python workspace (exact versions from uv.lock)
	$(UV) sync --frozen

# ---- stack ------------------------------------------------------------------------------------------------------------------------
examples:            ## run every example: the Sarah scenario, the CRM domain and the agents (starts the MCP server first; needs `make up seed examples-install`)
	$(MAKE) mcp-up
	$(MAKE) demo-sarah demo-crm examples-agents

demo-sarah:          ## example: the Sarah two-agent scenario in a fresh workspace, with a pass/fail report (needs `make up seed`)
	$(RUN) python -m examples.sarah.run

demo-crm:            ## example: the second domain (Project / Customer CRM), config only, with its benchmark (needs `make up seed`)
	$(RUN) python -m examples.domains.crm.run

eval-crm:            ## the CRM benchmark on its own: leakage 0, budget 100%, attribution 100% (needs `make up seed`)
	$(RUN) python -m examples.domains.crm.run --eval

examples-agents:     ## example: Research and Planning agents through the SDK and MCP, plus the unauthorized-agent tour (needs `make up seed mcp-up`)
	$(RUN) python -m examples.agents.run

notebooks:           ## execute the seven notebooks against the stack and save the executed copies, with outputs, to examples/notebooks/out/ (needs `make up seed examples-install`)
	$(RUN) --group examples python -m examples.notebooks.run --write

examples-install:    ## install the optional notebook tooling (jupyterlab, nbclient, networkx, matplotlib) into the environment
	$(UV) sync --frozen --group examples

test-examples:       ## run the examples and notebooks as tests (needs `make up seed examples-install`; the agents test also needs `make mcp-up`)
	$(RUN) --group examples pytest tests/examples -q

help:                ## list the targets that carry a ## description
	@grep -hE '^[a-zA-Z0-9_-]+:.*## ' $(MAKEFILE_LIST) | sed -E 's/:[^#]*## /\t/' | sort | column -t -s "$$(printf '\t')"

up:                  ## build and start the stack: postgres, neo4j, mock-router, migrate, api, worker (ports: api 8000, pg 5544, neo4j 7475/7688, mock 8782)
	mkdir -p .local/faults && chmod 777 .local/faults
	mkdir -p .local/deletion-journal && chmod 777 .local/deletion-journal
	$(KG_COMPOSE) up -d --build --wait

stack-up:            ## start only the backing services (what host-run tests and `make demo-p1` against a host API need)
	$(KG_COMPOSE) up -d --build --wait postgres neo4j mock-router

down:                ## stop the stack (keeps volumes)
	$(KG_COMPOSE) down

reset:               ## stop the stack and DROP its volumes (clean start)
	$(KG_COMPOSE) down -v --remove-orphans
	rm -f .local/dev-credentials.json

seed:                ## idempotent demo + other_demo workspaces; prints new API keys once and writes .local/dev-credentials.json (mode 0600)
	$(RUN) kg seed demo

demo-p1:             ## P1 demo: isolation, role gating, revocation, readiness (needs `make up seed`)
	$(RUN) python scripts/demo_p1.py

demo-p2:             ## P2 demo: ontology publish/rollback, deterministic commands, zero-LLM projection (needs `make up seed`; start from `make reset up seed` for the full first run)
	$(RUN) python scripts/demo_p2.py

demo-p3:             ## P3 demo: idempotent ledger, encryption at rest, SIGKILL mid-burst, Neo4j outage, dead letter + audited replay (needs `make up seed`; `make reset up seed demo-p3` for the first run)
	$(RUN) kg seed demo --no-memory-extraction --ontology examples/ontologies/personal-knowledge.yaml > /dev/null
	$(RUN) python scripts/demo_p3.py

demo-p4:             ## P4 demo: Graphiti extraction as a proposal, identity + review, time-aware facts, outage/unparseable/consent drills (needs `make up seed`; `make reset up seed demo-p1 demo-p2 demo-p3 demo-p4` for the first run)
	$(RUN) kg seed demo --no-memory-extraction --ontology examples/ontologies/personal-knowledge.yaml > /dev/null
	$(RUN) python scripts/demo_p4.py
	$(RUN) python -m eval.extraction.run_eval --mode mock

demo-p5:             ## P5 demo: curated memory: typed policy, extraction, explicit save, proposals, scopes, working memory, supersede, TTL, outage drill, leak check (needs `make up seed`; `make reset up seed demo-p1 demo-p2 demo-p3 demo-p4 demo-p5` for the first run)
	$(RUN) kg seed demo --ontology examples/ontologies/personal-knowledge-memory.yaml > /dev/null
	$(RUN) python scripts/demo_p5.py
	$(RUN) python -m eval.memory.run_eval --mode mock

demo-p6:             ## P6 demo: the task-aware context engine: the planning task at 600 / 250 / 64 tokens, as_of, the distractor, insufficient evidence, denied, a Neo4j outage, traces, re-materialisation (needs `make up seed`; `make reset up seed demo-p1 demo-p2 demo-p3 demo-p4 demo-p5 demo-p6` for the first run)
	$(RUN) kg seed demo --ontology examples/ontologies/personal-knowledge-context.yaml > /dev/null
	$(RUN) python scripts/demo_p6.py
	$(RUN) python -m eval.context.run_eval --mode mock

demo-p7:             ## P7 demo: the blueprint's two-agent scenario, offline: registration, subject ACLs (ontology v1.4.0), the publish model and labels, SDK + MCP, quotas, compromised revocation (needs `make up seed`; `make reset up seed demo-p1 demo-p2 demo-p3 demo-p4 demo-p5 demo-p6 demo-p7` for the first run)
	$(RUN) kg seed demo --ontology examples/ontologies/personal-knowledge-agents.yaml > /dev/null
	$(RUN) python scripts/demo_p7.py
	$(RUN) python -m eval.context.run_eval --mode mock

demo-p8:             ## P8 demo: governance: simulate and publish a draft ontology, corrections with impact preview, split, consolidation, deletion with a receipt, legal hold, containment, the privacy audit (needs `make up seed`; run-unique data, repeatable; `make reset up seed demo-p1 demo-p2 demo-p3 demo-p4 demo-p5 demo-p6 demo-p7 demo-p8` for the first run)
	$(RUN) kg seed demo --ontology examples/ontologies/personal-knowledge-agents.yaml > /dev/null
	$(RUN) python scripts/demo_p8.py

mcp-up:              ## start the MCP server container (profile `agents`, port 8010); it forwards each caller's own key to the API and holds no credential
	$(KG_COMPOSE) --profile agents up -d --build --wait mcp

sdk-models:          ## regenerate sdk/python/src/kg_platform_sdk/models.py from contracts/openapi.json (the drift test fails when it is stale)
	$(RUN) python scripts/gen_sdk_models.py

eval-extraction:     ## extraction evaluation against the scenario gold set through the strict mock router (offline, deterministic)
	$(RUN) python -m eval.extraction.run_eval --mode mock

eval-extraction-live: ## extraction evaluation through the REAL router (user action; needs OSO_ROUTER_API_KEY; costs model calls). Writes eval/reports/
	$(LIVE_ENV) OSO_ROUTER_MODE=live $(RUN) python -m eval.extraction.run_eval --mode live

eval-memory:         ## memory evaluation against the scenario gold set through the strict mock router (offline, deterministic; must score 100%)
	$(RUN) python -m eval.memory.run_eval --mode mock

eval-memory-live:    ## memory evaluation through the REAL router (user action; needs OSO_ROUTER_API_KEY; costs model calls). Writes eval/reports/
	$(LIVE_ENV) OSO_ROUTER_MODE=live $(RUN) python -m eval.memory.run_eval --mode live

eval-context:        ## context evaluation (16 frozen cases: 14 in Sarah's world, 2 with subject ACLs enforced) through the strict mock router (offline, deterministic): leakage 0, budget 100%, attribution 100%; ARGS="--ablate proximity" zeroes one ranking weight
	$(RUN) python -m eval.context.run_eval --mode mock $(ARGS)

eval-context-live:   ## context evaluation through the REAL router (user action; needs OSO_ROUTER_API_KEY; costs model calls). Writes eval/reports/
	$(LIVE_ENV) OSO_ROUTER_MODE=live $(RUN) python -m eval.context.run_eval --mode live $(ARGS)

eval-consolidation:  ## consolidation evaluation (reinforce clustering and the synthesis validator over a labelled gold set), offline and deterministic: every strict counter must be 0
	$(RUN) python -m eval.consolidation.run_eval --mode mock

eval-consolidation-live: ## the same synthesis groups answered by the REAL router (user action; needs OSO_ROUTER_API_KEY; costs model calls). Writes eval/reports/
	$(LIVE_ENV) OSO_ROUTER_MODE=live $(RUN) python -m eval.consolidation.run_eval --mode live

eval-simulation-live: ## ontology simulation sanity through the REAL router: well-formed report, cost within 0.2x-5x of the estimate, nothing left behind (user action; needs OSO_ROUTER_API_KEY; costs model calls)
	$(LIVE_ENV) OSO_ROUTER_MODE=live $(RUN) python -m eval.simulation.run_eval --mode live

eval-conflicts:      ## conflicts + identity: 151 frozen cases through the pure policy code (offline, no stack): every mismatch is a failure
	$(RUN) python -m eval.conflicts.run_eval

eval-authorization:  ## authorization: ~900 generated probes (every route x caller class, foreign administrator, frozen and deleted subject): policy mismatches, leakage and 5xx must all be 0
	$(RUN) python -m eval.authorization.run_eval

eval-injection:      ## injection: the frozen SEC-04 / AT-15 corpus through every ingress; every failed check is a failure
	$(RUN) python -m eval.injection.run_eval

eval-freeze:         ## (re)freeze the hash-locked datasets: eval/datasets/<suite>/v1/manifest.json (an owner act; then `python -m eval.gate --write-baseline`)
	$(RUN) python -m eval.conflicts.generate
	$(RUN) python -m eval.authorization.generate
	$(RUN) python -m eval.datasets freeze

eval-all:            ## every suite against the strict mock (offline, deterministic) -> eval/reports/<date>/all.json
	$(RUN) python -m eval.all

eval-gate:           ## compare the newest eval-all with eval/baselines/release.json: strict metrics must be exact, the others may not regress past tolerance
	$(RUN) python -m eval.gate

# ---- load baselines (P9 WP4): the kg-perf compose project has its own ports and volumes and never touches the dev stack ---------------------------------------
perf-up:             ## start kg-perf with the mock router's PROVISIONAL response-time model
	$(RUN) python -m eval.load.run up

perf-seed:           ## S/M/L workspaces + background tenants (bulk SQL; the L workspace has 200,000 memories)
	$(RUN) python -m eval.load.run seed --sizes S,M,L

perf-baseline:       ## steady / burst / search / long-document scenarios + size curve + freshness + cost; writes eval/reports/baseline-<date>.{json,md,svg}
	$(RUN) python -m eval.load.run baseline --size M --rps 4 --seconds 90 --burst-seconds 30 --settle 300

perf-down:           ## remove kg-perf and its volumes
	$(RUN) python -m eval.load.run down

test-release:        ## the release sweep: leak suites, route walkers, injection, egress, revocation, trigger gates, sentinels - against TWO API replicas; report keyed by SEC-01..06 / AT-01..18 (eval/reports/<date>/release-sweep.*)
	$(RUN) python scripts/test_release.py

release-candidate:   ## EVERY gate, in order, from a clean tree (drops the dev volumes!); writes docs/release/rc-report-<date>.md. Does not tag: the owner signs docs/release/checklist.md first
	$(RUN) python scripts/release_candidate.py $(ARGS)

live-session:        ## OWNER ACTION: the one run against the REAL router (preflight, cost estimate, steps a-h, calibration, proposed diff). Needs OSO_ROUTER_MODE=live, OSO_ROUTER_BASE_URL, a NON-ADMIN OSO_ROUTER_API_KEY. Then read eval/reports/live/<date>/proposal.diff and `make live-session-apply`
	$(LIVE_ENV) OSO_ROUTER_MODE=live $(RUN) python -m tools.live_session run $(ARGS)

live-session-apply:  ## write the reviewed proposal (.env.example thresholds, model-purposes status, ADR-012/013/020 status, latency profile)
	$(RUN) python -m tools.live_session apply

obs-test:            ## promtool: check the alert rules and unit-test them (deploy/observability/rules_test.yml); uses the pinned prometheus image
	docker run --rm -v $(CURDIR)/deploy/observability:/obs:ro --entrypoint promtool prom/prometheus@sha256:efd719c99d83b060d9daefdcf00360461adf279f45ef5391f8d111892118753e check rules /obs/rules.yml
	docker run --rm -v $(CURDIR)/deploy/observability:/obs:ro --entrypoint promtool prom/prometheus@sha256:efd719c99d83b060d9daefdcf00360461adf279f45ef5391f8d111892118753e test rules /obs/rules_test.yml

prod-secrets:        ## create deploy/compose/secrets (0600, git-ignored): generated credentials for the production compose; never overwrites
	$(RUN) python scripts/prod_secrets.py

prod-smoke:          ## bring docker-compose.prod.yml up under kg-prod-smoke with generated secrets and prove its claims (TLS, read-only, internal networks, non-root, break-glass); needs `make up` (mock router)
	$(RUN) python scripts/prod_smoke.py

scan:                ## dependency, image, filesystem and secret scanning + SBOMs; HIGH/CRITICAL blocks unless an unexpired exception exists (docs/security/scan-exceptions.yaml)
	$(RUN) python scripts/scan.py

scan-third-party:    ## the same, plus the Postgres and Neo4j images (their findings are the vendors', tracked in the dependency-refresh runbook)
	$(RUN) python scripts/scan.py --third-party

eval-gate-live:      ## the same gate against the REAL router (user action; costs model calls): the record CI requires for a change to an alias, a price, a prompt, a ranker/packer version or a Graphiti pin
	$(LIVE_ENV) OSO_ROUTER_MODE=live $(RUN) python -m eval.all --mode live
	$(RUN) python -m eval.gate --live

logs:
	$(KG_COMPOSE) logs -f --tail=100

ps:
	$(KG_COMPOSE) ps

# ---- tests (the host-run suites need `make stack-up`; the targets fail loudly if it is missing) ----------------------------------------
test:                ## unit + offline contract tests (no stack needed)
	$(RUN) pytest packages apps sdk tools/mock_router/tests tools/backup/tests eval/tests -q

test-int:            ## integration tests against postgres + neo4j + mock router
	KG_REQUIRE_STACK=1 $(RUN) pytest tests/integration -q

test-security:       ## tenant-isolation, revocation and policy-registry tests
	KG_REQUIRE_STACK=1 $(RUN) pytest tests/security -q

test-contract:       ## endpoint, gateway and boundary contracts + OpenAPI drift
	KG_REQUIRE_STACK=1 $(RUN) pytest tests/contract tests/integration/test_api_contract.py packages/modelgw -q

test-resilience:     ## failure injection (FI-01..23): crashes, SIGKILL, lease expiry, postgres and neo4j restarts, router outage and latency, killed replicas, clock skew. Needs `make stack-up`; restarts compose services
	KG_REQUIRE_STACK=1 KG_RESILIENCE=1 $(RUN) pytest tests/resilience -q -p no:cacheprovider

test-all: test test-int test-security test-contract

# ---- backup, restore and reconciliation (P9) -------------------------------------------------------------------------------------------
# Development keys live in .local/backup-keys (gitignored): `data.age` is the RECOVERY identity (keep it OFF the machine that makes backups in production), `escrow.age` is the key-escrow identity.
BACKUP_DATA_PUB   = $$(grep 'public key' .local/backup-keys/data.age | cut -d' ' -f4)
BACKUP_ESCROW_PUB = $$(grep 'public key' .local/backup-keys/escrow.age | cut -d' ' -f4)

backup-init:         ## generate the development age identities (data recovery + key escrow) once
	@mkdir -p .local/backup-keys
	@test -f .local/backup-keys/data.age || $(RUN) python -m tools.backup keygen .local/backup-keys/data.age
	@test -f .local/backup-keys/escrow.age || $(RUN) python -m tools.backup keygen .local/backup-keys/escrow.age

backup: backup-init  ## encrypted backup set: Postgres, Neo4j (offline dump, ~20 s of graph downtime), configuration; keys to the escrow location (BACKUP_DIR, BACKUP_ESCROW_DIR)
	BACKUP_RECIPIENTS="$${BACKUP_RECIPIENTS:-$(BACKUP_DATA_PUB)}" BACKUP_ESCROW_RECIPIENTS="$${BACKUP_ESCROW_RECIPIENTS:-$(BACKUP_ESCROW_PUB)}" $(RUN) python -m tools.backup backup

backup-verify:       ## hashes of the newest backup set against its manifest
	$(RUN) python -m tools.backup verify

backup-prune:        ## remove backup sets older than BACKUP_RETENTION_DAYS (always keeps the newest three)
	$(RUN) python -m tools.backup prune

restore-rehearsal:   ## AT-18: restore the newest backup into the separate kg-restore project, replay the deletion journal, reconcile, verify, report RTO/RPO (eval/reports/restore-<date>.md)
	$(RUN) python -m tools.backup rehearse --identity "$${BACKUP_IDENTITY:-.local/backup-keys/data.age}"

reconcile:           ## drift between the ledger, the graph, the memory store and the deletion records: make reconcile WS=demo [ARGS="--repair"]
	$(RUN) kg reconcile --workspace $(or $(WS),demo) $(ARGS)

# ---- quality --------------------------------------------------------------------------------------------------------------------
lint:                ## ruff (lint + format check), import-linter boundaries, eslint
	$(RUN) ruff check $(PYSRC)
	$(RUN) ruff format --check $(PYSRC)
	$(RUN) lint-imports
	cd apps/admin && npm run --silent lint

typecheck:           ## mypy --strict on packages/ and apps/, tsc on the admin app
	$(RUN) mypy packages apps sdk/python/src
	cd apps/admin && npm run --silent typecheck

openapi:             ## regenerate contracts/openapi.json, the SDK models and the admin TypeScript client from it
	$(RUN) kg openapi export
	$(RUN) python scripts/gen_sdk_models.py
	cd apps/admin && npm run --silent generate:api

admin-install:
	cd apps/admin && npm ci

admin-dev:           ## Vite dev server on :5273 (proxies /v1 and /health to :8000); port 5273 because 5173/5174 are taken on this machine
	cd apps/admin && npm run dev

test-admin:          ## Vitest component tests
	cd apps/admin && npm test

test-e2e:            ## Playwright: P1 smoke, P2 ADM-01, P3 ADM-06, P4 ADM-02, P5 Memory Explorer, P6 Context Lab (ADM-04), P7 Agents & Access, P8 governance (needs `make up seed`; runs demo-p2, demo-p4, demo-p5 and demo-p6 first so the demo workspace has Sarah's story)
	$(MAKE) -s demo-p2 > /dev/null
	$(MAKE) -s demo-p4 > /dev/null
	$(MAKE) -s demo-p5 > /dev/null
	$(MAKE) -s demo-p6 > /dev/null
	$(MAKE) -s demo-p7 > /dev/null
	$(RUN) kg seed demo --ontology examples/ontologies/personal-knowledge-context.yaml > /dev/null   # back to v1.3.0: the P6 specs assert open fact access; the P7 spec activates v1.4.0 itself and restores this one
	cd apps/admin && npm run e2e

check: lint typecheck test-all test-admin   ## everything except the Playwright smoke (run `make test-e2e` with the stack up)

# ---- Phase 0 spike targets ----------------------------------------------------------------------------------------------------------
.PHONY: release-candidate live-session live-session-apply obs-test prod-secrets prod-smoke test-release perf-up perf-seed perf-baseline perf-down scan scan-third-party tools spike-up spike-offline spike-report spike-down verify-router mock-router test-mock sync-catalog

tools:               ## install uv into .tools/uvenv if it is not on PATH (needs python3 >= 3.9; uv then fetches Python 3.12)
	@command -v uv >/dev/null 2>&1 || test -x $(CURDIR)/.tools/uvenv/bin/uv || (python3 -m venv .tools/uvenv && .tools/uvenv/bin/pip install -q uv)

spike-up:            ## start neo4j, postgres(pgvector), mock-router and wait for health
	$(SPIKE_COMPOSE) up -d --build --wait

spike-offline:       ## run every OFFLINE check; writes spikes/results/*.json
	@find spikes/results -maxdepth 1 -name '*.json' ! -name 'live-*' -delete
	$(SPIKE) pytest -q -m "not live"
	$(SPIKE) python -m results_report --write

spike-report:        ## rebuild docs/compatibility-report.md from spikes/results/*.json
	$(SPIKE) python -m results_report --write

spike-down:
	$(SPIKE_COMPOSE) down -v

mock-router:         ## run the mock router on the host (127.0.0.1:8781)
	$(UV) run --project spikes uvicorn tools.mock_router.app:create_app --factory --host 127.0.0.1 --port 8781

test-mock:
	$(SPIKE) pytest -q ../tools/mock_router/tests

sync-catalog:        ## regenerate tools/mock_router/catalog.json from the router seed
	$(UV) run --project spikes python -m tools.mock_router.sync_catalog

verify-router:       ## LIVE kit against https://models.oso.group: reads OSO_ROUTER_API_KEY from .env (or the environment); a handful of small calls
	@$(LIVE_ENV) OSO_ROUTER_MODE=live $(UV) run --project spikes python -m tools.verify_router; rc=$$?; if [ $$rc -ne 2 ]; then $(MAKE) -s spike-report; fi; exit $$rc
