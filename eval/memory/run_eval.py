"""The memory evaluation (P5 WP7, the live-gate evidence).

    make eval-memory          the mock router in strict scenario mode: must score 100% (a regression test of the platform's filtering and policy)
    make eval-memory-live     OSO_ROUTER_MODE=live against the real router: the numbers that finalise ADR-030/031 (a user action; costs model calls)

PART 1, extraction: for each scenario in the gold set (tools/mock_router/fixtures/scenarios/memory_*.json) it builds Sarah's story in a fresh workspace, ingests the
scenario through the REAL pipeline (API -> outbox -> normalize -> graph_ingest -> memory_propose -> embed) and compares what the platform recorded with the gold:
candidate precision and recall, drop reasons, and the three STRICT counters (escalation violations, unmapped-subject leaks, procedural auto-activations: all 0).

PART 2, retrieval (RET-02): one workspace with the extracted memories plus unrelated filler; a small labelled query set; recall@3 of the gold memory, whether a distractor
reaches the top 3, and the cosine distribution of gold / distractor / filler (the data that sets MEMORY_VEC_MIN_COSINE, ADR-013).

Live runs are not repeatable by nature, so `--repeat N` runs every scenario N times in fresh workspaces and reports the mean and range, and `--semantic` (on by default in live
mode) lets a judge model accept a correct paraphrase of a `must_contain` phrase (eval/memory/judge.py); the offline run is unchanged.

Output: eval/reports/memory-<mode>-<date>.json and a markdown summary next to it. Exit code 0 only when the thresholds hold."""

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

from eval.memory.gold import load  # noqa: E402
from eval.memory.judge import Judge  # noqa: E402
from eval.memory.queries import FILLERS, QUERIES  # noqa: E402
from eval.memory.score import score  # noqa: E402

REPORTS = ROOT / "eval" / "reports"
MOCK = "http://127.0.0.1:8782"
# proposed baselines (plan §10), to be confirmed with the first live report in hand; the three counters are strict
THRESHOLDS = {
    "escalation_violations": 0,
    "unmapped_subject_leaks": 0,
    "procedural_auto_activations": 0,
    "candidate_precision": 0.8,
    "recall_at_3": 0.9,
    "distractor_in_top3": 0,
}


def summarise(scenarios: list[dict[str, Any]], retrieval: dict[str, Any]) -> dict[str, Any]:
    tp = sum(s["memories"]["tp"] for s in scenarios)
    fp = sum(s["memories"]["fp"] for s in scenarios)
    fn = sum(s["memories"]["fn"] for s in scenarios)
    return {
        "scenarios": len(scenarios),
        "candidate_precision": round(tp / (tp + fp), 4) if tp + fp else 1.0,
        "candidate_recall": round(tp / (tp + fn), 4) if tp + fn else 1.0,
        "escalation_violations": sum(s["escalation_violations"] for s in scenarios),
        "unmapped_subject_leaks": sum(s["unmapped_subject_leaks"] for s in scenarios),
        "procedural_auto_activations": sum(s["procedural_auto_activations"] for s in scenarios),
        "scope_violations": sum(s["scope_violations"] for s in scenarios),
        "stage_outcomes_ok": all(s["stage_ok"] and s["error_code_ok"] for s in scenarios),
        "drop_reasons_ok": all(s["drops_ok"] for s in scenarios),
        "reviews_ok": all(s["reviews_ok"] for s in scenarios),
        "recall_at_3": retrieval["recall_at_3"],
        "distractor_in_top3": retrieval["distractor_in_top3"],
        "model_calls_per_event": round(statistics.mean(s["chat_calls"] for s in scenarios), 2),
        "tokens_per_event": round(statistics.mean(s["prompt_tokens"] + s["completion_tokens"] for s in scenarios), 1),
        "latency_seconds": {
            "p50": round(statistics.median(s["seconds"] for s in scenarios), 2),
            "p95": round(sorted(s["seconds"] for s in scenarios)[max(0, int(len(scenarios) * 0.95) - 1)], 2),
        },
    }


def aggregate(results: list[dict[str, Any]], repeat: int) -> dict[str, Any]:
    """Mean and range over the repetitions: per run, and per scenario (the same scenario is run `repeat` times in fresh workspaces)."""

    def rate(tp: int, other: int) -> float:
        return round(tp / (tp + other), 4) if tp + other else 1.0

    def spread(values: list[float]) -> dict[str, float]:
        return {"mean": round(statistics.mean(values), 4), "min": round(min(values), 4), "max": round(max(values), 4)}

    per_run = []
    for run_no in range(1, repeat + 1):
        rs = [r for r in results if r.get("run") == run_no]
        tp, fp, fn = (sum(r["memories"][k] for r in rs) for k in ("tp", "fp", "fn"))
        per_run.append({"run": run_no, "tp": tp, "fp": fp, "fn": fn, "precision": rate(tp, fp), "recall": rate(tp, fn)})
    per_scenario: dict[str, Any] = {}
    for sid in sorted({r["id"] for r in results}):
        rs = [r for r in results if r["id"] == sid]
        per_scenario[sid] = {
            "tp": [r["memories"]["tp"] for r in rs],
            "fp": [r["memories"]["fp"] for r in rs],
            "fn": [r["memories"]["fn"] for r in rs],
            "tp_mean": round(statistics.mean(r["memories"]["tp"] for r in rs), 2),
            "fp_mean": round(statistics.mean(r["memories"]["fp"] for r in rs), 2),
            "fn_mean": round(statistics.mean(r["memories"]["fn"] for r in rs), 2),
            "seconds": spread([r["seconds"] for r in rs]),
        }
    return {
        "runs": repeat,
        "per_run": per_run,
        "per_scenario": per_scenario,
        "overall": {
            "runs": repeat,
            "precision": spread([r["precision"] for r in per_run]),
            "recall": spread([r["recall"] for r in per_run]),
            "strict_counters_all_zero": all(
                r["escalation_violations"] == r["unmapped_subject_leaks"] == r["procedural_auto_activations"] == r["scope_violations"] == 0
                for r in results
            ),
        },
    }


def verdict(summary: dict[str, Any], mode: str) -> list[str]:
    problems = []
    for key in ("escalation_violations", "unmapped_subject_leaks", "procedural_auto_activations"):
        if summary[key] > THRESHOLDS[key]:
            problems.append(f"{key} = {summary[key]} (strict: must be {THRESHOLDS[key]})")
    if summary["scope_violations"]:
        problems.append(f"scope_violations = {summary['scope_violations']} (a content-chosen scope reached the store; must be 0)")
    floor = {
        "candidate_precision": 1.0 if mode == "mock" else THRESHOLDS["candidate_precision"],
        "recall_at_3": 1.0 if mode == "mock" else THRESHOLDS["recall_at_3"],
    }
    for key, need in floor.items():
        if summary[key] < need:
            problems.append(f"{key} = {summary[key]} (needs {need})")
    if summary["distractor_in_top3"] > THRESHOLDS["distractor_in_top3"]:
        problems.append(f"distractor_in_top3 = {summary['distractor_in_top3']} (must be 0)")
    if mode == "mock":
        if summary["candidate_recall"] < 1.0:
            problems.append(f"candidate_recall = {summary['candidate_recall']} (the mock must score 100%)")
        for key in ("stage_outcomes_ok", "drop_reasons_ok", "reviews_ok"):
            if not summary[key]:
                problems.append(f"{key} is false (the mock must score 100%)")
    return problems


def markdown(report: dict[str, Any]) -> str:
    s, r = report["summary"], report["retrieval"]
    lines = [
        f"# Memory evaluation ({report['mode']}, {report['date']})",
        "",
        f"Router mode: **{report['mode']}**. Scenarios: {s['scenarios']}. Verdict: **{'PASS' if not report['problems'] else 'FAIL'}**.",
        "",
        "| metric | value | threshold |",
        "|---|---|---|",
        f"| candidate precision | {s['candidate_precision']} | ≥ {THRESHOLDS['candidate_precision']} (proposed) |",
        f"| candidate recall | {s['candidate_recall']} | recorded |",
        f"| escalation violations | {s['escalation_violations']} | 0 (strict) |",
        f"| unmapped-subject leaks | {s['unmapped_subject_leaks']} | 0 (strict) |",
        f"| procedural auto-activations | {s['procedural_auto_activations']} | 0 (strict) |",
        f"| content-chosen scopes | {s['scope_violations']} | 0 (strict) |",
        f"| gold memory recall@3 | {s['recall_at_3']} | ≥ {THRESHOLDS['recall_at_3']} (proposed) |",
        f"| queries with a distractor in the top 3 | {s['distractor_in_top3']} | 0 (proposed) |",
        f"| model calls per event | {s['model_calls_per_event']} | recorded (DEC-11) |",
        f"| tokens per event | {s['tokens_per_event']} | recorded |",
        f"| latency p50 / p95 (s) | {s['latency_seconds']['p50']} / {s['latency_seconds']['p95']} | recorded |",
        "",
        "## Query cosine distribution (ADR-013)",
        "",
        "| group | n | min | mean | max |",
        "|---|---|---|---|---|",
        *[
            f"| {g} | {d['n']} | {d['min']} | {d['mean']} | {d['max']} |"
            for g, d in r["cosine"].items()
            if isinstance(d, dict) and d.get("n")
        ],
        "",
        f"Suggested `MEMORY_VEC_MIN_COSINE` (midpoint between the lowest gold and the highest non-gold cosine): **{r['cosine'].get('suggested_floor')}**",
        "",
        "## Queries",
        "",
        "| query | gold rank | distractor rank | top 3 |",
        "|---|---|---|---|",
        *[f"| {q['query']} | {q['gold_rank']} | {q['distractor_rank']} | {', '.join(q['top3'])} |" for q in r["queries"]],
        "",
        "## Scenarios",
        "",
        "| scenario | memories tp/fp/fn | stage | drops | reviews | escalations | leaks | procedural active | calls | seconds |",
        "|---|---|---|---|---|---|---|---|---|---|",
        *[
            f"| {x['id']} | {x['memories']['tp']}/{x['memories']['fp']}/{x['memories']['fn']} | {'ok' if x['stage_ok'] and x['error_code_ok'] else 'WRONG'} | {'ok' if x['drops_ok'] else 'WRONG'} | {'ok' if x['reviews_ok'] else 'WRONG'} | {x['escalation_violations']} | {x['unmapped_subject_leaks']} | {x['procedural_auto_activations']} | {x['chat_calls']} | {x['seconds']} |"
            for x in report["scenarios"]
        ],
    ]
    if report.get("repeats"):
        rp = report["repeats"]
        o = rp["overall"]
        lines += [
            "",
            f"## Repeated runs ({rp['runs']}; semantic match {'ON' if s.get('semantic_match') else 'off'})",
            "",
            f"Precision mean {o['precision']['mean']} (range {o['precision']['min']} to {o['precision']['max']}); recall mean {o['recall']['mean']} "
            f"(range {o['recall']['min']} to {o['recall']['max']}); strict counters all zero in every run: {o['strict_counters_all_zero']}.",
            "",
            "| run | tp | fp | fn | precision | recall |",
            "|---|---|---|---|---|---|",
            *[f"| {x['run']} | {x['tp']} | {x['fp']} | {x['fn']} | {x['precision']} | {x['recall']} |" for x in rp["per_run"]],
            "",
            "| scenario | tp per run | fp per run | fn per run | seconds mean (min-max) |",
            "|---|---|---|---|---|",
            *[
                f"| {sid} | {v['tp']} | {v['fp']} | {v['fn']} | {v['seconds']['mean']} ({v['seconds']['min']}-{v['seconds']['max']}) |"
                for sid, v in rp["per_scenario"].items()
            ],
        ]
    if report["problems"]:
        lines += ["", "## Problems", "", *[f"- {p}" for p in report["problems"]]]
    return "\n".join(lines) + "\n"


def _stats(values: list[float]) -> dict[str, Any]:
    return (
        {"n": len(values), "min": round(min(values), 4), "mean": round(statistics.mean(values), 4), "max": round(max(values), 4)}
        if values
        else {"n": 0}
    )


async def retrieval_phase(api: Any, make_tenant: Any, clear_graph: Any) -> dict[str, Any]:
    """One workspace: the extracted memories of the gold events plus filler, then the labelled queries."""
    from kg_db.session import tenant_session
    from kg_domain.enums import PrincipalKind, Role
    from kg_domain.principal import Principal
    from kg_memory.models import SearchIn
    from kg_memory.reads import Viewer
    from kg_memory.scopes import ScopeGrants
    from kg_memory.search import MemorySearch

    from tests.support.p4 import ctx_of, reviews, run_event, sarah_story
    from tests.support.p4 import sql as p4_sql
    from tests.support.p5 import run_embeds, save, search
    from tests.support.tenants import MEMORY_YAML

    t = await make_tenant(api, ontology=MEMORY_YAML)
    try:
        ids = await sarah_story(t)
        await t.grant("agent", "subject", ids["sarah"])
        events: dict[str, str] = {}
        gold = {g["id"]: g for g in load()}
        for gid in sorted({q[1] for q in QUERIES} | {q[2] for q in QUERIES}):
            detail = await run_event(
                api, t, gold[gid]["content"], subjects=[ids["sarah"], ids["michael"], ids["topic"]], occurred_at=gold[gid]["occurred_at"]
            )
            events[gid] = detail["id"]
        for item in await reviews(t, status="open"):
            if item["item_type"] == "memory_proposal":  # a reviewer vouches for the extracted guideline, as the demo does
                got = await t.get(f"/v1/reviews/{item['id']}", "reviewer")
                await t.req(
                    "POST",
                    f"/v1/reviews/{item['id']}/decision",
                    "reviewer",
                    json={"decision": "approve", "rationale_code": "verified_with_source"},
                    headers={"if-match": got.headers["etag"]},
                )
        for filler in FILLERS:
            r = await save(
                t,
                "admin",
                {
                    "type": "semantic",
                    "content": filler,
                    "subject_ids": [ids["sarah"]],
                    "source_ref": "eval:filler",
                    "purpose": "retrieval filler",
                },
            )
            assert r.status_code == 201, r.text
        await run_embeds(api, t)
        by_event = {
            str(r.interaction_id): str(r.memory_id)
            for r in await p4_sql(
                api, t, "SELECT memory_id, interaction_id FROM memory_evidence WHERE workspace_id = :ws AND source_kind = 'interaction'"
            )
        }
        memory_of = {gid: by_event.get(eid) for gid, eid in events.items()}
        # a second searcher with NO cosine floor, only to read the distribution (the floor itself is what the platform serves with)
        wide = MemorySearch(
            api.app.state.kg.router.gateway,
            api.app.state.kg.router.config,
            api.settings.model_copy(update={"memory_vec_min_cosine": -1.0}),
            service="kg-eval",
        )
        principal = Principal(
            principal_id=uuid.UUID(await t.principal_id("agent")),
            workspace_id=t.ws,
            kind=PrincipalKind.AGENT,
            handle="agent",
            roles=frozenset({Role.AGENT}),
        )
        out_q: list[dict[str, Any]] = []
        cos: dict[str, list[float]] = {"gold": [], "distractor": [], "filler": []}
        for query, gold_id, distractor_id in QUERIES:
            res = await search(t, "agent", query, subject_ids=[ids["sarah"]], limit=10)
            order = [h["memory_id"] for h in res["hits"]]
            gold_rank = order.index(memory_of[gold_id]) + 1 if memory_of.get(gold_id) in order else None
            dis_rank = order.index(memory_of[distractor_id]) + 1 if memory_of.get(distractor_id) in order else None
            out_q.append(
                {"query": query, "gold_rank": gold_rank, "distractor_rank": dis_rank, "top3": [h["summary"][:40] for h in res["hits"][:3]]}
            )
            async with tenant_session(api.engine, ctx_of(t)) as conn:
                grants = await ScopeGrants.for_principal(conn, principal)
                wide_res = await wide.run(
                    conn, Viewer(principal, grants), SearchIn(query=query, subject_ids=[uuid.UUID(ids["sarah"])], limit=50)
                )
            for h in wide_res.hits:
                if h.components.cosine is None:
                    continue
                mid = str(h.memory_id)
                group = "gold" if mid == memory_of.get(gold_id) else "distractor" if mid == memory_of.get(distractor_id) else "filler"
                cos[group].append(h.components.cosine)
        gold_hits = sum(1 for q in out_q if q["gold_rank"] is not None and q["gold_rank"] <= 3)
        distractors = sum(1 for q in out_q if q["distractor_rank"] is not None and q["distractor_rank"] <= 3)
        others = cos["distractor"] + cos["filler"]
        floor = round((min(cos["gold"]) + max(others)) / 2, 4) if cos["gold"] and others and min(cos["gold"]) > max(others) else None
        return {
            "queries": out_q,
            "recall_at_3": round(gold_hits / len(out_q), 4),
            "distractor_in_top3": distractors,
            "cosine": {**{g: _stats(v) for g, v in cos.items()}, "suggested_floor": floor},
        }
    finally:
        await clear_graph(api, t.group_id)


async def run(mode: str, only: set[str] | None, *, semantic: bool = False, repeat: int = 1) -> int:
    from asgi_lifespan import LifespanManager
    from kg_api.main import build_app
    from kg_db import migrate
    from kg_db.engine import create_app_engine

    from tests.conftest import APP_URL, OWNER_URL, ApiStack, make_test_settings
    from tests.support.p4 import run_event, sarah_story, stage
    from tests.support.p5 import linked_names, memory_review_count, memory_snapshot
    from tests.support.tenants import MEMORY_YAML, clear_graph, make_tenant

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
    judge = Judge(settings.router_base_url, os.environ["OSO_ROUTER_API_KEY"]) if semantic and live else None
    results: list[dict[str, Any]] = []
    retrieval: dict[str, Any] = {}
    try:
        async with LifespanManager(app, startup_timeout=60, shutdown_timeout=60) as manager:
            transport = httpx.ASGITransport(app=manager.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://kg.test") as client:
                api = ApiStack(app, client, {}, engine, settings)
                for rep in range(1, repeat + 1):
                    if repeat > 1:
                        print(f"-- run {rep} of {repeat}", flush=True)
                    for gold in load():
                        if only and gold["id"] not in only:
                            continue
                        t = await make_tenant(api, ontology=MEMORY_YAML)
                        try:
                            ids = await sarah_story(t)
                            names = {
                                "Sarah Johnson": ids["sarah"],
                                "Michael": ids["michael"],
                                "Entrepreneurship": ids["topic"],
                                "Acme": ids["acme"],
                            }
                            started = time.perf_counter()
                            detail = await run_event(
                                api, t, gold["content"], subjects=[names[n] for n in gold["subjects"]], occurred_at=gold["occurred_at"]
                            )
                            seconds = time.perf_counter() - started
                            memories = await memory_snapshot(api, t)
                            scored = score(
                                gold,
                                memories,
                                stage(detail, "memory_propose"),
                                gold["content"]["content"],
                                await linked_names(api, t, detail["id"]),
                                await memory_review_count(api, t),
                                judge.conveys if judge else None,
                            )
                            scored["seconds"] = round(seconds, 2)
                            scored["run"] = rep
                            if judge:
                                scored["judge"], judge.log = judge.log, []
                            results.append(scored)
                            ok = (
                                scored["stage_ok"]
                                and scored["drops_ok"]
                                and scored["reviews_ok"]
                                and not scored["memories"]["fp"]
                                and not scored["memories"]["fn"]
                            )
                            judged = f"  (judge asked {len(scored['judge'])}x)" if judge and scored["judge"] else ""
                            print(f"  {gold['id']:<14} {'ok' if ok else 'CHECK'}  {seconds:5.1f}s{judged}", flush=True)
                        finally:
                            await clear_graph(api, t.group_id)
                if not only:
                    retrieval = await retrieval_phase(api, make_tenant, clear_graph)
    finally:
        await engine.dispose()
        if not live:
            async with httpx.AsyncClient() as c:
                await c.post(f"{MOCK}/__mock/strict", json={"enabled": False})
    if not retrieval:
        retrieval = {"queries": [], "recall_at_3": 1.0, "distractor_in_top3": 0, "cosine": {}}
    summary = summarise(results, retrieval)
    repeats = aggregate(results, repeat) if repeat > 1 else None
    if repeats:
        summary["repeats"] = repeats["overall"]
    summary["semantic_match"] = bool(judge)
    problems = verdict(summary, mode)
    report = {
        "mode": mode,
        "date": dt.date.today().isoformat(),
        "summary": summary,
        "retrieval": retrieval,
        "scenarios": results,
        **({"repeats": repeats} if repeats else {}),
        "problems": problems,
    }
    REPORTS.mkdir(parents=True, exist_ok=True)
    base = REPORTS / f"memory-{mode}-{report['date']}"
    base.with_suffix(".json").write_text(json.dumps(report, indent=2, default=str) + "\n")
    base.with_suffix(".md").write_text(markdown(report))
    print(markdown(report))
    print(f"report: {base.with_suffix('.json').relative_to(ROOT)}")
    return 1 if problems else 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", choices=["mock", "live"], default=os.environ.get("OSO_ROUTER_MODE", "mock"))
    parser.add_argument("--only", default="", help="comma list of scenario ids (skips the retrieval phase)")
    parser.add_argument(
        "--repeat",
        type=int,
        default=1,
        help="run every scenario this many times in fresh workspaces and report the mean and range (a live model does not repeat itself)",
    )
    parser.add_argument(
        "--semantic",
        choices=["auto", "on", "off"],
        default="auto",
        help="live mode only: when the exact must_contain check fails, ask a judge model whether the memory conveys the required elements in other words (auto = on in live mode)",
    )
    args = parser.parse_args()
    if args.mode == "live" and not os.environ.get("OSO_ROUTER_API_KEY"):
        print("live mode needs OSO_ROUTER_API_KEY in the environment (it is never printed or written)", file=sys.stderr)
        sys.exit(2)
    semantic = args.semantic == "on" or (args.semantic == "auto" and args.mode == "live")
    sys.exit(asyncio.run(run(args.mode, set(filter(None, args.only.split(","))) or None, semantic=semantic, repeat=max(1, args.repeat))))


if __name__ == "__main__":
    main()
