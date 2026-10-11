"""TK-01 (OFFLINE): choose tokenizer + margin by comparing candidates on representative packages. TK-02 (calibration vs the router) is live."""
from __future__ import annotations

import math

import pytest

import results_plugin as rp
from tokenizer.counter import TokenCounter, heuristic_chars
from tokenizer.samples import SAMPLES


def test_tk01_compare_tokenizers_and_choose_default():
    o200k, cl100k = TokenCounter(("o200k_base",), 0.0), TokenCounter(("cl100k_base",), 0.0)
    both = TokenCounter(margin=0.0)
    rows = {}
    for name, text in SAMPLES.items():
        o, c, h = o200k.raw(text), cl100k.raw(text), heuristic_chars(text)
        rows[name] = {"chars": len(text), "o200k_base": o, "cl100k_base": c, "chars/4": h, "o200k_vs_cl100k": round(o / c, 3), "chars4_vs_o200k": round(h / o, 3)}
    # determinism + margin arithmetic
    t = SAMPLES["planning_package"]
    assert o200k.raw(t) == o200k.raw(t)
    mc = TokenCounter()                                                        # the platform default
    assert mc.count(t) == math.ceil(max(o200k.raw(t), cl100k.raw(t)) * 1.10) and mc.count(t) > o200k.raw(t)
    assert mc.tokenizer_id == "tiktoken:max(o200k_base,cl100k_base)+margin0.1"
    assert all(both.raw(x) == max(o200k.raw(x), cl100k.raw(x)) for x in SAMPLES.values())
    assert both.raw(SAMPLES["multilingual"]) > o200k.raw(SAMPLES["multilingual"]) * 1.4   # why the default takes the max
    # the char heuristic is NOT conservative on structured/multilingual text: it under-counts relative to a real tokenizer on some samples
    unsafe = [n for n, r in rows.items() if r["chars/4"] < r["o200k_base"]]
    spread = {n: r["o200k_vs_cl100k"] for n, r in rows.items()}
    worst_gap = max(r["o200k_base"] / min(r["cl100k_base"], r["o200k_base"]) for r in rows.values())
    assert unsafe, "expected the chars/4 heuristic to under-count at least one structured sample"
    assert all(r["o200k_base"] > 0 for r in rows.values())
    # empty string and special-token text must not raise
    assert o200k.raw("") == 0 and o200k.raw("<|endoftext|> literal") > 0
    rp.evidence("TK-01", samples=rows, heuristic_undercounts=unsafe, o200k_over_cl100k_ratio=spread, default="max(o200k_base, cl100k_base) * 1.10", margin=0.10)
    rp.note("TK-01", "DEFAULT: count = ceil(max(o200k_base, cl100k_base) * 1.10), tokenizer id 'tiktoken:max(o200k_base,cl100k_base)+margin0.1' recorded in "
                     "every context trace. The two vocabularies agree to within 1% on English/JSON/UUID text but cl100k_base counts ~47% MORE on "
                     "multilingual text, and the router's physical model is unknown, so the larger of the two is used. A chars/4 heuristic under-counts "
                     f"{', '.join(unsafe)} and must not be used for hard budgets. The 10% margin is PROVISIONAL: TK-02 (live) measures router prompt_tokens "
                     "for the same samples on each alias and raises the margin until the platform count >= the observed count for 100% of samples "
                     "(after subtracting the measured per-request template overhead).")
