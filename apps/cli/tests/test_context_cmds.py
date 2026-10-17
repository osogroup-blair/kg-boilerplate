"""`kg context` (P6 WP10): build as an agent through the API's act-as, read a trace, with the API replaced by a mock transport (no stack needed)."""

from __future__ import annotations

import json

import httpx
import pytest
from kg_cli import context_cmds
from kg_cli.main import app
from typer.testing import CliRunner

pytestmark = pytest.mark.unit
PLANNING = "a0000000-0000-4000-8000-0000000000a1"
CTX = "c0000000-0000-7000-8000-000000000001"


def client_with(handler):  # type: ignore[no-untyped-def]
    return lambda workspace: httpx.Client(
        base_url="http://kg.test", transport=httpx.MockTransport(handler), headers={"authorization": "Bearer k"}
    )


def test_build_resolves_the_handle_and_acts_as_that_principal(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/principals":
            return httpx.Response(200, json={"items": [{"id": PLANNING, "handle": "agent_planning@demo"}], "next_cursor": None})
        seen["header"], seen["body"] = request.headers.get("x-act-as-principal"), json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "status": "complete",
                "context_id": CTX,
                "context_text": "[CONTEXT DATA]\n- hello\n[END CONTEXT DATA]",
                "profile": {"name": "planning"},
                "budgets": {"used_tokens": 40, "max_tokens": 600},
                "unavailable_sources": [{"source": "graph", "reason": "timeout"}],
            },
        )

    monkeypatch.setattr(context_cmds, "_client", client_with(handler))
    result = CliRunner().invoke(
        app, ["context", "build", "Help Sarah", "--as", "agent_planning", "--profile", "planning", "--budget", "600", "--subject", "s1"]
    )
    assert result.exit_code == 0, result.output
    assert seen["header"] == PLANNING and seen["body"] == {
        "task": "Help Sarah",
        "profile": "planning",
        "subject_ids": ["s1"],
        "max_tokens": 600,
    }
    assert (
        "status=complete" in result.output
        and "tokens=40/600" in result.output
        and "unavailable: graph (timeout)" in result.output
        and "- hello" in result.output
    )


def test_build_reports_an_unknown_agent_and_an_api_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/principals":
            return httpx.Response(200, json={"items": [], "next_cursor": None})
        return httpx.Response(
            422, json={"error": {"code": "exceeds_profile", "message": "too much", "details": {"fields": ["max_tokens"]}}}
        )

    monkeypatch.setattr(context_cmds, "_client", client_with(handler))
    missing = CliRunner().invoke(app, ["context", "build", "x", "--as", "nobody"])
    assert missing.exit_code == 1 and "no principal 'nobody'" in missing.output
    refused = CliRunner().invoke(app, ["context", "build", "x", "--budget", "99999"])
    assert refused.exit_code == 1 and "exceeds_profile" in refused.output


def test_trace_prints_the_plan_and_every_candidate(monkeypatch: pytest.MonkeyPatch) -> None:
    trace = {
        "profile": "planning",
        "profile_source": "ontology",
        "status": "complete",
        "used_tokens": 40,
        "max_tokens": 600,
        "compression": "structured",
        "versions": {"ranker": "ctx-rank.v1"},
        "counts": {"selected": 1},
        "latencies": {"total": 0.05},
        "unavailable": [],
        "items": [
            {
                "position": 0,
                "kind": "fact",
                "section": "current_facts",
                "tier": "required",
                "score": 2.4,
                "included": True,
                "exclusion_reason": None,
                "id": "f1",
            },
            {
                "position": 1,
                "kind": "memory",
                "section": "episodic",
                "tier": "optional",
                "score": 0.1,
                "included": False,
                "exclusion_reason": "below_threshold",
                "id": "m1",
            },
        ],
    }
    monkeypatch.setattr(context_cmds, "_client", client_with(lambda request: httpx.Response(200, json=trace)))
    result = CliRunner().invoke(app, ["context", "trace", CTX])
    assert result.exit_code == 0, result.output
    assert (
        "planning (ontology) status=complete tokens=40/600" in result.output
        and "IN " in result.output
        and "out (below_threshold)" in result.output
    )
