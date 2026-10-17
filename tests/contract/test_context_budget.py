"""P6 WP8: CTX-01 / AT-08 / RET-01 as a contract: for every benchmark case at ten budgets, the package never costs more than the ceiling, measured by the platform's own tokenizer on
the bytes it returned (a re-count, not the number the response claims), whatever the status. A budget above the profile's cap is a 422, never a silent clamp."""

from __future__ import annotations

import pytest
from kg_common.tokens import TokenCounter

from eval.context.run_eval import Harness, apply_setup, build_acl_world, load_cases, plant_injections
from tests.support.p4 import strict_mock  # noqa: F401
from tests.support.p6 import sarah_world
from tests.support.tenants import make_tenant

pytestmark = pytest.mark.contract
BUDGETS = (64, 80, 120, 160, 200, 250, 300, 600, 900, 1200)
CAPS = {"quick_answer": 300, "planning": 1200, "research": 2400, "review": 1600, "personal_assistant": 800}


async def test_at08_no_case_at_no_budget_exceeds_its_ceiling(api, tenant_ctx, strict_mock):  # noqa: F811
    t = tenant_ctx
    other = await make_tenant(api, ontology=None)
    world = await sarah_world(api, t)
    world["ghost"] = (await t.entity("Person", "Priya Raman")).json()["entity_id"]
    world["acl"], world["_acl_tenant"] = await build_acl_world(api)  # the P7 cases run in their own v1.4.0 tenant
    h = Harness(api, t, other, world, "mock")
    counter = TokenCounter(margin=api.settings.context_token_margin)
    checked = over_cap = 0
    for case in load_cases():
        if case["id"].startswith("C14"):
            continue  # the summarised case needs a model answer registered per budget; its ceiling is the validator's (tests/integration/test_p6_compression.py)
        undo = [apply_setup(h, case["setup"])] if case.get("setup") else []
        undo += [await plant_injections(h)] if case.get("plant") == "injection" else []
        try:
            for budget in BUDGETS:
                out, _, _ = await h.build({k: v for k, v in case.items() if k != "budgets"}, max_tokens=budget)
                if budget > CAPS[case["profile"]]:
                    over_cap += 1
                    assert out["status"] == "http_422" and out["error"]["code"] == "exceeds_profile", (case["id"], budget)
                    continue
                checked += 1
                if out["status"] == "denied":
                    assert out["context_text"] == ""
                    continue
                text = out["context_text"]
                assert out["budgets"]["used_tokens"] <= budget, (case["id"], budget)
                assert counter.count(text) <= budget, (case["id"], budget, counter.count(text))
                assert out["status"] in ("complete", "partial", "insufficient_evidence", "insufficient_budget"), (
                    case["id"],
                    budget,
                    out["status"],
                )
                if out["status"] == "insufficient_budget":
                    assert out["facts"] == [] and out["budgets"]["required_tokens"] > budget
        finally:
            for fn in undo:
                fn()
            if getattr(h, "pending_expiry", None):
                from tests.support.p5 import act

                for mid in h.pending_expiry:
                    await act(t, "admin", mid, "expire", {"reason_code": "drill_over"})
                h.pending_expiry = []
    assert checked >= 13 * 3 and over_cap >= 1
