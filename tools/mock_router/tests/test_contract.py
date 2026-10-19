"""Contract tests: each test pins one behaviour from docs/router-contract.md. Derived from the router's zod schema + seed."""
from __future__ import annotations

import json
import math
import uuid

import openai
import pytest

from tools.mock_router.app import CATALOG
from tools.mock_router.fixtures import fixture_key

from .conftest import AUTH, KEY, make_client

MSG = [{"role": "user", "content": "hello"}]


def chat(client, **body):
    body.setdefault("model", "oso/general")
    body.setdefault("messages", MSG)
    return client.post("/v1/chat/completions", json=body, headers=AUTH)


# ---- catalog is the router seed ------------------------------------------------------------------------------------------
def test_catalog_structured_output_models_match_the_seed():
    so = sorted(m for m, v in CATALOG["models"].items() if "structured_output" in v["capabilities"])
    assert so == ["gemini-2.5-pro", "gemini-3.1-pro-preview", "gemini-3.5-flash", "gemini-3.8-flash", "glm-5.2"]


def test_nine_aliases():
    assert sorted(CATALOG["aliases"]) == sorted(["oso/fast", "oso/general", "oso/reasoning", "oso/code", "oso/code-high", "oso/vision",
                                                 "oso/general-vision", "oso/frontier", "oso/embedding"])


# ---- health / auth / models ---------------------------------------------------------------------------------------------
def test_health_needs_no_auth(client):
    assert client.get("/health").json() == {"status": "ok"}


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"}, {"Authorization": KEY}])
def test_bad_key_is_authentication_error_with_request_id(client, headers):
    r = client.post("/v1/chat/completions", json={"model": "oso/fast", "messages": MSG}, headers=headers)
    assert r.status_code == 401
    err = r.json()["error"]
    assert err["code"] == err["type"] == "authentication_error"
    assert err["request_id"] == r.headers["x-request-id"]


def test_models_lists_aliases_only_with_min_limits(client):
    data = client.get("/v1/models", headers=AUTH).json()["data"]
    ids = [m["id"] for m in data]
    assert "oso/general" in ids and "gpt-oss-120b" not in ids
    general = next(m for m in data if m["id"] == "oso/general")
    assert general["context_length"] == min(CATALOG["models"][m]["context_window"] for m in CATALOG["aliases"]["oso/general"])


# ---- request id handling ------------------------------------------------------------------------------------------------
def test_valid_uuid_request_id_is_reused_and_echoed(client):
    rid = str(uuid.uuid4())
    r = client.post("/v1/chat/completions", json={"model": "oso/fast", "messages": MSG}, headers={**AUTH, "x-request-id": rid})
    assert r.headers["x-request-id"] == rid and r.headers["x-gateway-request-id"] == rid


def test_non_uuid_request_id_is_replaced(client):
    r = client.post("/v1/chat/completions", json={"model": "oso/fast", "messages": MSG}, headers={**AUTH, "x-request-id": "not-a-uuid"})
    assert r.headers["x-request-id"] != "not-a-uuid"
    uuid.UUID(r.headers["x-request-id"])


# ---- chat basics ---------------------------------------------------------------------------------------------------------
def test_chat_echoes_alias_and_reports_usage(client):
    r = chat(client, model="oso/general")
    body = r.json()
    assert r.status_code == 200 and body["model"] == "oso/general" and body["object"] == "chat.completion"
    u = body["usage"]
    assert u["prompt_tokens"] > 0 and u["completion_tokens"] > 0 and u["total_tokens"] == u["prompt_tokens"] + u["completion_tokens"]
    assert body["choices"][0]["message"]["content"] == "echo: hello"


def test_unknown_top_level_fields_are_stripped_not_rejected(client):
    r = chat(client, logprobs=True, top_logprobs=5, logit_bias={"1": 1}, seed=7, n=2)
    assert r.status_code == 200
    assert len(r.json()["choices"]) == 1                      # n is ignored
    assert "logprobs" not in r.json()["choices"][0]           # no logprobs come back
    log = client.get("/__mock/requests").json()["requests"][-1]
    assert {"logprobs", "top_logprobs", "logit_bias", "seed", "n"} <= set(log["stripped_fields"])


def test_direct_model_name_works_without_fallback(client):
    assert chat(client, model="gemma-4-26b").status_code == 200


def test_unknown_model_is_model_not_found(client):
    r = chat(client, model="oso/nope")
    assert r.status_code == 404 and r.json()["error"]["code"] == "model_not_found"


@pytest.mark.parametrize("body", [{"messages": []}, {"messages": [{"role": "developer", "content": "x"}]}, {"temperature": 3}, {"max_tokens": 0}])
def test_invalid_requests_are_400(client, body):
    r = chat(client, **body)
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_request"


def test_system_content_must_be_a_string(client):
    r = chat(client, messages=[{"role": "system", "content": [{"type": "text", "text": "x"}]}, *MSG])
    assert r.status_code == 400


# ---- capability gating ----------------------------------------------------------------------------------------------------
SCHEMA = {"type": "json_schema", "json_schema": {"name": "Out", "schema": {
    "type": "object", "properties": {"name": {"type": "string"}, "n": {"type": "integer"}, "tags": {"type": "array", "items": {"type": "string"}}},
    "required": ["name", "n", "tags"]}}}


def test_json_schema_on_oso_fast_is_unsupported_capability(client):
    r = chat(client, model="oso/fast", response_format=SCHEMA)
    assert r.status_code == 422 and r.json()["error"]["code"] == "unsupported_capability"


@pytest.mark.parametrize("alias", ["oso/general", "oso/code", "oso/reasoning"])
def test_json_schema_on_capable_aliases_returns_valid_object(client, alias):
    r = chat(client, model=alias, response_format=SCHEMA)
    assert r.status_code == 200
    obj = json.loads(r.json()["choices"][0]["message"]["content"])
    assert obj == {"name": "sample", "n": 0, "tags": []}


def test_json_object_also_requires_structured_output(client):
    assert chat(client, model="oso/fast", response_format={"type": "json_object"}).status_code == 422


def test_text_response_format_does_not_gate(client):
    assert chat(client, model="oso/fast", response_format={"type": "text"}).status_code == 200


def test_tools_require_tools_capability(client):
    tool = {"type": "function", "function": {"name": "f", "parameters": {}}}
    assert chat(client, model="oso/fast", tools=[tool]).status_code == 200      # gpt-oss-20b has tools
    assert chat(client, model="gemini-3.5-flash-lite", tools=[tool]).status_code == 200
    assert chat(client, model="qwen3-coder", tools=[tool]).status_code == 200


# ---- classification / egress ---------------------------------------------------------------------------------------------
def test_local_only_is_data_policy_violation_via_routing_and_metadata(client):
    for body in ({"routing": {"data_classification": "local_only"}}, {"metadata": {"data_classification": "local_only"}},
                 {"routing": {"privacy": "local_only"}}):
        r = chat(client, **body)
        assert r.status_code == 422 and r.json()["error"]["code"] == "data_policy_violation"


def test_routing_classification_wins_over_metadata(client):
    r = chat(client, routing={"data_classification": "public"}, metadata={"data_classification": "local_only"})
    assert r.status_code == 200


def test_key_max_classification_caps_requests():
    c = make_client(max_classification="internal")          # the real seeded client key
    assert chat(c, routing={"data_classification": "internal"}).status_code == 200
    r = chat(c, routing={"data_classification": "confidential"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "classification_not_permitted"


# ---- errors / faults -----------------------------------------------------------------------------------------------------
def test_rate_limited_fault_carries_retry_after(client):
    client.post("/__mock/faults", json={"faults": [{"code": "rate_limited", "count": 1, "endpoint": "chat"}]})
    r = chat(client)
    assert r.status_code == 429 and r.headers["retry-after"] == "1" and r.json()["error"]["code"] == "rate_limited"
    assert chat(client).status_code == 200                   # queue drained


@pytest.mark.parametrize("code,status", [("unknown_provider_error", 502), ("provider_unavailable", 503), ("timeout", 504), ("budget_exceeded", 429)])
def test_injected_fault_statuses_match_router_table(client, code, status):
    client.post("/__mock/faults", json={"faults": [{"code": code, "count": 1}]})
    r = chat(client)
    assert r.status_code == status and r.json()["error"]["code"] == code


def test_simulate_failure_falls_back_when_alternatives_exist(client):
    assert chat(client, model="oso/general", routing={"simulate_failure": "primary_timeout"}).status_code == 200


def test_simulate_failure_with_fallback_false_fails(client):
    r = chat(client, model="oso/general", routing={"simulate_failure": "primary_timeout", "fallback": False})
    assert r.status_code == 504 and r.json()["error"]["code"] == "timeout"


def test_single_candidate_has_no_fallback_for_structured_output(client):
    """oso/general has exactly one structured_output candidate, so a simulated primary failure there is still fatal for extraction."""
    r = chat(client, model="oso/general", response_format=SCHEMA, routing={"simulate_failure": "provider_unavailable"})
    assert r.status_code == 503


# ---- fixtures ------------------------------------------------------------------------------------------------------------
def test_fixture_replay_by_rule_and_by_key(tmp_path):
    msgs = [{"role": "system", "content": "extract"}, {"role": "user", "content": "Sarah  joined Acme"}]
    key = fixture_key("oso/general", msgs, "Out")
    (tmp_path / "a.json").write_text(json.dumps([
        {"key": key, "response": {"name": "keyed", "n": 1, "tags": []}},
        {"when": {"alias": "oso/general", "contains": ["rule me"]}, "response": "by rule"},
    ]))
    c = make_client(fixtures_dir=tmp_path)
    keyed = chat(c, messages=msgs, response_format=SCHEMA)       # whitespace is normalised in the key
    assert json.loads(keyed.json()["choices"][0]["message"]["content"])["name"] == "keyed"
    ruled = chat(c, messages=[{"role": "user", "content": "please RULE ME now"}])
    assert ruled.json()["choices"][0]["message"]["content"] == "by rule"


def test_streaming_emits_sse_and_done(client):
    r = chat(client, model="oso/general", stream=True)
    assert r.headers["content-type"].startswith("text/event-stream") and r.text.rstrip().endswith("data: [DONE]")


# ---- embeddings ----------------------------------------------------------------------------------------------------------
def embed(client, **body):
    body.setdefault("model", "oso/embedding")
    return client.post("/v1/embeddings", json=body, headers=AUTH)


def test_embeddings_are_384_dim_unit_vectors_in_order(client):
    r = embed(client, input=["alpha beta", "gamma"])
    data = r.json()["data"]
    assert [d["index"] for d in data] == [0, 1] and r.json()["model"] == "oso/embedding"
    for d in data:
        assert len(d["embedding"]) == 384 and math.isclose(sum(x * x for x in d["embedding"]), 1.0, rel_tol=1e-6)
    assert r.json()["usage"]["prompt_tokens"] == r.json()["usage"]["total_tokens"] > 0


def test_embeddings_are_deterministic_and_lexically_similar(client):
    a, b, c = (embed(client, input=t).json()["data"][0]["embedding"] for t in ["sarah works at acme", "sarah works at acme corp", "completely unrelated words here"])
    dot = lambda x, y: sum(p * q for p, q in zip(x, y))
    assert a == embed(client, input="sarah works at acme").json()["data"][0]["embedding"]
    assert dot(a, b) > dot(a, c)


def test_e5_prefixes_are_not_content(client):
    q = embed(client, input="query: sarah acme").json()["data"][0]["embedding"]
    p = embed(client, input="passage: sarah acme").json()["data"][0]["embedding"]
    assert q == p


def test_embedding_alias_rejects_chat_and_chat_alias_rejects_embeddings(client):
    assert chat(client, model="oso/embedding").json()["error"]["code"] == "unsupported_capability"
    assert embed(client, model="oso/general", input="x").json()["error"]["code"] == "unsupported_capability"


def test_overlong_embedding_input_truncates_at_512_then_errors_past_8192(client):
    long_ = "word " * 700                                    # ~700 tokens: over the model's 512, under the deployment's 8192
    r = embed(client, input=long_)
    assert r.status_code == 200 and r.headers.get("x-mock-truncated") == "true" and r.json()["usage"]["prompt_tokens"] == 512
    huge = embed(client, input="word " * 9000)
    assert huge.status_code == 400 and huge.json()["error"]["code"] == "context_too_large"


def test_embeddings_have_no_dimensions_or_encoding_format_field(client):
    embed(client, input="x", dimensions=64, encoding_format="base64")
    log = client.get("/__mock/requests").json()["requests"][-1]
    assert {"dimensions", "encoding_format"} <= set(log["stripped_fields"])


# ---- the real OpenAI SDK against a live server ---------------------------------------------------------------------------
def sdk(live) -> openai.OpenAI:
    return openai.OpenAI(base_url=live.base_url, api_key=KEY, max_retries=0)


def test_sdk_chat_and_usage(live):
    r = sdk(live).chat.completions.create(model="oso/general", messages=MSG)
    assert r.model == "oso/general" and r.choices[0].message.content == "echo: hello" and r.usage.total_tokens > 0


def test_sdk_json_schema(live):
    r = sdk(live).chat.completions.create(model="oso/code", messages=MSG, response_format=SCHEMA)
    assert json.loads(r.choices[0].message.content)["name"] == "sample"


def test_sdk_embeddings(live):
    r = sdk(live).embeddings.create(model="oso/embedding", input=["a b", "c d"])
    assert len(r.data) == 2 and len(r.data[0].embedding) == 384


def test_sdk_error_paths(live):
    c = sdk(live)
    with pytest.raises(openai.UnprocessableEntityError) as e:
        c.chat.completions.create(model="oso/fast", messages=MSG, response_format=SCHEMA)
    assert e.value.body["code"] == "unsupported_capability" if isinstance(e.value.body, dict) else True
    with pytest.raises(openai.NotFoundError):
        c.chat.completions.create(model="oso/nope", messages=MSG)
    with pytest.raises(openai.AuthenticationError):
        openai.OpenAI(base_url=live.base_url, api_key="bad", max_retries=0).chat.completions.create(model="oso/fast", messages=MSG)


def test_sdk_beta_parse_depends_on_responses_free_chat_path(live):
    """`beta.chat.completions.parse` posts to /chat/completions with json_schema, so it works; the Responses API does not exist."""
    from pydantic import BaseModel

    class Out(BaseModel):
        name: str
        n: int
        tags: list[str]

    r = sdk(live).beta.chat.completions.parse(model="oso/general", messages=MSG, response_format=Out)
    assert r.choices[0].message.parsed == Out(name="sample", n=0, tags=[])
    with pytest.raises(openai.NotFoundError):
        sdk(live).responses.create(model="oso/general", input="hi")


# ---- scripted assistant messages (P10: agent examples) -------------------------------------------------------------------
def test_a_rule_can_script_tool_calls_and_a_later_turn_answers_with_text(client):
    rules = [
        {"when": {"alias": "oso/general", "contains": ["plan the trip"], "not_contains": ["tool result"]},
         "response": {"_message": {"content": None, "tool_calls": [{"function": {"name": "lookup", "arguments": {"q": "flights"}}}]}}},
        {"when": {"alias": "oso/general", "contains": ["tool result"]}, "response": "Book the 9am flight."},
    ]
    assert client.post("/__mock/fixtures", json={"rules": rules}).status_code == 200
    tools = [{"type": "function", "function": {"name": "lookup", "description": "d", "parameters": {"type": "object", "properties": {}}}}]
    first = chat(client, messages=[{"role": "user", "content": "plan the trip"}], tools=tools).json()["choices"][0]
    assert first["finish_reason"] == "tool_calls" and first["message"]["content"] is None
    call = first["message"]["tool_calls"][0]
    assert call["id"] and call["type"] == "function" and json.loads(call["function"]["arguments"]) == {"q": "flights"}
    msgs = [{"role": "user", "content": "plan the trip"}, first["message"], {"role": "tool", "tool_call_id": call["id"], "content": "tool result: 9am"}]
    second = chat(client, messages=msgs, tools=tools).json()["choices"][0]
    assert second["finish_reason"] == "stop" and second["message"]["content"] == "Book the 9am flight."
