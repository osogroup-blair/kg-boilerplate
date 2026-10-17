"""The SDK's own rules, with no server: the key never leaks, mutating calls carry ONE idempotency key across their retries, retries are bounded to 429/502/503/504 and honour
retry_after, every error envelope becomes its typed exception, and the request id is exposed. A `httpx.MockTransport` stands in for the platform."""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

import httpx
import pytest
from kg_platform_sdk import (
    AuthError,
    ContextIncomplete,
    InvalidEvidence,
    KGClient,
    KGClientSync,
    KGError,
    NotFound,
    QuotaExceeded,
    RateLimited,
    ServerError,
)

KEY = "kgk_abcd1234_SECRET-DO-NOT-LEAK-0123456789"
EVENT = str(uuid.uuid4())
CONTEXT = {
    "context_id": str(uuid.uuid4()),
    "status": "complete",
    "generated_at": "2026-10-12T10:00:00Z",
    "context_text": "[CONTEXT DATA] - a fact [F:abcd1234]\n[END CONTEXT DATA]",
}


def envelope(code: str, message: str = "x", **extra: Any) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, "request_id": "req-from-body", **extra}}


class Server:
    """Answers from a script, one response per call, and records what it was sent."""

    def __init__(self, *responses: httpx.Response):
        self.responses, self.seen = list(responses), []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        return self.responses.pop(0) if self.responses else httpx.Response(200, json=CONTEXT)


def ok(body: Any = None, **headers: str) -> httpx.Response:
    return httpx.Response(200, json=body if body is not None else CONTEXT, headers={"x-request-id": "req-1", **headers})


def sync_client(server: Server, **kw: Any) -> KGClientSync:
    return KGClientSync("http://kg.test", KEY, transport=httpx.MockTransport(server), sleep=lambda s: server.seen.append(s), **kw)  # type: ignore[arg-type]


def test_a_missing_key_is_refused_before_any_request(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KG_API_KEY", raising=False)
    with pytest.raises(ValueError, match="no API key"):
        KGClientSync("http://kg.test")


def test_the_key_comes_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KG_API_KEY", KEY)
    monkeypatch.setenv("KG_BASE_URL", "http://kg.test/")
    server = Server(ok())
    with KGClientSync(transport=httpx.MockTransport(server)) as kg:  # type: ignore[arg-type]
        kg.build_context("task")
    assert server.seen[0].headers["authorization"] == f"Bearer {KEY}" and str(server.seen[0].url) == "http://kg.test/v1/context/build"


def test_the_key_never_appears_in_a_repr_a_str_or_a_log(caplog: pytest.LogCaptureFixture) -> None:
    server = Server(httpx.Response(401, json=envelope("unauthenticated")))
    with caplog.at_level(logging.DEBUG), sync_client(server) as kg:
        with pytest.raises(AuthError) as err:
            kg.build_context("task")
        for text in (repr(kg), str(kg), repr(kg._cfg), str(err.value), repr(err.value), repr(err.value.__dict__), caplog.text):
            assert KEY not in text and "SECRET-DO-NOT-LEAK" not in text
    assert "redacted" in repr(kg)


def test_a_result_carries_the_request_id_and_serialises_without_it() -> None:
    server = Server(ok())
    with sync_client(server) as kg:
        ctx = kg.build_context("task")
    assert ctx.request_id == "req-1"
    assert "request_id" not in ctx.model_dump(mode="json") and ctx.as_prompt_block() == CONTEXT["context_text"]


def test_a_mutating_call_reuses_one_idempotency_key_across_its_retries() -> None:
    accepted = {"event_id": EVENT, "status": "accepted", "status_url": f"/v1/interactions/{EVENT}", "request_id": "r"}
    server = Server(
        httpx.Response(503, json=envelope("dependency_unavailable")), httpx.Response(502, json=envelope("bad_gateway")), ok(accepted)
    )
    with sync_client(server) as kg:
        kg.ingest_interaction(
            source_system="agent", source_ref="evt_1", kind="message", content="hello", occurred_at="2026-10-11T10:00:00Z"
        )
    requests = [r for r in server.seen if isinstance(r, httpx.Request)]
    keys = {r.headers["idempotency-key"] for r in requests}
    assert len(requests) == 3 and len(keys) == 1  # one logical write
    assert len([s for s in server.seen if isinstance(s, float)]) == 2  # it slept between attempts


def test_two_logical_calls_get_two_keys_and_a_caller_key_is_used_verbatim() -> None:
    accepted = {"event_id": EVENT, "status": "accepted", "status_url": "u", "request_id": "r"}
    server = Server(ok(accepted), ok(accepted), ok(accepted))
    with sync_client(server) as kg:
        a = {"source_system": "agent", "source_ref": "e", "kind": "message", "content": "x", "occurred_at": "2026-10-11T10:00:00Z"}
        kg.ingest_interaction(**a)
        kg.ingest_interaction(**a)
        kg.ingest_interaction(idempotency_key="mine-1", **a)
    keys = [r.headers["idempotency-key"] for r in server.seen if isinstance(r, httpx.Request)]
    assert keys[0] != keys[1] and keys[2] == "mine-1"


def test_a_read_carries_no_idempotency_key() -> None:
    server = Server(ok())
    with sync_client(server) as kg:
        kg.build_context("task")
    assert "idempotency-key" not in server.seen[0].headers  # a context build is a read


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 422, 500])
def test_other_statuses_are_never_retried(status: int) -> None:
    server = Server(httpx.Response(status, json=envelope("whatever")), ok())
    with sync_client(server) as kg, pytest.raises(KGError):
        kg.build_context("task")
    assert len(server.seen) == 1


def test_retries_are_bounded_and_the_last_error_is_raised() -> None:
    server = Server(*[httpx.Response(503, json=envelope("dependency_unavailable")) for _ in range(10)])
    with sync_client(server, max_retries=2) as kg, pytest.raises(ServerError):
        kg.build_context("task")
    assert len([r for r in server.seen if isinstance(r, httpx.Request)]) == 3  # the first try and two retries


def test_retry_after_is_honoured_and_a_daily_quota_is_raised_not_slept() -> None:
    limited = httpx.Response(429, json=envelope("rate_limited", retry_after=2), headers={"retry-after": "2"})
    server = Server(limited, ok())
    with sync_client(server) as kg:
        kg.build_context("task")
    waits = [s for s in server.seen if isinstance(s, float)]
    assert len(waits) == 1 and 2.0 <= waits[0] <= 3.1

    quota = httpx.Response(429, json=envelope("quota_exceeded", retry_after=40_000, details={"quota": "context_tokens_per_day"}))
    server = Server(quota, ok())
    with sync_client(server) as kg, pytest.raises(QuotaExceeded) as err:
        kg.build_context("task")
    assert err.value.retry_after == 40_000 and err.value.details == {"quota": "context_tokens_per_day"}
    assert len(server.seen) == 1  # raised at once: sleeping for hours is the caller's decision


@pytest.mark.parametrize(
    ("status", "code", "cls"),
    [
        (401, "unauthenticated", AuthError),
        (404, "not_found", NotFound),
        (422, "invalid_evidence", InvalidEvidence),
        (429, "rate_limited", RateLimited),
        (503, "context_incomplete", ContextIncomplete),
    ],
)
def test_every_error_envelope_becomes_its_typed_exception(status: int, code: str, cls: type[KGError]) -> None:
    server = Server(httpx.Response(status, json=envelope(code, "m", details={"x": 1}), headers={"x-request-id": "req-9"}))
    with sync_client(server, max_retries=0) as kg, pytest.raises(cls) as err:
        kg.get_evidence(uuid.uuid4())
    assert err.value.code == code and err.value.status == status and err.value.request_id == "req-from-body"


def test_a_body_that_is_not_the_envelope_still_raises_a_typed_error() -> None:
    server = Server(httpx.Response(502, text="<html>bad gateway</html>"))
    with sync_client(server, max_retries=0) as kg, pytest.raises(ServerError) as err:
        kg.build_context("task")
    assert err.value.status == 502


def test_a_transport_failure_is_a_kgerror_without_the_url_or_key() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    with KGClientSync("http://kg.test", KEY, transport=httpx.MockTransport(boom)) as kg, pytest.raises(KGError) as err:
        kg.build_context("task")
    assert err.value.code == "transport_error" and KEY not in str(err.value)


def test_publish_reads_the_revision_when_it_is_not_given() -> None:
    mem = str(uuid.uuid4())
    memory = {
        "id": mem, "type": "working", "origin": "working", "status": "active", "summary": "s", "authority_class": "agent_generated", "evidence_status": "unverified",
        "classification": "internal", "importance": 0.5, "created_by": str(uuid.uuid4()), "recorded_at": "2026-10-12T10:00:00Z", "effective_from": "2026-10-12T10:00:00Z",
        "embedding_status": "pending", "revision": 4, "scopes": [], "subject_ids": [], "content": "c", "evidence": [], "lineage": [],
        "embedding": {"status": "pending"}, "agent_generated": True, "command_id": str(uuid.uuid4()), "effective_until": None, "expires_at": None,
        "pipeline_version": "p5.v1",
    }  # fmt: skip
    written = {"memory_id": str(uuid.uuid4()), "status": "proposed", "deduplicated": False, "revision": 1, "command_id": str(uuid.uuid4())}
    server = Server(ok(memory, etag='W/"rev-4"'), ok(written))
    with sync_client(server) as kg:
        kg.publish_memory(mem, [{"kind": "project", "ref": "newco-plan"}])
    publish = server.seen[1]
    assert publish.headers["if-match"] == 'W/"rev-4"' and publish.headers["idempotency-key"] and publish.method == "POST"
    assert json.loads(publish.content)["scopes"] == [{"kind": "project", "ref": "newco-plan"}]


async def test_the_async_client_retries_the_same_way() -> None:
    accepted = {"event_id": EVENT, "status": "accepted", "status_url": "u", "request_id": "r"}
    server = Server(httpx.Response(429, json=envelope("rate_limited", retry_after=1)), ok(accepted))
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    async with KGClient("http://kg.test", KEY, transport=httpx.MockTransport(server), sleep=sleep) as kg:  # type: ignore[arg-type]
        out = await kg.ingest_interaction(
            source_system="agent", source_ref="e", kind="message", content="x", occurred_at="2026-10-11T10:00:00Z"
        )
    assert out.request_id == "req-1" and len(slept) == 1 and 1.0 <= slept[0] <= 2.3
    assert len({r.headers["idempotency-key"] for r in server.seen}) == 1
