"""TK-02 (LIVE-DEFERRED): calibrate the platform token count against the router's reported prompt_tokens."""
from __future__ import annotations

import math
import os
import uuid

import httpx
import pytest

import results_plugin as rp
from tokenizer.counter import TokenCounter
from tokenizer.samples import SAMPLES

pytestmark = [pytest.mark.live, pytest.mark.skipif(os.environ.get("OSO_ROUTER_MODE") != "live", reason="OSO_ROUTER_MODE != live")]
ALIASES = ("oso/fast", "oso/general", "oso/code", "oso/reasoning")


async def prompt_tokens(c: httpx.AsyncClient, alias: str, text: str) -> int:
    for attempt in (1, 2):   # the router's latency tail is long (oso/general answered a 16-token reply in 36-52 s on 2026-10-19): one retry, not a bigger failure
        try:
            r = await c.post(os.environ["OSO_ROUTER_BASE_URL"].rstrip("/") + "/chat/completions",
                             headers={"Authorization": f"Bearer {os.environ['OSO_ROUTER_API_KEY']}", "x-request-id": str(uuid.uuid4())},
                             json={"model": alias, "max_tokens": 16, "temperature": 0, "messages": [{"role": "user", "content": text}]})
            r.raise_for_status()
            return r.json()["usage"]["prompt_tokens"]
        except httpx.ReadTimeout:
            if attempt == 2:
                raise
    raise AssertionError("unreachable")


async def test_tk02_calibrate_against_router_prompt_tokens():
    base = TokenCounter(margin=0.0)
    table, needed = {}, 0.0
    async with httpx.AsyncClient(timeout=240) as c:
        for alias in ALIASES:
            overhead = await prompt_tokens(c, alias, ".") - base.raw(".")            # chat-template overhead per request
            for name, text in SAMPLES.items():
                observed = await prompt_tokens(c, alias, text) - overhead
                raw = base.raw(text)
                ratio = observed / raw
                needed = max(needed, ratio - 1)
                table[f"{alias}:{name}"] = {"observed": observed, "platform_raw": raw, "ratio": round(ratio, 3)}
    recommended = math.ceil((needed + 0.05) * 20) / 20                              # +5% headroom, rounded up to a 5% step
    recommended = max(recommended, 0.10)
    covered = all(TokenCounter(margin=recommended).count(SAMPLES[k.split(":", 1)[1]]) >= v["observed"] for k, v in table.items())
    rp.evidence("TK-02", ratios=table, worst_case_overshoot=round(needed, 3), recommended_margin=recommended, covers_all_samples=covered)
    rp.note("TK-02", f"Worst observed router/platform token ratio exceeds 1 by {needed:.1%}; recommended margin {recommended:.0%}. Update TokenCounter.margin and "
                     "ADR-020 with this number.")
    assert covered
