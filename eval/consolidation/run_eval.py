"""The consolidation evaluation (P8 WP12).

    make eval-consolidation        offline and deterministic: the platform's own rules over a labelled gold set (reinforce clustering and the synthesis validator). Must score 100%.
    make eval-consolidation-live   the same synthesis groups answered by the REAL router (a user action; needs OSO_ROUTER_API_KEY; costs model calls). Writes eval/reports/.

PART 1, reinforce: labelled memories; the clusters the rules form must be exactly the groups that share a gold claim. Counters (strict, all 0): merged_distinct (a cluster mixing gold claims),
crossed_scope (a cluster mixing scope sets or classes), missed_duplicates (offline only: a paraphrase the rules failed to fold).

PART 2, synthesis: for each group a scripted GOOD answer must be accepted and every scripted BAD answer refused for the stated reason (offline). Live, the model answers the group itself and
its answer is judged by the same validator: `validity_rate` (answers the validator accepts or that correctly condense nothing), and the strict counters `merged_distinct_escaped` and
`escalations_escaped`, which count answers the validator ACCEPTED although they merge gold-distinct claims or turn a hedge into a decision (gold judged by the cited handles)."""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from kg_memory.consolidate_rules import Proposal, Statement, clusters, validate  # noqa: E402

from eval.consolidation.gold import REINFORCE, SYNTHESIS, SynthCase  # noqa: E402

REPORTS = ROOT / "eval" / "reports"
JACCARD, COSINE = 0.9, 0.95
STRICT = (
    "merged_distinct",
    "crossed_scope",
    "missed_duplicates",
    "bad_accepted",
    "good_refused",
    "merged_distinct_escaped",
    "escalations_escaped",
)


def reinforce() -> dict[str, Any]:
    out: dict[str, Any] = {"cases": {}, "merged_distinct": 0, "crossed_scope": 0, "missed_duplicates": 0}
    for name, labelled in REINFORCE.items():
        claim = {x.mem.id: x.claim for x in labelled}
        found = clusters([x.mem for x in labelled], jaccard_min=JACCARD, cosine_min=COSINE)
        got = sorted(sorted(m.id for m in c) for c in found)
        by_claim: dict[str, list[str]] = {}
        for x in labelled:
            # two memories share a gold claim only when everything that would stop a fold agrees too
            by_claim.setdefault(x.claim, []).append(x.mem.id)
        want = sorted(sorted(v) for v in by_claim.values() if len(v) > 1)
        mixed = sum(1 for c in found if len({claim[m.id] for m in c}) > 1)
        crossed = sum(1 for c in found if len({(m.scopes, m.classification) for m in c}) > 1)
        missed = sum(1 for w in want if not any(set(w) <= set(g) for g in got))
        out["merged_distinct"] += mixed
        out["crossed_scope"] += crossed
        out["missed_duplicates"] += missed
        out["cases"][name] = {"got": got, "want": want, "ok": got == want}
    out["ok"] = all(c["ok"] for c in out["cases"].values())
    return out


def to_proposal(answer: dict[str, Any]) -> Proposal | None:
    groups = answer.get("groups") or []
    if not groups:
        return None
    g = groups[0]
    return Proposal(
        sources=[h.strip().upper() for h in g["source_memory_ids"]],
        summary=g["summary"],
        statements=[Statement(s["text"], [h.strip().upper() for h in s["source_memory_ids"]]) for s in g["statements"]],
    )


def synthesis_offline() -> dict[str, Any]:
    res: dict[str, Any] = {"cases": {}, "bad_accepted": 0, "good_refused": 0, "wrong_reason": 0}
    for case in SYNTHESIS:
        handles = {f"M{i}": x.mem for i, x in enumerate(case.memories, start=1)}
        good = validate(to_proposal(case.good), handles)  # type: ignore[arg-type]
        refused_good = 0 if good.ok else 1
        bad_rows = []
        for label, answer, reason in case.bad:
            v = validate(to_proposal(answer), handles)  # type: ignore[arg-type]
            bad_rows.append(
                {"label": label, "expected": reason, "got": v.reason if not v.ok else "ACCEPTED", "ok": (not v.ok) and v.reason == reason}
            )
            res["bad_accepted"] += int(v.ok)
            res["wrong_reason"] += int((not v.ok) and v.reason != reason)
        res["good_refused"] += refused_good
        res["cases"][case.id] = {"good_ok": good.ok, "good_reason": good.reason, "bad": bad_rows}
    res["ok"] = res["bad_accepted"] == 0 and res["good_refused"] == 0 and res["wrong_reason"] == 0
    return res


def judge_live(case: SynthCase, answer: dict[str, Any], accepted: bool) -> dict[str, int]:
    """An ACCEPTED answer that cites two memories gold says differ, or that makes a decision out of hedges, escaped the validator."""
    merged = escalated = 0
    if accepted:
        for g in answer.get("groups") or []:
            for s in g["statements"]:
                cited = {h.strip().upper() for h in s["source_memory_ids"]}
                if any(pair <= cited for pair in case.distinct):
                    merged += 1
        if case.id == "considerations":
            for g in answer.get("groups") or []:
                for s in g["statements"]:
                    t = s["text"].lower()
                    if any(w in t for w in ("decided", "will ", "is starting", "has decided")) and "not decided" not in t:
                        escalated += 1
    return {"merged_distinct_escaped": merged, "escalations_escaped": escalated}


async def synthesis_live() -> dict[str, Any]:
    from kg_memory.consolidate import ConsolidationProposal
    from kg_memory.prompts import cons_v1
    from kg_modelgw.calllog import MemorySink
    from kg_modelgw.factory import build_router_stack
    from kg_modelgw.gateway import Correlation

    from tests.conftest import make_test_settings

    key = os.environ["OSO_ROUTER_API_KEY"]
    settings = make_test_settings(oso_router_mode="live", oso_router_api_key_api=key, oso_router_api_key_worker=key)
    stack = build_router_stack(settings, service="kg-api", sink=MemorySink())
    res: dict[str, Any] = {"cases": {}, "answers": 0, "valid": 0, "merged_distinct_escaped": 0, "escalations_escaped": 0, "drops": {}}
    try:
        for case in SYNTHESIS:
            handles = {f"M{i}": x.mem for i, x in enumerate(case.memories, start=1)}
            messages = cons_v1.build_messages(memories=[(h, m.content) for h, m in handles.items()], subject_names=["Sarah Johnson"])
            from kg_modelgw.egress import egress

            with egress(
                classification="internal",
                purpose="memory_consolidation",
                workspace_id="eval",
                correlation_id=f"eval-{case.id}",
                service="kg-api",
            ):
                completion = await stack.gateway.complete(
                    "memory_consolidation",
                    messages,
                    classification="internal",
                    correlation=Correlation(f"eval-{case.id}", "eval", "", "kg-api"),
                    response_schema=ConsolidationProposal,
                )
            parsed = completion.parsed
            answer = parsed.model_dump(mode="json")  # type: ignore[union-attr]
            prop = to_proposal(answer)
            accepted, reason = (True, "") if prop is None else (lambda v: (v.ok, v.reason))(validate(prop, handles))
            res["answers"] += 1
            res["valid"] += int(accepted)
            if not accepted:
                res["drops"][reason] = res["drops"].get(reason, 0) + 1
            j = judge_live(case, answer, accepted and prop is not None)
            res["merged_distinct_escaped"] += j["merged_distinct_escaped"]
            res["escalations_escaped"] += j["escalations_escaped"]
            res["cases"][case.id] = {"accepted": accepted, "reason": reason, "groups": len(answer.get("groups") or [])}
    finally:
        await stack.aclose()
    res["validity_rate"] = round(res["valid"] / res["answers"], 4) if res["answers"] else 0.0
    res["ok"] = res["merged_distinct_escaped"] == 0 and res["escalations_escaped"] == 0
    return res


def render(report: dict[str, Any]) -> str:
    lines = [f"# Consolidation evaluation ({report['mode']}, {report['date']})", ""]
    r = report["reinforce"]
    lines += [
        "## Reinforce",
        f"merged_distinct={r['merged_distinct']} crossed_scope={r['crossed_scope']} missed_duplicates={r['missed_duplicates']}",
        "",
    ]
    lines += [f"- {n}: {'ok' if c['ok'] else 'FAIL'} got={c['got']} want={c['want']}" for n, c in r["cases"].items()]
    s = report["synthesis"]
    lines += ["", "## Synthesis", json.dumps({k: v for k, v in s.items() if k != "cases"}, sort_keys=True), ""]
    lines += [f"- {n}: {json.dumps(c)}" for n, c in s["cases"].items()]
    lines += ["", f"verdict: {'PASS' if report['ok'] else 'FAIL'}"]
    return "\n".join(lines) + "\n"


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=("mock", "live"), default="mock")
    args = ap.parse_args()
    if args.mode == "live" and not os.environ.get("OSO_ROUTER_API_KEY"):
        print("live mode needs OSO_ROUTER_API_KEY", file=sys.stderr)
        return 2
    report: dict[str, Any] = {"mode": args.mode, "date": dt.date.today().isoformat(), "reinforce": reinforce()}
    report["synthesis"] = synthesis_offline() if args.mode == "mock" else await synthesis_live()
    strict = {k: (report["reinforce"].get(k, 0) if k in report["reinforce"] else report["synthesis"].get(k, 0)) for k in STRICT}
    report["strict"] = strict
    report["ok"] = report["reinforce"]["ok"] and report["synthesis"]["ok"] and not any(strict.values())
    REPORTS.mkdir(parents=True, exist_ok=True)
    stem = REPORTS / f"consolidation-{args.mode}-{report['date']}"
    stem.with_suffix(".json").write_text(json.dumps(report, indent=2, default=str))
    stem.with_suffix(".md").write_text(render(report))
    print(render(report))
    print(f"strict counters: {strict}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
