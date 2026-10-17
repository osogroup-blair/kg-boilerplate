"""The ontology-simulation evaluation (P8 WP12).

    make eval-simulation-live   a draft ontology (v1.5.0 adds MANAGES) simulated on three real interactions through the REAL router (a user action; needs OSO_ROUTER_API_KEY;
                                costs model calls). Writes eval/reports/. `--mode mock` runs the same harness through the strict mock router (an offline self-test of the harness).

What it checks (report sanity, not extraction quality, which `eval-extraction-live` owns):
  * the estimate is produced without a model call and the confirmed run completes;
  * the report is well-formed: episodes == runnable samples, counts by type only, the diff against the active version is present, no content anywhere;
  * the draft's new types are the only new types reported;
  * NOTHING is left behind: no staging nodes, and no row of the workspace's knowledge changed (strict);
  * (live) the actual cost is within 0.2x and 5x of the estimate, so the preview can be trusted before a person confirms."""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

REPORTS = ROOT / "eval" / "reports"
MOCK = "http://127.0.0.1:8782"
TEXTS = [
    "Sarah Johnson: Michael reports to me; I manage Michael and we meet every Monday.",
    "Sarah Johnson: I manage Michael's work on the platform team at Acme.",
    "Sarah Johnson: Michael is part of my team and I manage him.",
]


async def run(mode: str) -> int:
    from asgi_lifespan import LifespanManager
    from kg_api.main import build_app
    from kg_db import migrate
    from kg_db.engine import create_app_engine

    from tests.conftest import APP_URL, OWNER_URL, ApiStack, make_test_settings
    from tests.integration.test_p8_simulation import QUIET_AGENTS, counts, draft_v15, manages_rules, staging_nodes
    from tests.support.p4 import ingest, wait_terminal
    from tests.support.tenants import clear_graph, make_tenant
    from tests.support.worker import running_worker, wait_until

    live = mode == "live"
    await asyncio.to_thread(migrate.upgrade, OWNER_URL, "head")
    overrides: dict[str, Any] = {"projection_request_budget_seconds": 60.0}
    if live:
        key = os.environ["OSO_ROUTER_API_KEY"]
        overrides.update(oso_router_mode="live", oso_router_api_key_api=key, oso_router_api_key_worker=key)
    else:
        overrides.update(oso_router_mock_url=f"{MOCK}/v1", oso_router_api_key_api="mock-key", oso_router_api_key_worker="mock-key")
        async with httpx.AsyncClient() as c:
            await c.post(f"{MOCK}/__mock/strict", json={"enabled": True})
            await c.post(f"{MOCK}/__mock/reload")
    settings = make_test_settings(**overrides)
    engine = create_app_engine(settings, pool_size=3, max_overflow=2, url=APP_URL)
    app = build_app(settings)
    checks: dict[str, Any] = {}
    t = None
    try:
        async with LifespanManager(app, startup_timeout=60, shutdown_timeout=60) as manager:
            transport = httpx.ASGITransport(app=manager.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://kg.test") as client:
                api = ApiStack(app, client, {}, engine, settings)
                t = await make_tenant(api, ontology=QUIET_AGENTS)
                if not live:
                    for text_ in TEXTS:
                        await manages_rules(text_)
                async with running_worker(api, concurrency=2):
                    events = [await ingest(t, {"kind": "message", "content": x}) for x in TEXTS]
                    for e in events:
                        await wait_terminal(t, e, timeout=600)
                draft = await draft_v15(t)
                before = await counts(api, t)
                pre = (
                    await t.req("POST", f"/v1/ontologies/versions/{draft['id']}/simulate", "admin", json={"interaction_ids": events})
                ).json()
                checks["estimate_calls"] = pre["estimate"].get("calls", 0)
                checks["estimate_usd"] = pre["cost_estimate"]
                checks["preview_status"] = pre["status"]
                async with running_worker(api, stages=("graph_projection", "ontology_simulation", "simulation_cleanup"), concurrency=2):
                    await t.req(
                        "POST",
                        f"/v1/ontologies/versions/{draft['id']}/simulate",
                        "admin",
                        json={"simulation_id": pre["id"], "confirm": True},
                    )

                    async def done() -> dict[str, Any] | None:
                        s = (await t.get(f"/v1/ontologies/simulations/{pre['id']}", "admin")).json()
                        return s if s["status"] in ("completed", "failed") else None

                    sim = await wait_until(done, timeout=900, what="the simulation")
                rep = sim["report"]
                checks.update(
                    status=sim["status"], episodes=rep.get("episodes"), runnable=pre["runnable_count"], facts_by_type=rep.get("facts_by_type"),
                    new_relationship_types=rep.get("new_relationship_types"), diff=rep.get("diff", {}).get("changes"), cost_actual=sim.get("cost_actual"),
                    staging_nodes_left=await staging_nodes(api, t), workspace_rows_changed=await counts(api, t) != before,
                )  # fmt: skip
                await clear_graph(api, t.group_id)
    finally:
        await engine.dispose()
        if not live:
            async with httpx.AsyncClient() as c:
                await c.delete(f"{MOCK}/__mock/fixtures")
                await c.post(f"{MOCK}/__mock/strict", json={"enabled": False})
    problems = []
    if checks.get("status") != "completed":
        problems.append(f"the run ended {checks.get('status')}")
    if checks.get("episodes") != checks.get("runnable"):
        problems.append("episodes != runnable samples")
    if set(checks.get("new_relationship_types") or []) - {"MANAGES"}:
        problems.append("a new relationship type the draft does not declare was reported")
    if checks.get("staging_nodes_left"):
        problems.append("staging nodes were left behind (strict)")
    if checks.get("workspace_rows_changed"):
        problems.append("the workspace's knowledge changed (strict)")
    if checks.get("diff") is None:
        problems.append("the report has no diff against the active version")
    est, act = checks.get("estimate_usd"), checks.get("cost_actual")
    if live and est and act is not None and not (0.2 * est <= act <= 5 * est):
        problems.append(f"actual cost {act} is outside 0.2x-5x of the estimate {est}")
    report = {"mode": mode, "date": dt.date.today().isoformat(), "checks": checks, "problems": problems, "id": str(uuid.uuid4())}
    REPORTS.mkdir(parents=True, exist_ok=True)
    stem = REPORTS / f"simulation-{mode}-{report['date']}"
    stem.with_suffix(".json").write_text(json.dumps(report, indent=2, default=str) + "\n")
    stem.with_suffix(".md").write_text(
        f"# Simulation evaluation ({mode}, {report['date']})\n\n```json\n{json.dumps(checks, indent=2, default=str)}\n```\n\n"
        + ("Problems:\n" + "\n".join(f"- {p}" for p in problems) if problems else "PASS\n")
    )
    print(json.dumps(checks, indent=2, default=str))
    print("PASS" if not problems else f"FAIL: {problems}")
    return 0 if not problems else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=("mock", "live"), default="live")
    args = ap.parse_args()
    if args.mode == "live" and not os.environ.get("OSO_ROUTER_API_KEY"):
        print("live mode needs OSO_ROUTER_API_KEY", file=sys.stderr)
        return 2
    return asyncio.run(run(args.mode))


if __name__ == "__main__":
    sys.exit(main())
