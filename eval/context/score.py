"""Scoring one benchmark case (phase-6 plan WP6; blueprint 05-delivery/02 "Retrieval evaluation methodology").

Pure: takes the benchmark case (with its logical keys already resolved to ids), the context package the engine returned and the trace of that build, and returns the numbers.

    evidence_recall        share of the expected ids that are in the package
    irrelevant_inclusion   share of the package's facts and memories that are neither expected nor allowed
    temporal_correct       every required phrase present and every forbidden phrase absent (the current/superseded distinction)
    attribution            share of packed facts and memories with at least one citation that resolves to a real source (the caller verifies authorisation)
    leakage                ids or phrases the caller may NOT see, found ANYWHERE it could look: the package, its citations, the trace JSON (must be 0)
    wrongly_included       ids the caller MAY see but that do not belong in this package (a relevance error, counted with irrelevant inclusion; the trace may list them as excluded)
    budget_ok              the recounted text is inside the ceiling (must be 100%)
    status_ok              the status is one the case allows (denied and insufficient_evidence behave as specified)
"""

from __future__ import annotations

import json
from typing import Any


def package_ids(out: dict[str, Any]) -> set[str]:
    return {i["id"] for k in ("facts", "memories", "instructions", "conflicts") for i in out.get(k, []) if i.get("id")}


def evidence_ids(out: dict[str, Any]) -> list[str]:
    """Ids of the packed facts and memories (what recall and precision are about; conflicts and unknowns are caveats)."""
    return [i["id"] for k in ("facts", "memories", "instructions") for i in out.get(k, []) if i.get("id")]


def score_case(
    case: dict[str, Any], resolved: dict[str, Any], out: dict[str, Any], trace: dict[str, Any] | None, *, counter: Any, attribution: float
) -> dict[str, Any]:
    expect, forbid = case.get("expect", {}), case.get("forbid", {})
    text = out.get("context_text", "") or ""
    everything = json.dumps(out) + json.dumps(trace or {})
    returned = evidence_ids(out)
    wanted = set(resolved["expect_ids"])
    relevant = wanted | set(resolved["allow_ids"])
    leak = case.get("leak", {})
    leaked = [i for i in resolved["leak_ids"] if i in everything] + [t for t in leak.get("text", []) if t in everything]
    in_package = json.dumps(out)
    wrongly_included = [i for i in resolved["forbid_ids"] if i in in_package]
    missing_text = [
        t
        for t in expect.get("text", [])
        if t not in text and t not in json.dumps(out.get("unknowns", [])) and t not in json.dumps(out.get("conflicts", []))
    ]
    present_text = [
        t for t in forbid.get("text", []) if t in text
    ]  # a phrase that must not be in the package: it is in the package, not merely in the trace
    max_tokens = (out.get("budgets") or {}).get("max_tokens")
    used = (out.get("budgets") or {}).get("used_tokens", 0)
    recounted = counter.count(text) if text else 0
    budget_ok = max_tokens is None or (used <= max_tokens and recounted <= max_tokens)
    status_ok = out.get("status") in expect.get("status", [out.get("status")])
    unavailable = {u["source"] for u in out.get("unavailable_sources", [])}
    return {
        "id": case["id"],
        "title": case.get("title", ""),
        "scored": bool(case.get("scored")),
        "status": out.get("status"),
        "status_ok": status_ok,
        "recall": round(len(wanted & set(returned)) / len(wanted), 4) if wanted else 1.0,
        "missing_ids": sorted(wanted - set(returned)),
        "irrelevant_inclusion": round(len([i for i in returned if i not in relevant]) / len(returned), 4) if returned else 0.0,
        "irrelevant_ids": sorted(i for i in returned if i not in relevant),
        "irrelevant_text": [
            i["text"][:90] for k in ("facts", "memories", "instructions") for i in out.get(k, []) if i.get("id") and i["id"] not in relevant
        ],
        "temporal_correct": not missing_text and not present_text,
        "missing_text": missing_text,
        "forbidden_text_present": present_text,
        "unavailable_ok": set(expect.get("unavailable", [])) <= unavailable,
        "attribution": attribution,
        "leakage": len(leaked),
        "leaked": leaked,
        "wrongly_included": wrongly_included,
        "budget_ok": budget_ok,
        "used_tokens": used,
        "max_tokens": max_tokens,
        "items": len(returned),
        "freshness_lag": (out.get("freshness") or {}).get("graph_lag_seconds"),
    }
