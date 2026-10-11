"""G-05 (LIVE-DEFERRED): the same contract against the real router. Skipped unless OSO_ROUTER_MODE=live.

Run through `make verify-router`, which checks reachability first and records outcomes in spikes/results/live-*.json.
"""
from __future__ import annotations

import os
import uuid

import pytest
from pydantic import BaseModel

from gateway.errors import EgressDenied, RouterError
from gateway.gateway import Correlation, ModelGateway

pytestmark = [pytest.mark.live, pytest.mark.skipif(os.environ.get("OSO_ROUTER_MODE") != "live", reason="OSO_ROUTER_MODE != live")]


class Fact(BaseModel):
    person: str
    employer: str


def gw() -> ModelGateway:
    return ModelGateway(os.environ["OSO_ROUTER_BASE_URL"], os.environ["OSO_ROUTER_API_KEY"], max_attempts=2)


def corr() -> Correlation:
    return Correlation(str(uuid.uuid4()), "ws_live_check", "verify_kit", "kg-worker")


async def test_g05_chat_usage_and_request_id_round_trip():
    g = gw()
    r = await g.complete("context_compression", [{"role": "user", "content": "Reply with the single word: ok"}], classification="public", correlation=corr())
    assert r.prompt_tokens > 0 and r.router_request_id


async def test_g05_structured_output_on_general():
    g = gw()
    r = await g.complete("graph_extraction", [{"role": "user", "content": "Sarah Chen works at Acme. Extract person and employer."}],
                         classification="public", correlation=corr(), response_schema=Fact)
    assert r.parsed is not None and r.parsed.person


async def test_g05_embeddings_are_384():
    g = gw()
    r = await g.embed("embedding", ["Sarah works at Acme"], kind="passage", classification="public", correlation=corr())
    assert len(r.vectors[0]) == 384


async def test_g05_local_only_never_sent():
    g = gw()
    with pytest.raises(EgressDenied):
        await g.complete("context_compression", [{"role": "user", "content": "x"}], classification="local_only", correlation=corr())


async def test_g05_oso_fast_rejects_json_schema():
    g = gw()
    g.purposes["graph_extraction_small"]["alias"] = "oso/fast"
    with pytest.raises(RouterError) as e:
        await g.complete("graph_extraction_small", [{"role": "user", "content": "x"}], classification="public", correlation=corr(), response_schema=Fact)
    assert e.value.code == "unsupported_capability"
