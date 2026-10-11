"""G-01..G-04 ported from the Phase 0 spike onto the production ModelGateway + RouterTransport. G-05 stays in `make verify-router`."""

from __future__ import annotations

import pytest
from kg_common.tokens import count_tokens
from kg_modelgw.errors import Category, EgressDenied, EmbeddingDimensionError, RouterError, StructuredOutputError
from pydantic import BaseModel

from tests.support.router_env import Env

pytestmark = pytest.mark.contract

MSGS = [{"role": "system", "content": "Extract."}, {"role": "user", "content": "Sarah works at Acme."}]


class Extraction(BaseModel):
    name: str
    n: int
    tags: list[str]


# ---- G-01 round trips ----------------------------------------------------------------------------------------------------
async def test_g01_chat_round_trip_records_usage_and_metadata_only_log(env, corr):
    r = await env.gw.complete("context_compression", MSGS, classification="internal", correlation=corr)
    assert r.text.startswith("echo:") and r.alias == "oso/fast" and r.prompt_tokens > 0 and r.router_request_id is not None
    rec = env.log[-1]
    assert rec.outcome == "ok" and rec.est_cost_usd is not None and rec.tokenizer == "tiktoken:o200k_base"
    assert "Sarah" not in repr(rec)  # metadata only, never content


async def test_g01_json_schema_round_trip_is_typed(env, corr):
    r = await env.gw.complete("graph_extraction", MSGS, classification="confidential", correlation=corr, response_schema=Extraction)
    assert r.parsed == Extraction(name="sample", n=0, tags=[]) and r.alias == "oso/general"


async def test_g01_embeddings_round_trip(env, corr):
    r = await env.gw.embed("embedding", ["sarah acme", "michael topic"], kind="passage", classification="internal", correlation=corr)
    assert len(r.vectors) == 2 and all(len(v) == 384 for v in r.vectors) and r.prompt_tokens > 0 and r.chunked == [False, False]


async def test_g01_request_carries_correlation_and_hashed_identity(env, corr):
    await env.gw.complete("memory_extraction", MSGS, classification="internal", correlation=corr, response_schema=Extraction)
    sent = env.spy.requests[-1]
    assert sent["headers"]["x-request-id"] == corr.correlation_id
    meta = sent["body"]["metadata"]
    assert meta["application_id"] == "kg-platform" and meta["agent"] == "kg-worker" and meta["task_type"] == "extraction"
    assert corr.principal_id not in str(sent["body"]) and corr.workspace_id not in str(sent["body"])  # hashed
    assert sent["headers"]["authorization"] == "Bearer test-key" and meta["user_id"] == sent["body"]["user"]
    assert sent["body"]["routing"]["data_classification"] == "internal"
    log = env.app.state.request_log[-1]
    assert log["stripped_fields"] == []  # the gateway sends only fields the router keeps


# ---- G-02 egress ----------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("classification", ["local_only", "top_secret"])
async def test_g02_denied_classifications_never_reach_the_transport(env, corr, classification):
    with pytest.raises(EgressDenied) as e:
        await env.gw.complete("graph_extraction", MSGS, classification=classification, correlation=corr, response_schema=Extraction)
    assert e.value.stage_state == "skipped_by_policy" and env.spy.requests == []
    with pytest.raises(EgressDenied):
        await env.gw.embed("embedding", ["x"], kind="query", classification=classification, correlation=corr)
    assert env.spy.requests == [] and env.log[-1].outcome == "egress_denied"


async def test_g02_router_side_policy_error_is_policy_terminal_and_not_retried(env, corr):
    env.faults({"code": "data_policy_violation", "count": 1})
    with pytest.raises(RouterError) as e:
        await env.gw.complete("context_compression", MSGS, classification="internal", correlation=corr)
    assert e.value.category is Category.POLICY_TERMINAL and e.value.stage_state == "skipped_by_policy" and len(env.spy.requests) == 1


# ---- G-03 retry and error categories -------------------------------------------------------------------------------------
async def test_g03_429_honours_retry_after_then_succeeds(env, corr):
    env.faults({"code": "rate_limited", "count": 2})
    r = await env.gw.complete("context_compression", MSGS, classification="internal", correlation=corr)
    assert r.attempts == 3 and env.sleeps == [3, 3] and len(env.spy.requests) == 3  # Retry-After wins over exponential backoff


@pytest.mark.parametrize("code", ["unknown_provider_error", "provider_unavailable", "timeout", "routing_failed"])
async def test_g03_retryable_codes_back_off_exponentially_and_are_bounded(env, corr, code):
    env.faults({"code": code, "count": 99})
    with pytest.raises(RouterError) as e:
        await env.gw.complete("context_compression", MSGS, classification="internal", correlation=corr)
    assert e.value.code == code and e.value.category is Category.RETRYABLE
    assert len(env.spy.requests) == env.gw.max_attempts == 4 and env.sleeps == [0.5, 1.0, 2.0]


@pytest.mark.parametrize(
    "code,category",
    [
        ("unsupported_capability", Category.CONFIG_TERMINAL),
        ("model_not_found", Category.CONFIG_TERMINAL),
        ("authentication_error", Category.CONFIG_TERMINAL),
        ("budget_exceeded", Category.BUDGET),
        ("classification_not_permitted", Category.POLICY_TERMINAL),
    ],
)
async def test_g03_non_retryable_codes_are_attempted_once(env, corr, code, category):
    env.faults({"code": code, "count": 99})
    with pytest.raises(RouterError) as e:
        await env.gw.complete("context_compression", MSGS, classification="internal", correlation=corr)
    assert e.value.category is category and len(env.spy.requests) == 1 and env.sleeps == []


async def test_g03_real_unsupported_capability_from_wrong_alias_is_not_retried(env, corr):
    env.gw.config.purposes["graph_extraction_small"].alias = "oso/fast"  # the misconfiguration the contract warns about
    with pytest.raises(RouterError) as e:
        await env.gw.complete("graph_extraction_small", MSGS, classification="internal", correlation=corr, response_schema=Extraction)
    assert e.value.code == "unsupported_capability" and len(env.spy.requests) == 1


async def test_g03_exhausted_primary_falls_back_to_fallback_alias(env, corr):
    # first alias fails 4 times (all attempts), the fallback alias then succeeds
    env.faults({"code": "provider_unavailable", "count": 4})
    r = await env.gw.complete("graph_extraction", MSGS, classification="internal", correlation=corr, response_schema=Extraction)
    assert (
        r.fallback_used and r.alias == "oso/code" and [x["body"]["model"] for x in env.spy.requests] == ["oso/general"] * 4 + ["oso/code"]
    )
    assert env.log[-1].fallback_used and env.log[0].outcome == "provider_unavailable"


async def test_g03_budget_exhaustion_does_not_trigger_fallback(env, corr):
    env.faults({"code": "budget_exceeded", "count": 1})
    with pytest.raises(RouterError):
        await env.gw.complete("graph_extraction", MSGS, classification="internal", correlation=corr, response_schema=Extraction)
    assert len(env.spy.requests) == 1


async def test_g03_invalid_structured_output_is_its_own_error(env, corr, tmp_path):
    import json

    (tmp_path / "f.json").write_text(json.dumps([{"when": {"contains": ["Sarah"]}, "response": {"wrong": "shape"}}]))
    bad = Env(fixtures_dir=tmp_path)
    with pytest.raises(StructuredOutputError):
        await bad.gw.complete("graph_extraction", MSGS, classification="internal", correlation=corr, response_schema=Extraction)
    assert bad.log[-1].outcome == "invalid_structured_output"


async def test_g03_client_side_timeout_is_retryable(corr):
    import httpx
    from kg_modelgw.gateway import ModelGateway

    class Boom(httpx.AsyncBaseTransport):
        calls = 0

        async def handle_async_request(self, request):
            Boom.calls += 1
            raise httpx.ReadTimeout("slow", request=request)

    from kg_common.settings import Settings
    from kg_modelgw.calllog import MemorySink
    from kg_modelgw.factory import build_router_stack

    slept = []

    async def sl(s):
        slept.append(s)

    sink = MemorySink()
    stack = build_router_stack(Settings(_env_file=None, oso_router_mock_url="http://mock/v1"), service="kg-worker", sink=sink, inner=Boom())
    gw = ModelGateway(stack.http, stack.config, sink, sleep=sl, jitter=lambda: 0.0, max_attempts=3)
    with pytest.raises(RouterError) as e:
        await gw.complete("context_compression", MSGS, classification="internal", correlation=corr)
    assert e.value.code == "timeout" and Boom.calls == 3 and [r.outcome for r in sink.records] == ["transport_error"] * 3


# ---- G-04 embeddings -----------------------------------------------------------------------------------------------------
async def test_g04_prefixes_are_added_per_kind(env, corr):
    await env.gw.embed("embedding", ["alpha"], kind="query", classification="internal", correlation=corr)
    await env.gw.embed("embedding", ["alpha"], kind="passage", classification="internal", correlation=corr)
    assert env.spy.requests[0]["body"]["input"] == ["query: alpha"] and env.spy.requests[1]["body"]["input"] == ["passage: alpha"]


async def test_g04_long_input_is_chunked_within_512_tokens_and_pooled(env, corr):
    text = " ".join(f"token{i}" for i in range(1500))
    r = await env.gw.embed("embedding", [text, "short"], kind="passage", classification="internal", correlation=corr)
    sent = [i for req in env.spy.requests for i in req["body"]["input"]]
    assert r.chunked == [True, False] and len(r.vectors) == 2 and len(sent) > 2
    assert max(count_tokens(s) for s in sent) <= 512
    assert abs(sum(x * x for x in r.vectors[0]) - 1.0) < 1e-6  # pooled vector re-normalised
    assert not any("x-mock-truncated" in str(x) for x in env.spy.requests)  # the router never had to truncate


async def test_g04_batches_inputs(env, corr):
    env.gw.embed_batch_size = 4
    r = await env.gw.embed("embedding", [f"text {i}" for i in range(10)], kind="passage", classification="internal", correlation=corr)
    assert [len(x["body"]["input"]) for x in env.spy.requests] == [4, 4, 2] and len(r.vectors) == 10


async def test_g04_batched_order_is_preserved(env, corr):
    env.gw.embed_batch_size = 2
    texts = ["sarah acme manager", "cycling race weekend", "runway finance plan"]
    solo = [(await env.gw.embed("embedding", [t], kind="passage", classification="internal", correlation=corr)).vectors[0] for t in texts]
    batched = (await env.gw.embed("embedding", texts, kind="passage", classification="internal", correlation=corr)).vectors
    assert solo == batched


async def test_g04_wrong_dimension_is_rejected(corr):
    bad = Env(embedding_dim=128)
    with pytest.raises(EmbeddingDimensionError):
        await bad.gw.embed("embedding", ["x"], kind="query", classification="internal", correlation=corr)
