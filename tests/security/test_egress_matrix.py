"""SEC-06: the egress decision for every (purpose x classification x workspace setting) combination, observed where it matters, at the transport that is the only way to the router.
A request leaves if and only if (a) the platform policy allows its classification, (b) the workspace has not narrowed it away and (c) it carries an egress context; the classification
the router is told is never looser than the data's own, and a workspace can only ever add to the platform's deny list."""

from __future__ import annotations

import json

import httpx
import pytest
from kg_modelgw.config import CLASSIFICATION_ORDER
from kg_modelgw.egress import egress
from kg_modelgw.errors import EgressDenied

from tests.support.p4 import Spied
from tests.support.p8 import owner_exec

pytestmark = [pytest.mark.integration, pytest.mark.security]
PLATFORM_DENIED = {"local_only"}


async def send(api, purpose: str, classification: str, ws: str | None, *, chat: bool = True, ctx: bool = True, body_cls: str | None = None):  # type: ignore[no-untyped-def]
    transport = api.app.state.kg.router.transport
    base = api.settings.router_base_url.rstrip("/")
    body: dict = (  # type: ignore[type-arg]
        {"model": "oso/general", "messages": [{"role": "user", "content": "ping"}], "max_tokens": 1}
        if chat
        else {"model": "oso/embedding", "input": ["ping"]}
    )
    if body_cls:
        body["metadata"] = {"data_classification": body_cls}
    path = "/chat/completions" if chat else "/embeddings"
    async with httpx.AsyncClient(transport=transport) as c:
        if not ctx:
            return await c.post(f"{base}{path}", json=body)
        with egress(classification=classification, purpose=purpose, workspace_id=ws, correlation_id="matrix", service="kg-worker"):
            return await c.post(f"{base}{path}", json=body)


def purposes(api) -> list[str]:  # type: ignore[no-untyped-def]
    return sorted(api.app.state.kg.router.config.purposes)


async def set_workspace_denials(ws, classes: list[str]) -> None:  # type: ignore[no-untyped-def]
    await owner_exec(
        "INSERT INTO workspace_settings (workspace_id, version, settings) VALUES (:ws, (SELECT COALESCE(max(version), 0) + 1 FROM workspace_settings WHERE workspace_id = :ws), "
        "CAST(:s AS jsonb))",
        ws=ws,
        s=json.dumps({"egress": {"denied_classifications": classes}}),
    )


async def test_the_platform_matrix_every_purpose_and_classification(api, tenant):
    t = tenant
    all_purposes = purposes(api)
    assert {"memory_consolidation", "ontology_simulation"} <= set(all_purposes) and len(all_purposes) >= 7
    for purpose in all_purposes:
        for classification in CLASSIFICATION_ORDER:
            for chat in (True, False):
                with Spied(api) as spy:
                    if classification in PLATFORM_DENIED:
                        with pytest.raises(EgressDenied):
                            await send(api, purpose, classification, str(t.ws), chat=chat)
                        assert spy.requests == [], (purpose, classification, chat)  # nothing is forwarded, not even a probe
                    else:
                        r = await send(api, purpose, classification, str(t.ws), chat=chat)
                        assert len(spy.requests) == 1, (purpose, classification, chat)
                        sent = spy.requests[0]["body"]
                        if chat:
                            assert (
                                sent["metadata"]["data_classification"] == classification
                                and sent["routing"]["data_classification"] == classification
                            )
                        assert r.status_code < 500 or r.status_code == 502


async def test_a_request_without_a_context_or_to_another_host_never_leaves(api):
    with Spied(api) as spy:
        with pytest.raises(EgressDenied):
            await send(api, "x", "internal", None, ctx=False)
        transport = api.app.state.kg.router.transport
        async with httpx.AsyncClient(transport=transport) as c:
            with egress(classification="public", purpose="graph_extraction", workspace_id=None, correlation_id="x", service="kg-worker"):
                with pytest.raises(EgressDenied):
                    await c.post("https://api.openai.example/v1/chat/completions", json={"model": "x", "messages": []})
        assert spy.requests == []


async def test_a_stricter_classification_set_by_the_caller_wins_and_a_looser_one_cannot_lower_it(api, tenant):
    t = tenant
    with Spied(api) as spy:
        await send(
            api, "graph_extraction", "internal", str(t.ws), body_cls="restricted"
        )  # the body says more than the context: the router is told restricted
        await send(
            api, "graph_extraction", "restricted", str(t.ws), body_cls="public"
        )  # a body claiming public cannot lower the context's restricted
        assert [r["body"]["metadata"]["data_classification"] for r in spy.requests] == ["restricted", "restricted"]
        with pytest.raises(EgressDenied):
            await send(api, "graph_extraction", "internal", str(t.ws), body_cls="local_only")
        assert len(spy.requests) == 2


async def test_a_workspace_narrowing_adds_to_the_platform_list_applies_to_every_purpose_and_never_to_another_workspace(
    api, tenant, bare_tenant
):
    mine, other = tenant, bare_tenant
    await set_workspace_denials(mine.ws, ["confidential", "restricted"])
    api.app.state.kg.router.narrowing.invalidate()
    narrowed = {"confidential", "restricted"}
    for purpose in purposes(api):
        for classification in CLASSIFICATION_ORDER:
            denied = classification in PLATFORM_DENIED or classification in narrowed
            with Spied(api) as spy:
                if denied:
                    with pytest.raises(EgressDenied):
                        await send(api, purpose, classification, str(mine.ws))
                    assert spy.requests == [], (purpose, classification)
                else:
                    await send(api, purpose, classification, str(mine.ws))
                    assert len(spy.requests) == 1, (purpose, classification)
            # the other workspace is not narrowed by it
            if classification not in PLATFORM_DENIED:
                with Spied(api) as spy2:
                    await send(api, purpose, classification, str(other.ws))
                    assert len(spy2.requests) == 1, ("other workspace", purpose, classification)
    # a deny list that tries to unlock the platform's own is just a list: `local_only` stays denied whatever it contains
    await set_workspace_denials(mine.ws, [])
    api.app.state.kg.router.narrowing.invalidate()
    with Spied(api) as spy:
        with pytest.raises(EgressDenied):
            await send(api, "graph_extraction", "local_only", str(mine.ws))
        await send(api, "graph_extraction", "restricted", str(mine.ws))
        assert len(spy.requests) == 1


async def test_when_the_workspace_policy_cannot_be_read_nothing_leaves(api, tenant):
    t = tenant
    narrowing = api.app.state.kg.router.narrowing
    original = narrowing._engine
    narrowing.invalidate()

    class Broken:
        def connect(self, *a, **k):  # type: ignore[no-untyped-def]
            raise RuntimeError("database unavailable")

    narrowing._engine = Broken()
    try:
        with Spied(api) as spy:
            with pytest.raises(EgressDenied):
                await send(api, "graph_extraction", "internal", str(t.ws))
            assert spy.requests == []  # fail closed
    finally:
        narrowing._engine = original
        narrowing.invalidate()
