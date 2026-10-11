# Python 3.12 via uv. If uv is not on PATH, the repo-local copy in .tools/uvenv is used.
# Phase 0 spike targets are at the bottom; Phase 1+ targets (up/seed/test/check) come first.
UV      := $(shell command -v uv 2>/dev/null || echo $(CURDIR)/.tools/uvenv/bin/uv)
SPIKE_COMPOSE := docker compose -f spikes/docker-compose.spike.yml --env-file .env.example
SPIKE   := cd spikes && $(UV) run

ENVFILE := $(if $(wildcard .env),.env,.env.example)
KG_COMPOSE := docker compose -f deploy/compose/docker-compose.yml --env-file $(ENVFILE)
RUN := $(UV) run
PYSRC := packages apps tests scripts

.PHONY: sync up stack-up down reset seed demo-p1 demo-p2 demo-p3 logs ps admin-dev admin-install \
        test test-int test-security test-contract test-resilience test-all test-admin test-e2e lint typecheck openapi admin-client check

sync:                ## install the Python workspace (exact versions from uv.lock)
	$(UV) sync --frozen

# ---- stack ------------------------------------------------------------------------------------------------------------------------
up:                  ## build and start the stack: postgres, neo4j, mock-router, migrate, api, worker (ports: api 8000, pg 5544, neo4j 7475/7688, mock 8782)
	mkdir -p .local/faults && chmod 777 .local/faults
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
	$(RUN) kg seed demo --ontology examples/ontologies/personal-knowledge.yaml > /dev/null
	$(RUN) python scripts/demo_p3.py

logs:
	$(KG_COMPOSE) logs -f --tail=100

ps:
	$(KG_COMPOSE) ps

# ---- tests (the host-run suites need `make stack-up`; the targets fail loudly if it is missing) ----------------------------------------
test:                ## unit + offline contract tests (no stack needed)
	$(RUN) pytest packages apps tools/mock_router/tests -q

test-int:            ## integration tests against postgres + neo4j + mock router
	KG_REQUIRE_STACK=1 $(RUN) pytest tests/integration -q

test-security:       ## tenant-isolation, revocation and policy-registry tests
	KG_REQUIRE_STACK=1 $(RUN) pytest tests/security -q

test-contract:       ## endpoint, gateway and boundary contracts + OpenAPI drift
	KG_REQUIRE_STACK=1 $(RUN) pytest tests/contract tests/integration/test_api_contract.py packages/modelgw -q

test-resilience:     ## failure injection (FI-01..08): crashes, SIGKILL, lease expiry, postgres restart, neo4j outage. Needs `make stack-up`; restarts compose services
	KG_REQUIRE_STACK=1 KG_RESILIENCE=1 $(RUN) pytest tests/resilience -q -p no:cacheprovider

test-all: test test-int test-security test-contract

# ---- quality --------------------------------------------------------------------------------------------------------------------
lint:                ## ruff (lint + format check), import-linter boundaries, eslint
	$(RUN) ruff check $(PYSRC)
	$(RUN) ruff format --check $(PYSRC)
	$(RUN) lint-imports
	cd apps/admin && npm run --silent lint

typecheck:           ## mypy --strict on packages/ and apps/, tsc on the admin app
	$(RUN) mypy packages apps
	cd apps/admin && npm run --silent typecheck

openapi:             ## regenerate contracts/openapi.json and the admin TypeScript client from it
	$(RUN) kg openapi export
	cd apps/admin && npm run --silent generate:api

admin-install:
	cd apps/admin && npm ci

admin-dev:           ## Vite dev server on :5273 (proxies /v1 and /health to :8000); port 5273 because 5173/5174 are taken on this machine
	cd apps/admin && npm run dev

test-admin:          ## Vitest component tests
	cd apps/admin && npm test

test-e2e:            ## Playwright: P1 smoke + P2 ADM-01 (needs `make up seed`; runs demo-p2 first so the demo workspace has Sarah's story)
	$(MAKE) -s demo-p2 > /dev/null
	cd apps/admin && npm run e2e

check: lint typecheck test-all test-admin   ## everything except the Playwright smoke (run `make test-e2e` with the stack up)

# ---- Phase 0 spike targets ----------------------------------------------------------------------------------------------------------
.PHONY: tools spike-up spike-offline spike-report spike-down verify-router mock-router test-mock sync-catalog

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

verify-router:       ## LIVE kit; needs the router running locally: OSO_ROUTER_MODE=live OSO_ROUTER_API_KEY=... (see spikes/README.md)
	@$(UV) run --project spikes python -m tools.verify_router; rc=$$?; if [ $$rc -ne 2 ]; then $(MAKE) -s spike-report; fi; exit $$rc
