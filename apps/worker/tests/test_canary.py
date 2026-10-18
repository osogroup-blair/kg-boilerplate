"""The canary's verdicts, against a stub API: complete -> ok, partial -> degraded, a budget overrun is its own outcome, and anything unreadable or unauthorised is a failure."""

from __future__ import annotations

import httpx
import pytest
from kg_worker.canary import probe


def client(handler) -> httpx.AsyncClient:  # type: ignore[no-untyped-def]
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://api.test")


def ok(status: str = "complete", used: int = 120, limit: int = 600) -> httpx.Response:
    return httpx.Response(200, json={"status": status, "budgets": {"used_tokens": used, "max_tokens": limit}})


@pytest.mark.parametrize(
    ("response", "outcome"),
    [
        (ok("complete"), "ok"),
        (ok("partial"), "degraded"),
        (ok("complete", used=601), "budget_violation"),
        (ok("denied"), "failed"),
        (ok("insufficient_evidence"), "failed"),
        (httpx.Response(401, json={}), "failed"),
        (httpx.Response(503, json={}), "failed"),
        (httpx.Response(200, text="<html>"), "failed"),
        (httpx.Response(200, json={"status": "complete"}), "failed"),
    ],
)
async def test_the_outcome_of_one_probe(response, outcome):
    async with client(lambda request: response) as c:
        assert await probe(c, task="t", max_tokens=None) == outcome


async def test_a_transport_error_is_a_failed_probe_not_an_exception():
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    async with client(boom) as c:
        assert await probe(c, task="t", max_tokens=None) == "failed"


async def test_the_leak_probe_is_clean_when_nothing_comes_back_and_a_leak_when_the_marker_does():
    from kg_worker.canary import leak_probe

    def clean(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": [], "context_text": "nothing relevant"})

    def leaking(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": [{"content": "the secret zq-marker-17 is here"}], "context_text": ""})

    async with client(clean) as c:
        assert await leak_probe(c, marker="zq-marker-17") == "clean"
        assert await leak_probe(c, marker="") == "skipped"
    async with client(leaking) as c:
        assert await leak_probe(c, marker="zq-marker-17") == "leak"
    async with client(lambda request: httpx.Response(503, json={})) as c:
        assert await leak_probe(c, marker="zq-marker-17") == "error"
