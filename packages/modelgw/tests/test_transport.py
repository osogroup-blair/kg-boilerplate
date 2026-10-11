"""T-MG-01..03, T-MG-05 and extra transport behaviour. (T-MG-04, the DB content check, is in tests/integration.)"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import httpx
import pytest
from kg_modelgw.calllog import MemorySink
from kg_modelgw.config import ConfigError, load_config
from kg_modelgw.egress import egress
from kg_modelgw.transport import RouterTransport, stricter
from kg_obs.logging import configure_logging
from openai import AsyncOpenAI

from tests.support.router_env import KEY, Env

pytestmark = pytest.mark.contract
ROOT = Path(__file__).resolve().parents[3]


def raw_openai(env: Env) -> AsyncOpenAI:
    """What Graphiti does: its own OpenAI SDK client, with the platform transport underneath."""
    http = httpx.AsyncClient(transport=env.stack.transport, base_url="http://mock/v1")
    return AsyncOpenAI(api_key="whatever-graphiti-holds", base_url="http://mock/v1", http_client=http, max_retries=0)


async def test_mg01_raw_sdk_call_without_egress_context_is_refused_and_sends_nothing(env):
    client = raw_openai(env)
    with pytest.raises(Exception):
        await client.chat.completions.create(model="oso/general", messages=[{"role": "user", "content": "hi"}])
    assert env.spy.requests == []  # nothing reached the network layer
    assert env.log[-1].outcome == "egress_denied" and env.log[-1].purpose == "unclassified"


async def test_mg01_local_only_never_sent_even_with_context(env):
    client = raw_openai(env)
    with egress(classification="local_only", purpose="graph_extraction"), pytest.raises(Exception):
        await client.chat.completions.create(model="oso/general", messages=[{"role": "user", "content": "hi"}])
    assert env.spy.requests == []


async def test_mg01_unknown_host_is_refused(env):
    http = httpx.AsyncClient(transport=env.stack.transport)
    with egress(classification="internal", purpose="x"), pytest.raises(Exception):
        await http.post("http://evil.example/v1/chat/completions", json={"model": "x"})
    assert env.spy.requests == []


async def test_mg02_metadata_key_and_hints_are_injected_on_raw_sdk_calls(env):
    client = raw_openai(env)
    ws, principal = str(uuid.uuid4()), str(uuid.uuid4())
    with egress(classification="confidential", purpose="graph_extraction", workspace_id=ws, principal_id=principal, service="kg-worker"):
        await client.chat.completions.create(model="oso/general", messages=[{"role": "user", "content": "Sarah works at Acme"}])
    sent = env.spy.requests[-1]
    assert sent["headers"]["authorization"] == f"Bearer {KEY}"  # the SDK's own key was replaced by the per-service key
    assert sent["body"]["routing"]["data_classification"] == "confidential"
    meta = sent["body"]["metadata"]
    assert meta["application_id"] == "kg-platform" and meta["data_classification"] == "confidential" and meta["agent"] == "kg-worker"
    assert ws not in json.dumps(sent["body"]) and principal not in json.dumps(sent["body"])
    assert uuid.UUID(sent["headers"]["x-request-id"])
    assert env.log[-1].purpose == "graph_extraction" and env.log[-1].outcome == "ok" and env.log[-1].workspace_id == ws


async def test_mg02_a_callers_stricter_classification_is_never_weakened(env):
    client = raw_openai(env)
    with egress(classification="internal", purpose="x"):
        await client.chat.completions.create(
            model="oso/general", messages=[{"role": "user", "content": "x"}], extra_body={"metadata": {"data_classification": "restricted"}}
        )
    assert env.spy.requests[-1]["body"]["routing"]["data_classification"] == "restricted"
    assert env.log[-1].classification == "restricted"
    assert (
        stricter("internal", "restricted") == "restricted"
        and stricter(None, "public") == "public"
        and stricter("restricted", "internal") == "restricted"
    )


async def test_mg02_stricter_caller_class_that_policy_denies_is_refused(env):
    client = raw_openai(env)
    with egress(classification="internal", purpose="x"), pytest.raises(Exception):
        await client.chat.completions.create(
            model="oso/general", messages=[{"role": "user", "content": "x"}], extra_body={"metadata": {"data_classification": "local_only"}}
        )
    assert env.spy.requests == []


class _AdminResponder(httpx.AsyncBaseTransport):
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        body = {
            "id": "x",
            "model": "oso/fast",
            "choices": [{"message": {"role": "assistant", "content": "ok"}, "index": 0, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
        }
        return httpx.Response(200, json=body, headers={"x-gateway-provider": "acme", "x-gateway-model": "m"})


@pytest.mark.parametrize("production,level", [(False, "warning"), (True, "error")])
async def test_mg03_admin_key_response_headers_are_flagged(capsys, production, level):
    configure_logging("INFO", service="t")
    cfg = load_config(ROOT / "compat/model-purposes.yaml", ROOT / "compat/pricing.yaml")
    t = RouterTransport(
        base_url="http://mock/v1",
        api_key="k",
        service="kg-api",
        config=cfg,
        sink=MemorySink(),
        hmac_secret="s",
        inner=_AdminResponder(),
        production=production,
    )
    async with httpx.AsyncClient(transport=t, base_url="http://mock/v1/") as c:
        with egress(classification="internal", purpose="x"):
            r = await c.post("chat/completions", json={"model": "oso/fast", "messages": [{"role": "user", "content": "x"}]})
    assert r.status_code == 200 and t.admin_key_detected
    line = next(json.loads(x) for x in capsys.readouterr().out.splitlines() if "router_key_is_admin" in x)
    assert line["level"] == level


async def test_usage_is_estimated_when_the_router_omits_it(env):
    class NoUsage(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            return httpx.Response(
                200, json={"model": "oso/fast", "choices": [{"message": {"role": "assistant", "content": "hello there"}, "index": 0}]}
            )

    sink = MemorySink()
    t = RouterTransport(
        base_url="http://mock/v1", api_key="k", service="kg-api", config=env.stack.config, sink=sink, hmac_secret="s", inner=NoUsage()
    )
    async with httpx.AsyncClient(transport=t, base_url="http://mock/v1/") as c:
        with egress(classification="internal", purpose="x"):
            await c.post(
                "chat/completions", json={"model": "oso/fast", "messages": [{"role": "user", "content": "count these tokens please"}]}
            )
    rec = sink.records[-1]
    assert rec.usage_estimated and rec.prompt_tokens and rec.completion_tokens and rec.tokenizer == "tiktoken:o200k_base"


# ---- T-MG-05 config validation -----------------------------------------------------------------------------------------------------
def _write(tmp_path: Path, purposes: str, name: str = "p.yaml") -> Path:
    p = tmp_path / name
    p.write_text(purposes)
    return p


GOOD = (ROOT / "compat/model-purposes.yaml").read_text()


def test_mg05_good_config_loads():
    cfg = load_config(ROOT / "compat/model-purposes.yaml", ROOT / "compat/pricing.yaml")
    assert (
        cfg.embedding.dim == 384
        and "oso/embedding" in cfg.aliases()
        and cfg.egress_allows("restricted")
        and not cfg.egress_allows("local_only")
    )


def test_mg05_missing_embedding_dim_is_a_startup_error(tmp_path):
    with pytest.raises(ConfigError, match="dim"):
        load_config(_write(tmp_path, GOOD.replace("    dim: 384\n", "")), ROOT / "compat/pricing.yaml")


def test_mg05_classification_both_allowed_and_denied_is_a_startup_error(tmp_path):
    with pytest.raises(ConfigError, match="both allowed and denied"):
        load_config(
            _write(tmp_path, GOOD.replace("denied_classifications: [local_only]", "denied_classifications: [local_only, restricted]")),
            ROOT / "compat/pricing.yaml",
        )


def test_mg05_missing_embedding_purpose_and_missing_file(tmp_path):
    with pytest.raises(ConfigError):
        load_config(
            _write(tmp_path, "model_purposes: {}\negress: {allowed_classifications: [], denied_classifications: []}\n"),
            ROOT / "compat/pricing.yaml",
        )
    with pytest.raises(ConfigError):
        load_config(tmp_path / "nope.yaml", ROOT / "compat/pricing.yaml")
