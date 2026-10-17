"""P6 WP3: `summarized` compression is opt-in, validated line by line, and always falls back to `structured` (ADR-033, plan §5.11).

The model is the strict mock: each test registers the exact answer the "model" gives, built from the structured text the engine itself produced, so the validator is exercised
against realistic good and bad summaries."""

from __future__ import annotations

import re

import httpx
import pytest

from tests.support.p4 import MOCK, Spied, add_mock_rules, strict_mock  # noqa: F401
from tests.support.p6 import ok_build, sarah_world, texts

pytestmark = pytest.mark.integration
TASK = "Where does Sarah work, and what is she considering about a business?"


def rule(answer: str) -> dict:  # type: ignore[type-arg]
    return {"when": {"alias": "oso/fast", "contains": ["CONTEXT DATA"]}, "response": answer}


async def structured(t, ids):  # type: ignore[no-untyped-def]
    return await ok_build(t, "agent", TASK, subject_ids=[ids["sarah"]], profile="research", compression="structured", max_tokens=1200)


def drop_first_matching(text: str, pattern: str) -> str:
    out, dropped = [], False
    for line in text.splitlines():
        if not dropped and line.startswith("- ") and re.search(pattern, line):
            dropped = True
            continue
        out.append(line)
    assert dropped, pattern
    return "\n".join(out)


async def summarized(t, ids, **over):  # type: ignore[no-untyped-def]
    return await ok_build(
        t,
        "agent",
        TASK,
        subject_ids=[ids["sarah"]],
        profile="research",
        compression="summarized",
        max_tokens=over.pop("max_tokens", 1200),
        **over,
    )


async def test_a_faithful_summary_is_accepted_and_still_inside_the_budget(api, tenant_ctx, strict_mock):  # noqa: F811
    t = tenant_ctx
    ids = await sarah_world(api, t)
    base = await structured(t, ids)
    good = drop_first_matching(texts(base), r"Michael")  # a shorter block that keeps every required handle, every caveat and the envelope
    await add_mock_rules([rule(good)])
    out = await summarized(t, ids)
    assert out["compression"] == "summarized" and texts(out) == good
    assert out["budgets"]["used_tokens"] <= out["budgets"]["max_tokens"] and out["versions"]["compression"].startswith("summarized")
    assert "compression_fallback" not in out["versions"]
    # the items returned are still exactly the packed ones (the summary replaces the text, never the evidence)
    assert {f["id"] for f in out["facts"]} == {f["id"] for f in base["facts"]} and out["citations"]


@pytest.mark.parametrize(
    ("name", "mutate", "reason"),
    [
        ("drops_a_required_handle", lambda s: re.sub(r"\[F:[0-9a-f]+ · E:evt_002\]", "", s), "required_handle_dropped"),
        ("invents_a_date", lambda s: s.replace("Acme", "Acme since 1999-03-04", 1), "invented_figure"),
        ("invents_a_name", lambda s: s.replace("Acme", "Acme and Zephyr Holdings", 1), "invented_name"),
        ("cites_a_handle_it_was_not_given", lambda s: s.replace("]", " · E:forged99]", 1), "unknown_handle"),
        ("rewords_a_caveat", lambda s: re.sub(r"(Unverified claim under review: )", r"\1(probably) ", s, 1), "caveat_not_verbatim"),
        ("breaks_the_envelope", lambda s: s.replace("[END CONTEXT DATA]", ""), "envelope_broken"),
        (
            "goes_over_budget",
            lambda s: s.replace(
                "[END CONTEXT DATA]", "\n".join("- filler line of the summary" for _ in range(200)) + "\n[END CONTEXT DATA]"
            ),
            "over_budget",
        ),
    ],
)
async def test_a_bad_summary_falls_back_to_structured_with_the_reason(api, tenant_ctx, strict_mock, name, mutate, reason):  # noqa: F811
    t = tenant_ctx
    ids = await sarah_world(api, t, newco=True)
    base = await structured(t, ids)
    await add_mock_rules([rule(mutate(texts(base)))])
    out = await summarized(t, ids)
    assert out["compression"] == "structured", name
    assert out["versions"]["compression_fallback"] == reason, name
    assert (
        texts(out) == texts(base) and out["budgets"]["used_tokens"] <= out["budgets"]["max_tokens"]
    )  # the answer is the deterministic one
    assert out["status"] == "complete"


async def test_an_unavailable_or_unhelpful_model_falls_back_without_failing_the_build(api, tenant_ctx, strict_mock):  # noqa: F811
    t = tenant_ctx
    ids = await sarah_world(api, t)
    out = await summarized(t, ids)  # no fixture for the summary: the mock answers something generic, which is not the envelope
    assert (
        out["compression"] == "structured" and out["versions"]["compression_fallback"] == "envelope_broken" and out["status"] == "complete"
    )
    async with httpx.AsyncClient() as c:  # and a router that is down
        await c.post(f"{MOCK}/__mock/faults", json={"faults": [{"endpoint": "chat", "code": "provider_unavailable", "count": 50}]})
    try:
        down = await summarized(t, ids)
    finally:
        async with httpx.AsyncClient() as c:
            await c.delete(f"{MOCK}/__mock/requests")
    assert (
        down["compression"] == "structured"
        and down["versions"]["compression_fallback"] == "model_unavailable"
        and down["status"] == "complete"
    )
    assert texts(down) == texts(out)


async def test_local_only_content_makes_no_model_call_at_all(api, tenant_ctx, strict_mock):  # noqa: F811
    t = tenant_ctx
    ids = await sarah_world(api, t)
    with Spied(api) as spy:
        out = await summarized(t, ids, sensitivity="local_only")
    assert spy.requests == []  # no embedding, no graph search, no summary: nothing left the platform (SEC-06)
    assert out["compression"] == "structured" and out["status"] == "partial"
    assert {u["reason"] for u in out["unavailable_sources"]} == {"egress_policy"} and {u["source"] for u in out["unavailable_sources"]} == {
        "graph",
        "memory_vector",
    }
    assert "Engineering Manager" in texts(out)  # Postgres still answers, lexically for memory


async def test_summarized_is_refused_where_the_profile_does_not_allow_it(api, tenant_ctx, strict_mock):  # noqa: F811
    t = tenant_ctx
    ids = await sarah_world(api, t)
    for profile in ("planning", "quick_answer", "review", "personal_assistant"):
        r = await t.req(
            "POST",
            "/v1/context/build",
            "agent",
            json={"task": TASK, "subject_ids": [ids["sarah"]], "profile": profile, "compression": "summarized"},
            key="",
        )
        assert r.status_code == 422 and r.json()["error"]["details"]["fields"] == ["compression"], profile
    _ = httpx, MOCK
