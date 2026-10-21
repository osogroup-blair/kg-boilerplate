# Live session 2026-10-21

## Steps

- (a) verify-router: LV-01..10, G-05, GR-11..13, TK-02: **FAIL (exit 1)**
- (b) eval-extraction-live: **FAIL (exit 1)**
- (c) eval-memory-live: **FAIL (exit 1)**
- (d) eval-context-live: **FAIL (exit 1)**
- (e) eval-consolidation-live: **PASS**
- (f) eval-simulation-live: **FAIL (exit 1)**
- (g) latency profile capture (router p50/p95 per alias): **PASS**
- (h) fixture recording refresh (LV-10): **FAIL (exit 1)**

## Calibration (proposals; nothing is applied until `python -m tools.live_session apply`)

- `CONTEXT_TOKEN_MARGIN`: {"margin": 0.85, "minimal_margin": 0.824, "headroom_pp": 2.0, "samples": 32, "covers_all": true}
- `SEMAPHORE_LIMIT`: {"semaphore_limit": 8}
- latency profile: written to `tools/mock_router/latency.live.json` on apply (use it with `MOCK_ROUTER_LATENCY_PROFILE`)

After applying: re-run `make eval-context` with the calibrated margin; budget adherence must stay at 100%. Failing thresholds are release blockers or exceptions the owner accepts (docs/release/decisions.md).
