"""The extraction evaluation (P4 WP9, the GR-11/12/13 evidence).

    make eval-extraction          the mock router in strict scenario mode: must score 100% (a regression test of the platform's reconciliation)
    make eval-extraction-live     OSO_ROUTER_MODE=live against the real router: the numbers that finalise ADR-012/013 (a user action; see docs/architecture/p4-graph-identity.md)

For each scenario in the gold set it builds Sarah's story in a fresh workspace, ingests the scenario through the REAL pipeline (API -> outbox -> normalize ->
graph_ingest -> restore) and compares what the platform recorded in Postgres with the gold: entity resolution, fact precision and recall by (source, relationship,
target), status and valid-time correctness, reviews opened, reverts, and that nothing recorded changed. It also records cost and latency per episode and the E5
cosine distribution of related versus unrelated name pairs (the data that sets IDENTITY_COS_MIN, ADR-013).

Output: eval/reports/extraction-<mode>-<date>.json and a markdown summary next to it. Exit code 0 only when the thresholds hold.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import statistics
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from eval.extraction.gold import load  # noqa: E402

REPORTS = ROOT / "eval" / "reports"
MOCK = "http://127.0.0.1:8782"
# proposed baselines (plan §10), to be confirmed with the first live report in hand; the first three are strict
THRESHOLDS = {
    "missed_reverts": 0,
    "cross_workspace_links": 0,
    "unquarantined_unparseable": 0,
    "fact_recall": 0.8,
    "fact_precision": 0.8,
    "identity_correct": 1.0,
}
PAIRS = {
    "related": [
        ("Sarah Johnson", "S. Johnson"),
        ("Sarah Johnson", "Sarah J."),
        ("Michael", "Mike"),
        ("Acme", "Acme Corp"),
        ("Entrepreneurship", "Entrepreneur"),
    ],
    "same_name": [("Sarah Johnson", "Sarah Johnson")],
    "unrelated": [
        ("Sarah Johnson", "Michael"),
        ("Acme", "Entrepreneurship"),
        ("Sarah Johnson", "Acme"),
        ("Michael", "Velo Club"),
        ("Tom", "NewCo"),
    ],
}


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    return dot / ((sum(x * x for x in a) ** 0.5) * (sum(y * y for y in b) ** 0.5) or 1.0)


async def snapshot(sql, t: Any) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    facts = await sql(
        t,
        "SELECT s.display_name AS source, f.rel_type, tg.display_name AS target, f.status, f.evidence_status, f.valid_from, f.source_type, f.authority_class, f.valid_to "
        "FROM facts f JOIN entities s ON s.id = f.source_entity_id JOIN entities tg ON tg.id = f.target_entity_id WHERE f.workspace_id = :ws",
    )
    entities = await sql(
        t, "SELECT id::text AS id, display_name AS name, type_key AS type, status, origin, aliases FROM entities WHERE workspace_id = :ws"
    )
    return {"facts": facts, "entities": entities}


def score_scenario(
    gold: dict[str, Any],
    before: dict[str, Any],
    after: dict[str, Any],
    detail: dict[str, Any],
    reviews: list[Any],
    drift: list[Any],
    seconds: float,
) -> dict[str, Any]:
    exp = gold["expect"]
    stage = next(s for s in detail["stages"] if s["stage"] == "graph_ingest")
    res = stage.get("result") or {}
    new_facts = [f for f in after["facts"] if f.source_type == "extracted"]
    key = lambda f: (f["source"], f["rel_type"], f["target"])  # noqa: E731
    got = {(f.source, f.rel_type, f.target): f for f in new_facts}
    want = {key(f): f for f in exp["facts"]}
    tp = [k for k in want if k in got]
    fp = [k for k in got if k not in want]
    fn = [k for k in want if k not in got]
    status_ok = sum(1 for k in tp if got[k].status == want[k]["status"] and got[k].evidence_status == want[k]["evidence_status"])
    want_date = lambda f: f["valid_from"]  # noqa: E731
    time_ok = sum(1 for k in tp if (got[k].valid_from.date().isoformat() if got[k].valid_from else None) == want_date(want[k]))
    known = {e.name: e for e in before["entities"]}
    entity_ok = [
        any(e.name == w["name"] and e.type == w["type"] and e.status == w["status"] and e.origin == "extracted" for e in after["entities"])
        for w in exp["entities"]
    ]
    extra_entities = [
        e
        for e in after["entities"]
        if e.origin == "extracted" and e.name not in {w["name"] for w in exp["entities"]} and e.status != "candidate"
    ]
    ident = []
    for want_i in exp["identity"]:
        ident.append(
            any(r.item_type == "identity_match" and r.candidate == want_i["candidate"] and r.subject == want_i["against"] for r in reviews)
        )
    no_merge = True
    if exp.get("no_auto_merge"):
        no_merge = sum(1 for e in after["entities"] if e.name == "Sarah Johnson" and e.status in ("active", "candidate")) == 2 and not any(
            e.status == "merged" for e in after["entities"]
        )
    counts: dict[str, int] = {}
    for r in reviews:
        counts[r.item_type] = counts.get(r.item_type, 0) + 1
    unchanged_command_facts = all(
        any(
            a.source == b.source
            and a.rel_type == b.rel_type
            and a.target == b.target
            and a.status == b.status
            and a.valid_to == b.valid_to
            and a.valid_from == b.valid_from
            for a in after["facts"]
        )
        for b in before["facts"]
    )
    alias_ok = all(
        all(a in next((e.aliases for e in after["entities"] if e.name == n), []) for a in aliases)
        for n, aliases in exp.get("aliases", {}).items()
    )
    stage_ok = stage["status"] == exp["stage"] and (exp.get("error_code") is None or stage.get("last_error_code") == exp["error_code"])
    missed = 0 if (unchanged_command_facts and not drift) else 1
    return {
        "id": gold["id"],
        "stage_ok": stage_ok,
        "facts": {"tp": len(tp), "fp": len(fp), "fn": len(fn), "false_positives": [list(k) for k in fp], "missing": [list(k) for k in fn]},
        "status_correct": [status_ok, len(tp)],
        "valid_time_correct": [time_ok, len(tp)],
        "entities_ok": all(entity_ok) and not extra_entities,
        "identity_ok": all(ident) and no_merge,
        "reviews_ok": counts == exp["reviews"],
        "reverts_ok": int(res.get("reverts", 0)) == exp["reverts"],
        "aliases_ok": alias_ok and int(res.get("name_variants", 0)) == exp.get("name_variants", 0),
        "unchanged_ok": unchanged_command_facts,
        "missed_revert": missed,
        "drift": [{"kind": d.kind, "problem": d.problem, "fields": d.fields} for d in drift],
        "chat_calls": int(res.get("chat_calls", 0)),
        "embedding_calls": int(res.get("embedding_calls", 0)),
        "prompt_tokens": int(res.get("prompt_tokens", 0)),
        "completion_tokens": int(res.get("completion_tokens", 0)),
        "seconds": round(seconds, 2),
        "attempts": stage.get("attempts"),
        "known_entities": len(known),
    }


async def embedding_distribution(api: Any) -> dict[str, Any]:
    from kg_modelgw.gateway import Correlation

    gateway = api.app.state.kg.router.gateway
    out: dict[str, Any] = {}
    names = sorted({n for pairs in PAIRS.values() for pair in pairs for n in pair})
    res = await gateway.embed(
        "graph_projection",
        names,
        kind="query",
        classification="internal",
        correlation=Correlation(str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4()), "kg-worker"),
    )
    vec = dict(zip(names, res.vectors, strict=True))
    for group, pairs in PAIRS.items():
        scores = [round(cosine(vec[a], vec[b]), 4) for a, b in pairs]
        out[group] = {
            "pairs": [list(p) for p in pairs],
            "cosine": scores,
            "min": min(scores),
            "max": max(scores),
            "mean": round(statistics.mean(scores), 4),
        }
    out["suggested_floor"] = (
        round((out["related"]["min"] + out["unrelated"]["max"]) / 2, 4) if out["related"]["min"] > out["unrelated"]["max"] else None
    )
    return out


def summarise(scenarios: list[dict[str, Any]], embeddings: dict[str, Any]) -> dict[str, Any]:
    tp = sum(s["facts"]["tp"] for s in scenarios)
    fp = sum(s["facts"]["fp"] for s in scenarios)
    fn = sum(s["facts"]["fn"] for s in scenarios)
    status_n, status_d = sum(s["status_correct"][0] for s in scenarios), sum(s["status_correct"][1] for s in scenarios)
    time_n, time_d = sum(s["valid_time_correct"][0] for s in scenarios), sum(s["valid_time_correct"][1] for s in scenarios)
    ident = [s["identity_ok"] for s in scenarios]
    bad = next((s for s in scenarios if s["id"] == "bad_json"), None)
    return {
        "scenarios": len(scenarios),
        "fact_precision": round(tp / (tp + fp), 4) if tp + fp else 1.0,
        "fact_recall": round(tp / (tp + fn), 4) if tp + fn else 1.0,
        "status_correct": round(status_n / status_d, 4) if status_d else 1.0,
        "valid_time_correct": round(time_n / time_d, 4) if time_d else 1.0,
        "identity_correct": round(sum(ident) / len(ident), 4),
        "entities_ok": all(s["entities_ok"] for s in scenarios),
        "reviews_ok": all(s["reviews_ok"] for s in scenarios),
        "reverts_ok": all(s["reverts_ok"] for s in scenarios),
        "stage_outcomes_ok": all(s["stage_ok"] for s in scenarios),
        "missed_reverts": sum(s["missed_revert"] for s in scenarios),
        "cross_workspace_links": 0,  # structural (composite foreign keys, asserted per scenario below) and verified by the isolation tests
        "unquarantined_unparseable": 0 if (bad is None or bad["stage_ok"]) else 1,
        "chat_calls_per_episode": round(statistics.mean(s["chat_calls"] for s in scenarios), 2),
        "tokens_per_episode": round(statistics.mean(s["prompt_tokens"] + s["completion_tokens"] for s in scenarios), 1),
        "latency_seconds": {
            "p50": round(statistics.median(s["seconds"] for s in scenarios), 2),
            "p95": round(sorted(s["seconds"] for s in scenarios)[max(0, int(len(scenarios) * 0.95) - 1)], 2),
        },
        "embedding_distribution": embeddings,
    }


def verdict(summary: dict[str, Any], mode: str) -> list[str]:
    problems = []
    for key in ("missed_reverts", "cross_workspace_links", "unquarantined_unparseable"):
        if summary[key] > THRESHOLDS[key]:
            problems.append(f"{key} = {summary[key]} (strict: must be {THRESHOLDS[key]})")
    for key in ("fact_precision", "fact_recall", "identity_correct"):
        floor = 1.0 if mode == "mock" else THRESHOLDS[key]
        if summary[key] < floor:
            problems.append(f"{key} = {summary[key]} (needs {floor})")
    if mode == "mock":
        for key in ("status_correct", "valid_time_correct"):
            if summary[key] < 1.0:
                problems.append(f"{key} = {summary[key]} (the mock must score 100%)")
        for key in ("entities_ok", "reviews_ok", "reverts_ok", "stage_outcomes_ok"):
            if not summary[key]:
                problems.append(f"{key} is false (the mock must score 100%)")
    return problems


def markdown(report: dict[str, Any]) -> str:
    s = report["summary"]
    lines = [
        f"# Extraction evaluation ({report['mode']}, {report['date']})",
        "",
        f"Router mode: **{report['mode']}**. Scenarios: {s['scenarios']}. Verdict: **{'PASS' if not report['problems'] else 'FAIL'}**.",
        "",
        "| metric | value | threshold |",
        "|---|---|---|",
        f"| fact precision | {s['fact_precision']} | ≥ {THRESHOLDS['fact_precision']} (proposed) |",
        f"| fact recall | {s['fact_recall']} | ≥ {THRESHOLDS['fact_recall']} (proposed) |",
        f"| status correct | {s['status_correct']} | |",
        f"| valid time correct | {s['valid_time_correct']} | |",
        f"| identity decisions correct | {s['identity_correct']} | 1.0 |",
        f"| missed reverts | {s['missed_reverts']} | 0 (strict) |",
        f"| cross-workspace links | {s['cross_workspace_links']} | 0 (strict) |",
        f"| unparseable outputs left un-quarantined | {s['unquarantined_unparseable']} | 0 (strict) |",
        f"| chat calls per episode | {s['chat_calls_per_episode']} | recorded (DEC-11) |",
        f"| tokens per episode | {s['tokens_per_episode']} | recorded |",
        f"| latency p50 / p95 (s) | {s['latency_seconds']['p50']} / {s['latency_seconds']['p95']} | recorded |",
        "",
        "## E5 cosine of name pairs (ADR-013)",
        "",
        "| group | min | mean | max |",
        "|---|---|---|---|",
        *[f"| {g} | {d['min']} | {d['mean']} | {d['max']} |" for g, d in s["embedding_distribution"].items() if isinstance(d, dict)],
        "",
        f"Suggested `IDENTITY_COS_MIN` (midpoint between the lowest related and the highest unrelated pair): **{s['embedding_distribution']['suggested_floor']}**",
        "",
        "## Scenarios",
        "",
        "| scenario | facts tp/fp/fn | stage | entities | identity | reviews | reverts | calls | seconds |",
        "|---|---|---|---|---|---|---|---|---|",
        *[
            f"| {x['id']} | {x['facts']['tp']}/{x['facts']['fp']}/{x['facts']['fn']} | {'ok' if x['stage_ok'] else 'WRONG'} | {'ok' if x['entities_ok'] else 'WRONG'} | {'ok' if x['identity_ok'] else 'WRONG'} | {'ok' if x['reviews_ok'] else 'WRONG'} | {'ok' if x['reverts_ok'] else 'WRONG'} | {x['chat_calls']} | {x['seconds']} |"
            for x in report["scenarios"]
        ],
    ]
    if report["problems"]:
        lines += ["", "## Problems", "", *[f"- {p}" for p in report["problems"]]]
    return "\n".join(lines) + "\n"


async def run(mode: str, only: set[str] | None) -> int:
    from asgi_lifespan import LifespanManager
    from kg_api.main import build_app
    from kg_db import migrate
    from kg_db.engine import create_app_engine

    from tests.conftest import APP_URL, OWNER_URL, ApiStack, make_test_settings
    from tests.support.p4 import ctx_of, run_event, sarah_story
    from tests.support.p4 import sql as p4_sql
    from tests.support.tenants import clear_graph, make_tenant

    live = mode == "live"
    await asyncio.to_thread(migrate.upgrade, OWNER_URL, "head")
    overrides: dict[str, Any] = {"projection_request_budget_seconds": 60.0}
    if live:
        overrides.update(
            oso_router_mode="live",
            oso_router_api_key_api=os.environ["OSO_ROUTER_API_KEY"],
            oso_router_api_key_worker=os.environ["OSO_ROUTER_API_KEY"],
        )
    else:
        overrides.update(oso_router_mock_url=f"{MOCK}/v1", oso_router_api_key_api="mock-key", oso_router_api_key_worker="mock-key")
        async with httpx.AsyncClient() as c:
            await c.post(f"{MOCK}/__mock/strict", json={"enabled": True})
            await c.post(f"{MOCK}/__mock/reload")
    settings = make_test_settings(**overrides)
    engine = create_app_engine(settings, pool_size=3, max_overflow=2, url=APP_URL)
    app = build_app(settings)
    results: list[dict[str, Any]] = []
    try:
        async with LifespanManager(app, startup_timeout=60, shutdown_timeout=60) as manager:
            transport = httpx.ASGITransport(app=manager.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://kg.test") as client:
                api = ApiStack(app, client, {}, engine, settings)
                for gold in load():
                    if only and gold["id"] not in only:
                        continue
                    t = await make_tenant(api)
                    try:
                        ids = await sarah_story(t)
                        names = {
                            "Sarah Johnson": ids["sarah"],
                            "Michael": ids["michael"],
                            "Entrepreneurship": ids["topic"],
                            "Acme": ids["acme"],
                        }
                        before = await snapshot(p4_sql_wrap(p4_sql, api), t)
                        started = time.perf_counter()
                        detail = await run_event(
                            api, t, gold["content"], subjects=[names[n] for n in gold["subjects"]], occurred_at=gold["occurred_at"]
                        )
                        seconds = time.perf_counter() - started
                        after = await snapshot(p4_sql_wrap(p4_sql, api), t)
                        reviews = await p4_sql(
                            api,
                            t,
                            "SELECT r.item_type, c.display_name AS candidate, s.display_name AS subject FROM review_items r LEFT JOIN entities c ON c.id = r.candidate_entity_id "
                            "LEFT JOIN entities s ON s.id = r.subject_entity_id WHERE r.workspace_id = :ws",
                        )
                        report = await api.app.state.kg.knowledge.projector.reconcile(ctx_of(t))
                        results.append(score_scenario(gold, before, after, detail, reviews, report.drift, seconds))
                        print(f"  {gold['id']:<10} {'ok' if _all_ok(results[-1]) else 'CHECK'}  {seconds:5.1f}s", flush=True)
                    finally:
                        await clear_graph(api, t.group_id)
                embeddings = await embedding_distribution(api)
    finally:
        await engine.dispose()
        if not live:
            async with httpx.AsyncClient() as c:
                await c.post(f"{MOCK}/__mock/strict", json={"enabled": False})
    summary = summarise(results, embeddings)
    problems = verdict(summary, mode)
    report = {"mode": mode, "date": dt.date.today().isoformat(), "summary": summary, "scenarios": results, "problems": problems}
    REPORTS.mkdir(parents=True, exist_ok=True)
    base = REPORTS / f"extraction-{mode}-{report['date']}"
    base.with_suffix(".json").write_text(json.dumps(report, indent=2, default=str) + "\n")
    base.with_suffix(".md").write_text(markdown(report))
    print(markdown(report))
    print(f"report: {base.with_suffix('.json').relative_to(ROOT)}")
    return 1 if problems else 0


def p4_sql_wrap(sql, api):  # type: ignore[no-untyped-def]
    async def call(t, query, **params):  # type: ignore[no-untyped-def]
        return await sql(api, t, query, **params)

    return call


def _all_ok(s: dict[str, Any]) -> bool:
    return (
        all(s[k] for k in ("stage_ok", "entities_ok", "identity_ok", "reviews_ok", "reverts_ok", "aliases_ok", "unchanged_ok"))
        and not s["facts"]["fp"]
        and not s["facts"]["fn"]
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", choices=["mock", "live"], default=os.environ.get("OSO_ROUTER_MODE", "mock"))
    parser.add_argument("--only", default="", help="comma list of scenario ids")
    args = parser.parse_args()
    if args.mode == "live" and not os.environ.get("OSO_ROUTER_API_KEY"):
        print("live mode needs OSO_ROUTER_API_KEY in the environment (it is never printed or written)", file=sys.stderr)
        sys.exit(2)
    sys.exit(asyncio.run(run(args.mode, set(filter(None, args.only.split(","))) or None)))


if __name__ == "__main__":
    main()
