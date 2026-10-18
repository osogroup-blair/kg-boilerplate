"""Pure calibration functions for the live session (phase-9 WP1 step 3). Inputs are the numbers the live steps record; outputs are PROPOSALS (nothing here writes a setting)."""

from __future__ import annotations

import math
import statistics
from typing import Any


def token_margin(ratios: dict[str, dict[str, float]], *, headroom_pp: float = 2.0) -> dict[str, Any]:
    """`ratios`: {"alias:sample": {"observed": n, "platform_raw": n}} (TK-02). The margin is the SMALLEST at which the platform's count (raw * (1 + margin), rounded up) is at least
    the router's observed count for every sample, plus `headroom_pp` percentage points, rounded up to a whole percent. Never below 0."""
    if not ratios:
        raise ValueError("no samples")
    need = 0.0
    for v in ratios.values():
        raw, observed = float(v["platform_raw"]), float(v["observed"])
        if raw <= 0:
            continue
        # smallest m with ceil(raw * (1 + m)) >= observed  <=>  raw * (1 + m) > observed - 1  (or >= when observed is an integer boundary)
        need = max(need, (observed - 1) / raw - 1 + 1e-9 if observed > 0 else 0.0)
    margin = max(0.0, math.ceil((need + headroom_pp / 100) * 100 - 1e-9) / 100)
    covers = all(math.ceil(float(v["platform_raw"]) * (1 + margin)) >= float(v["observed"]) for v in ratios.values())
    return {
        "margin": round(margin, 2),
        "minimal_margin": round(max(0.0, need), 4),
        "headroom_pp": headroom_pp,
        "samples": len(ratios),
        "covers_all": covers,
    }


def separating_floor(related: list[float], unrelated: list[float]) -> dict[str, Any]:
    """The threshold that best separates two cosine distributions (maximises correct classification, ties broken towards the larger gap), rounded DOWN to two decimals so
    a borderline related pair is kept rather than lost."""
    if not related or not unrelated:
        raise ValueError("both distributions are needed")
    candidates = sorted(set(related) | set(unrelated))
    best: tuple[float, float, float] | None = None
    for lo, hi in zip(candidates, candidates[1:], strict=False):
        t = (lo + hi) / 2
        correct = sum(1 for r in related if r >= t) + sum(1 for u in unrelated if u < t)
        key = (correct / (len(related) + len(unrelated)), hi - lo, t)
        if best is None or key[:2] > best[:2]:
            best = key
    assert best is not None
    t = best[2]
    floor = math.floor(t * 100) / 100
    return {
        "floor": floor,
        "accuracy": round(best[0], 4),
        "separable": min(related) > max(unrelated),
        "related": {"min": min(related), "mean": round(statistics.mean(related), 4)},
        "unrelated": {"max": max(unrelated), "mean": round(statistics.mean(unrelated), 4)},
    }


def semaphore(evidence: dict[str, Any]) -> dict[str, Any]:
    """GR-13: the largest parallelism the router served without throttling, as the spike recorded it."""
    return {"semaphore_limit": int(evidence.get("recommended_semaphore_limit") or evidence.get("max_clean_parallelism") or 1)}


def latency_profile(samples: dict[str, dict[str, list[float]]]) -> dict[str, Any]:
    """{"chat": {alias: [ms,...]}, "embedding": {...}} -> the mock router's latency profile format (p50/p95 per alias)."""
    out: dict[str, Any] = {"_note": "captured by the live session (tools/live_session, step g); replaces the PROVISIONAL profile"}
    for kind, aliases in samples.items():
        out[kind] = {}
        for alias, ms in aliases.items():
            if len(ms) < 5:
                continue
            ordered = sorted(ms)
            out[kind][alias] = {
                "p50_ms": round(statistics.median(ordered)),
                "p95_ms": round(ordered[min(len(ordered) - 1, math.ceil(0.95 * len(ordered)) - 1)]),
            }
    return out
