"""P7 WP7: the MCP server is a stateless transport over the SAME authorised API (AGT-03, SDK/MCP parity, no privileged path). Real MCP protocol over Streamable HTTP, in process.

* tools/list is exactly the specified tools, each with `additionalProperties: false`: a `workspace_id` / `principal_id` / `agent_id` / `group_id` argument is an error;
* for every tool the MCP data equals the HTTP result for the same key and request, and a principal denied over HTTP is denied through MCP;
* a missing or revoked key is an `unauthenticated` tool error with the platform's own envelope;
* the process has no database or router configuration and imports nothing of the platform but the SDK."""

from __future__ import annotations

import json
import subprocess
import sys
import uuid
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from asgi_lifespan import LifespanManager
from kg_mcp.app import create_app
from kg_mcp.config import ENV_FIELDS, McpSettings
from kg_mcp.tools import TOOLS
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from tests.support.p4 import sarah_story, strict_mock  # noqa: F401
from tests.support.p5 import run_embeds
from tests.support.p7 import FINDING, PROJECT, Agent, accept_event_as, propose, register, revoke

pytestmark = [pytest.mark.integration, pytest.mark.contract]
EXPECTED_TOOLS = {
    "build_context", "search_memory", "propose_memory", "save_working_memory", "publish_memory", "ingest_interaction", "get_interaction_status", "get_evidence", "list_profiles",
}  # fmt: skip


@asynccontextmanager
async def mcp_session(api: Any, key: str | None):
    app = create_app(McpSettings(api_url="http://kg.test"), transport=httpx.ASGITransport(app=api.app))
    async with LifespanManager(app):

        def factory(headers: dict[str, str] | None = None, timeout: Any = None, auth: Any = None) -> httpx.AsyncClient:
            return httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), headers=headers, timeout=timeout, auth=auth, follow_redirects=True
            )

        headers = {"authorization": f"Bearer {key}"} if key else {}
        async with streamablehttp_client("http://mcp.test/mcp/", headers=headers, httpx_client_factory=factory) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session


async def call(session: ClientSession, name: str, **args: Any) -> Any:
    return await session.call_tool(name, args)


def data(result: Any) -> dict[str, Any]:
    assert not result.isError, result.content
    return dict(result.structuredContent)


def error(result: Any) -> dict[str, Any]:
    assert result.isError, "expected a tool error"
    return dict(result.structuredContent["error"])


async def world(api: Any, t: Any) -> tuple[dict[str, str], Agent, Agent]:
    ids = await sarah_story(t, projected=False)
    research = await register(
        t, "research-agent", subjects=(ids["sarah"],), privileges=("read", "propose", "publish"), allowed_profiles=["research", "planning"]
    )
    planning = await register(t, "planning-agent", subjects=(ids["sarah"],), privileges=("read",), allowed_profiles=["planning"])
    event = await accept_event_as(research, {"kind": "message", "content": "Research: the NewCo plan needs a state licence."})
    r = await propose(research, FINDING, event, subject_ids=[ids["sarah"]])
    assert r.status_code == 202
    await run_embeds(api, t)
    return {**ids, "event": event, "finding": r.json()["memory_id"]}, research, planning


async def test_tools_list_is_exactly_the_specified_tools_and_every_schema_is_closed(api, tenant_agents):
    t = tenant_agents
    agent = await register(t, "any-agent", project=None)
    async with mcp_session(api, agent.key) as s:
        tools = (await s.list_tools()).tools
    assert {x.name for x in tools} == EXPECTED_TOOLS == {t_.name for t_ in TOOLS}
    for tool in tools:
        assert tool.inputSchema["additionalProperties"] is False and tool.inputSchema["type"] == "object", tool.name
        assert not {"workspace_id", "group_id", "principal_id", "agent_id"} & set(tool.inputSchema.get("properties", {})), tool.name
        assert tool.description


async def test_an_argument_naming_a_workspace_or_an_identity_is_a_validation_error_agt03(api, tenant_agents, strict_mock):  # noqa: F811
    t = tenant_agents
    w, research, _ = await world(api, t)
    async with mcp_session(api, research.key) as s:
        for forbidden in ("workspace_id", "group_id", "principal_id", "agent_id"):
            for tool, args in (
                ("build_context", {"task": "x", "subject_ids": [w["sarah"]]}),
                ("search_memory", {"query": "licence"}),
                ("list_profiles", {}),
                ("get_evidence", {"fact_id": w["em"]}),
                ("get_interaction_status", {"event_id": w["event"]}),
            ):
                result = await call(s, tool, **args, **{forbidden: str(uuid.uuid4())})
                assert result.isError and "validation" in json.dumps(result.content[0].text).lower(), (tool, forbidden)
        ok = await call(s, "list_profiles")
        assert not ok.isError


async def test_parity_the_mcp_data_equals_the_http_data_for_the_same_key(api, tenant_agents, strict_mock):  # noqa: F811
    t = tenant_agents
    w, research, planning = await world(api, t)
    async with mcp_session(api, planning.key) as s:
        via_mcp = data(
            await call(
                s,
                "build_context",
                task="What licence does NewCo need to operate?",
                subject_ids=[w["sarah"]],
                profile="planning",
                project=PROJECT,
                max_tokens=900,
            )
        )
        via_http = (
            await planning.build(
                "What licence does NewCo need to operate?", subject_ids=[w["sarah"]], profile="planning", project=PROJECT, max_tokens=900
            )
        ).json()
        for field in (
            "status",
            "context_text",
            "facts",
            "memories",
            "instructions",
            "unknowns",
            "conflicts",
            "citations",
            "budgets",
            "profile",
            "compression",
            "unavailable_sources",
        ):
            assert via_mcp[field] == via_http[field], field
        assert "(agent_generated, unverified)" in via_mcp["context_text"] and via_mcp["context_text"].startswith(
            "[CONTEXT DATA"
        )  # the P6 data envelope
        assert via_mcp["context_text"].rstrip().endswith("[END CONTEXT DATA]")
        text_result = await call(
            s,
            "build_context",
            task="What licence does NewCo need to operate?",
            subject_ids=[w["sarah"]],
            profile="planning",
            project=PROJECT,
        )
        assert text_result.content[0].text.startswith("[CONTEXT DATA")  # the text content IS the data block
        found_mcp = data(await call(s, "search_memory", query="state licence", project=PROJECT))
        found_http = (await planning.req("POST", "/v1/memories/search", json={"query": "state licence", "project": PROJECT})).json()
        assert found_mcp["hits"] == found_http["hits"] and found_mcp["hits"]
        assert (
            data(await call(s, "get_evidence", fact_id=w["em"]))["items"]
            == (await planning.get(f"/v1/relationships/{w['em']}/evidence")).json()
        )
        assert data(await call(s, "list_profiles"))["items"] == (await planning.get("/v1/context/profiles")).json()
        assert (
            error(await call(s, "get_interaction_status", event_id=w["event"]))["code"] == "not_found"
        )  # an agent reads only its OWN submissions, as over HTTP
        assert (await planning.get(f"/v1/interactions/{w['event']}")).status_code == 404
    async with mcp_session(api, research.key) as s:
        accepted = data(
            await call(
                s,
                "ingest_interaction",
                source_system="research-notes",
                source_ref="evt_007",
                kind="message",
                content="more notes",
                occurred_at="2026-10-11T09:00:00Z",
            )
        )
        assert accepted["status"] == "accepted"
        via_http = await research.get(f"/v1/interactions/{accepted['event_id']}")
        assert via_http.status_code == 200
        assert data(await call(s, "get_interaction_status", event_id=accepted["event_id"]))["id"] == accepted["event_id"]
        note = data(await call(s, "save_working_memory", run_id="r7", content="check NewCo licensing"))
        assert note["status"] == "active"
        prop = data(
            await call(
                s,
                "propose_memory",
                type="semantic",
                content="another finding about Sarah",
                scopes=[{"kind": "project", "ref": PROJECT}],
                source_event_ids=[accepted["event_id"]],
                subject_ids=[w["sarah"]],
                purpose="finding",
            )
        )
        assert prop["status"] == "active"  # the privilege and the policy are the same through MCP
        private = data(
            await call(
                s,
                "propose_memory",
                type="semantic",
                content="my hunch",
                scopes=[{"kind": "agent", "ref": "self"}],
                source_event_ids=[accepted["event_id"]],
                purpose="note",
            )
        )
        published = data(await call(s, "publish_memory", memory_id=private["memory_id"], scopes=[{"kind": "project", "ref": PROJECT}]))
        assert published["memory_id"] != private["memory_id"]
        bad = error(
            await call(
                s,
                "propose_memory",
                type="semantic",
                content="bad",
                scopes=[{"kind": "project", "ref": PROJECT}],
                source_event_ids=[str(uuid.uuid4())],
                purpose="x",
            )
        )
        assert bad["code"] == "invalid_evidence" and bad["request_id"]  # the platform's own envelope


async def test_an_agent_with_no_grant_reaches_nothing_through_any_tool(api, tenant_agents, strict_mock):  # noqa: F811
    t = tenant_agents
    w, research, planning = await world(api, t)
    blind = await register(t, "blind-agent", project=None)
    async with mcp_session(api, blind.key) as s:
        assert data(await call(s, "build_context", task="Tell me about Sarah", subject_ids=[w["sarah"]]))["status"] == "denied"
        assert data(await call(s, "search_memory", query="state licence", project=PROJECT))["hits"] == []
        assert data(await call(s, "search_memory", query="Sarah"))["hits"] == []
        assert error(await call(s, "get_evidence", fact_id=w["em"]))["code"] == "not_found"
        assert error(await call(s, "get_interaction_status", event_id=w["event"]))["code"] == "not_found"  # another agent's submission
        denied = await call(
            s,
            "propose_memory",
            type="semantic",
            content="x",
            scopes=[{"kind": "project", "ref": PROJECT}],
            source_event_ids=[w["event"]],
            purpose="x",
        )
        assert error(denied)["code"] in ("invalid_scope", "invalid_evidence")
        for result in (
            await call(s, "build_context", task="Sarah licence", subject_ids=[w["sarah"]]),
            await call(s, "search_memory", query="licence"),
        ):
            assert "state licence" not in json.dumps(result.structuredContent)


async def test_a_missing_or_revoked_key_makes_every_tool_an_unauthenticated_error(api, tenant_agents, strict_mock):  # noqa: F811
    t = tenant_agents
    w, research, _ = await world(api, t)
    args = {
        "build_context": {"task": "x"}, "search_memory": {"query": "x"}, "list_profiles": {}, "get_evidence": {"fact_id": w["em"]}, "get_interaction_status": {"event_id": w["event"]},
        "save_working_memory": {"run_id": "r", "content": "c"}, "ingest_interaction": {"source_system": "s", "source_ref": "r", "kind": "message", "content": "c", "occurred_at": "2026-10-11T09:00:00Z"},
        "propose_memory": {"type": "semantic", "content": "c", "scopes": [{"kind": "project", "ref": PROJECT}], "source_event_ids": [w["event"]], "purpose": "p"},
        "publish_memory": {"memory_id": w["finding"], "scopes": [{"kind": "project", "ref": PROJECT}]},
    }  # fmt: skip
    assert set(args) == EXPECTED_TOOLS
    async with mcp_session(api, None) as s:  # no Authorization header at all
        for tool, a in args.items():
            assert error(await call(s, tool, **a))["code"] == "unauthenticated", tool
    async with mcp_session(api, research.key) as s:
        assert not (await call(s, "list_profiles")).isError
        assert (await revoke(t, research, "compromised")).status_code == 200
        for tool, a in args.items():  # the SAME session, after the revoke: every tool fails on the next call
            err = error(await call(s, tool, **a))
            assert err["code"] == "unauthenticated" and err["message"], tool
    async with mcp_session(api, "kgk_notakey_" + "x" * 40) as s:
        assert error(await call(s, "list_profiles"))["code"] == "unauthenticated"


async def test_one_server_serves_two_keys_without_mixing_them_up(api, tenant_agents, strict_mock):  # noqa: F811
    t = tenant_agents
    w, research, planning = await world(api, t)
    app = create_app(McpSettings(api_url="http://kg.test"), transport=httpx.ASGITransport(app=api.app))
    async with LifespanManager(app):

        def factory(headers: dict[str, str] | None = None, timeout: Any = None, auth: Any = None) -> httpx.AsyncClient:
            return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), headers=headers, timeout=timeout, auth=auth)

        async def as_(key: str) -> Any:
            async with streamablehttp_client(
                "http://mcp.test/mcp/", headers={"authorization": f"Bearer {key}"}, httpx_client_factory=factory
            ) as (r, wr, _):
                async with ClientSession(r, wr) as s:
                    await s.initialize()
                    return data(await call(s, "build_context", task="Who am I?", subject_ids=[w["sarah"]], profile="planning"))

        a, b = await as_(research.key), await as_(planning.key)
        assert a["agent_id"] == research.id and b["agent_id"] == planning.id  # a stateless server remembers nobody


def test_the_process_has_no_database_or_router_settings_and_imports_nothing_of_the_platform_but_the_sdk():
    assert set(McpSettings.__dataclass_fields__) == {"api_url", "host", "port", "timeout_seconds"}
    assert ENV_FIELDS == ("KG_API_URL", "MCP_HOST", "MCP_PORT", "MCP_TIMEOUT_SECONDS")
    probe = "import sys, kg_mcp.app, kg_mcp.tools, kg_mcp.server; print(sorted(m for m in sys.modules if m.split('.')[0].startswith('kg_') and m.split('.')[0] not in ('kg_mcp', 'kg_platform_sdk')))"
    done = subprocess.run([sys.executable, "-I", "-c", probe], capture_output=True, text=True, env={"PATH": ""})  # noqa: S603
    assert done.returncode == 0, done.stderr[-800:]
    assert done.stdout.strip() == "[]", (
        done.stdout
    )  # no kg_db, kg_governance, kg_memory, ...: it cannot reach the database even by accident
