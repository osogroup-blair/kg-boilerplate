# Phase 0 spikes (disposable evidence code)

Everything here proves a compatibility question; none of it is imported by P1. The durable outputs are in `docs/` (`router-contract.md`, `adr/ADR-011..020`, `compatibility-report.md`), `compat/` and `tools/` (`mock_router`, `verify_router`).

## Run the offline checks

```bash
make tools          # only if `uv` is not installed (puts it in .tools/uvenv)
make spike-up       # neo4j 5.26, postgres 17 + pgvector, mock router (needs Docker; ~1 min first time)
make spike-offline  # all OFFLINE checks, ~6 min; writes spikes/results/*.json and docs/compatibility-report.md
make spike-down
```

Ports: Neo4j 7474/7687, Postgres **5543** (the usual 5432-5437 are taken on this machine), mock router 8781.

## Run the live router checks (when you are ready)

The Oso Model Router must be running locally (`./start-local.sh` in `oso_model_router`). Use a **non-admin** key with scope `chat` (the kit warns if the key is admin). The key is read from the environment only and is never printed or written.

```bash
make spike-up                      # LV-09 (Graphiti) needs Neo4j
export OSO_ROUTER_MODE=live
export OSO_ROUTER_BASE_URL=http://127.0.0.1:8780/v1
export OSO_ROUTER_API_KEY=...      # your kg key
make verify-router                 # LV-01..LV-10, then rebuilds the report
```

Cost: a few cents on the open models (about 30 Graphiti calls per episode x 3 episodes, a few dozen tiny probes). If the router is down it stops with `router not reachable at ...` (exit 2) and changes nothing. Manual step: LV-08 prints a request id to look up in the router Admin UI (Operations > Request Logs).
Afterwards: update the provisional ADRs (012, 013, 020) and `compat/model-purposes.yaml`, and review the recorded fixtures in `tools/mock_router/fixtures/recorded-sarah.json` (synthetic Sarah data only) before committing them.

## Layout

| Path | What |
|---|---|
| `gateway/` | ModelGateway prototype + G-01..G-05 |
| `graphiti/` | Graphiti wiring, ontology compiler, GR-01..GR-13, recorder |
| `pgvector_checks/` | PV-01..PV-04 (50k-row dataset) |
| `tokenizer/`, `auth/`, `networking/` | TK-01/02, AU-01, NW-01 |
| `results_plugin.py`, `results_report.py`, `checks_catalog.py` | turn test outcomes into `results/*.json` and the report |
