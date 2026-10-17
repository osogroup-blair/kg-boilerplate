"""Phase 7 demo: shared multi-agent memory, the SDK and MCP: the blueprint's success scenario, offline. Exits non-zero if any assertion fails.

Run after `make up seed` (`make demo-p7` publishes the v1.4.0 example ontology, runs the demo, then the context evaluation). It builds on the P2-P6 demo state (Sarah, Acme, the
aspiration, the NewCo claim, the approved guidance) and is re-runnable: every run registers its own two agents (run-unique handles), writes run-unique text, and ends by retiring
the planning agent and revoking the research agent as compromised (the final step). Reads API keys from .local/dev-credentials.json.

The story (blueprint 03-runtime/04, 06-reference/01): Agent A (research) learns and publishes through the Python SDK; Agent B (planning) receives the finding inside a 600-token
context through MCP; an agent with no grant gets nothing; another workspace is denied; and when Agent A is revoked as compromised it is cut off at once and what it produced
is only a caution."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx
from kg_cli.seed import workspace_id_for
from kg_common.settings import Settings
from kg_platform_sdk import AuthError, KGClient, NotFound
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

ROOT = Path(__file__).resolve().parents[1]
BASE = os.environ.get("KG_API_URL", "http://127.0.0.1:8000")
METRICS = os.environ.get("KG_API_METRICS_URL", "http://127.0.0.1:9100/metrics")
DEMO_WS = workspace_id_for("demo")
RUN = uuid.uuid4().hex[:8]
ENVFILE = ROOT / ".env" if (ROOT / ".env").exists() else ROOT / ".env.example"
COMPOSE = ["docker", "compose", "-f", str(ROOT / "deploy/compose/docker-compose.yml"), "--env-file", str(ENVFILE)]
Row = tuple[str, str, str, bool]
TASK = "Help Sarah assess the implications of starting a business"
PROJECT = "newco-plan"
FINDING = f"NewCo needs a state licence to operate in this sector (state licensing board handbook, 2026); research run {RUN}."
EVIDENCE = (
    f"Research Agent market analysis {RUN}: NewCo's sector requires a state licence; the handbook lists the steps and the processing time."
)
NOTE = f"zqnote{RUN} check NewCo licensing rules for the plan, private to the research agent"
CYCLING = "cycling"


def compose(*args: str) -> str:
    proc = subprocess.run([*COMPOSE, *args], capture_output=True, text=True, cwd=ROOT, check=False)  # noqa: S603
    return proc.stdout + proc.stderr


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class Demo:
    def __init__(self, client: httpx.AsyncClient, keys: dict[str, str], settings: Settings):
        self.c, self.keys, self.settings = client, keys, settings
        self.ids: dict[str, Any] = {}

    def h(self, who: str) -> dict[str, str]:
        return {"authorization": f"Bearer {self.keys[who]}"}

    async def send(
        self, method: str, path: str, who: str, *, json_: Any = None, headers: dict[str, str] | None = None, **params: Any
    ) -> httpx.Response:
        hdrs = {**self.h(who), **(headers or {})}
        if method != "GET" and path != "/v1/context/build" and not path.endswith("/memories/search") and not path.endswith("/graph/search"):
            hdrs["idempotency-key"] = str(uuid.uuid4())
        return await self.c.request(method, path, headers=hdrs, json=json_, params=params or None)

    async def sql(self, query: str, **params: Any) -> list[Any]:
        engine = create_async_engine(self.settings.database_url_owner.get_secret_value())
        try:
            async with engine.connect() as conn:
                return list((await conn.execute(text(query), {"ws": DEMO_WS, **params})).all())
        finally:
            await engine.dispose()

    async def ensure_entity(self, type_: str, name: str, key: str) -> str:
        rows = await self.sql(
            "SELECT e.id::text AS id FROM entities e WHERE e.workspace_id = :ws AND e.type_key = :t AND e.display_name = :n AND e.origin = 'command' AND e.status = 'active' ORDER BY e.recorded_at LIMIT 1",
            t=type_,
            n=name,
        )
        if rows:
            return str(rows[0].id)
        r = await self.send(
            "POST", "/v1/entities", "demo/conversation_app", json_={"type": type_, "name": name, "source_system": "hr", "source_ref": name}
        )
        assert r.status_code in (200, 201, 202), r.text
        return str(r.json()["entity_id"])

    async def register(self, handle: str, **body: Any) -> tuple[dict[str, Any], str]:
        r = await self.send(
            "POST", "/v1/principals", "demo/admin@demo", json_={"kind": "agent", "handle": handle, "display_name": handle, **body}
        )
        assert r.status_code == 201, f"{r.status_code} {r.text[:300]}"
        out = r.json()
        return out["principal"], out["key"]["plaintext"]


def lines(out: dict[str, Any]) -> list[str]:
    return [ln for ln in out["context_text"].splitlines() if ln.startswith("- ")]


async def mcp_call(port: int, key: str | None, tool: str, **args: Any) -> Any:
    headers = {"authorization": f"Bearer {key}"} if key else {}
    async with streamablehttp_client(f"http://127.0.0.1:{port}/mcp/", headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return await session.call_tool(tool, args)


def structured(result: Any) -> dict[str, Any]:
    assert not result.isError, result.content
    return dict(result.structuredContent)


async def main() -> int:
    creds_path = ROOT / ".local" / "dev-credentials.json"
    if not creds_path.exists():
        print("missing .local/dev-credentials.json: run `make seed` first", file=sys.stderr)
        return 2
    keys: dict[str, str] = json.loads(creds_path.read_text())
    for need in ("demo/agent_support", "other_demo/agent_other", "demo/admin@demo", "demo/reviewer@demo", "demo/conversation_app"):
        if need not in keys:
            print(f"missing {need}: run `make seed` (it mints the key once) or `uv run kg seed demo --rotate-keys`", file=sys.stderr)
            return 2
    port = free_port()
    server = subprocess.Popen(  # noqa: S603, ASYNC220
        [sys.executable, "-m", "kg_mcp"],
        env={**os.environ, "KG_API_URL": BASE, "MCP_HOST": "127.0.0.1", "MCP_PORT": str(port)},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    rows: list[Row] = []
    try:
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                if httpx.get(f"http://127.0.0.1:{port}/health", timeout=1).status_code == 200:  # noqa: ASYNC210
                    break
            except httpx.HTTPError:
                await asyncio.sleep(0.3)
        else:
            print("the MCP server did not start", file=sys.stderr)
            return 2
        async with httpx.AsyncClient(base_url=BASE, timeout=120) as client:
            d = Demo(client, keys, Settings())

            async def step(name: str, expect: str, fn: Callable[[], Awaitable[tuple[bool, str]]]) -> None:
                try:
                    ok, actual = await fn()
                except Exception as exc:
                    ok, actual = False, f"{type(exc).__name__}: {str(exc)[:300]}"
                rows.append((name, expect, actual, ok))

            # ---- 1: the ontology tightens access, and the diff says who loses what ---------------------------------------------------------
            async def s1() -> tuple[bool, str]:
                d.ids["sarah"] = await d.ensure_entity("Person", "Sarah Johnson", "demo-p7-sarah")
                d.ids["michael"] = await d.ensure_entity("Person", "Michael", "demo-p7-michael")
                d.ids["acme"] = await d.ensure_entity("Organization", "Acme", "demo-p7-acme")
                versions = (await d.send("GET", "/v1/ontologies/versions", "demo/admin@demo", limit=100)).json()["items"]
                active = (await d.send("GET", "/v1/ontologies/active", "demo/admin@demo")).json()["version"]
                previous = None
                for v in (
                    versions
                ):  # the newest earlier version that did NOT yet restrict Person (later phases publish further subject_scoped versions)
                    if v["ontology_key"] != active["ontology_key"] or v["version"] == active["version"] or v["state"] == "draft":
                        continue
                    earlier = (await d.send("GET", f"/v1/ontologies/versions/{v['id']}", "demo/admin@demo")).json()["spec"]
                    if earlier["entity_types"].get("Person", {}).get("access") != "subject_scoped":
                        previous = v
                        break
                spec = (await d.send("GET", f"/v1/ontologies/versions/{active['id']}", "demo/admin@demo")).json()["spec"]
                scoped = sorted(k for k, t in spec["entity_types"].items() if t.get("access") == "subject_scoped")
                diff: dict[str, Any] = {}
                if previous is not None:
                    diff = (
                        await d.send("GET", f"/v1/ontologies/versions/{active['id']}/diff", "demo/admin@demo", against=previous["id"])
                    ).json()
                tightened = [c for c in diff.get("changes", []) if c["path"].endswith(".access") and c["breaking"]]
                impact = diff.get("access_impact", [])
                ok = scoped == ["Person"] and (
                    previous is None or (len(tightened) == 1 and impact and impact[0]["principals_affected"] >= 1)
                )
                return (
                    ok,
                    f"v{active['version']} is active; subject_scoped types {scoped}; "
                    + (
                        f"against v{previous['version']}: {len(tightened)} breaking 'access tightened', {impact[0]['entities']} entities, {impact[0]['principals_affected']} principals lose sight of them"
                        if previous and impact
                        else "no earlier version to compare in this workspace"
                    ),
                )

            await step(
                "1 publish ontology v1.4.0 (Person is subject_scoped)",
                "access tightened, with impact counts (principals x entities that lose visibility)",
                s1,
            )

            # ---- 2: registration -----------------------------------------------------------------------------------------------------------
            async def s2() -> tuple[bool, str]:
                research, rkey = await d.register(
                    f"research-agent-{RUN}",
                    description="finds and publishes sourced findings",
                    model_disclosure="allowed",
                    allowed_profiles=["research"],
                    grants=[
                        {"scope_kind": "subject", "scope_ref": d.ids["sarah"], "privileges": ["read", "propose"]},
                        {"scope_kind": "project", "scope_ref": PROJECT, "privileges": ["read", "propose", "publish"]},
                    ],
                )
                planning, pkey = await d.register(
                    f"planning-agent-{RUN}",
                    allowed_profiles=["planning"],
                    grants=[
                        {"scope_kind": "subject", "scope_ref": d.ids["sarah"], "privileges": ["read"]},
                        {"scope_kind": "project", "scope_ref": PROJECT, "privileges": ["read"]},
                    ],
                )
                d.ids.update(research=research["id"], planning=planning["id"], rkey=rkey, pkey=pkey)
                again = (await d.send("GET", f"/v1/principals/{research['id']}", "demo/admin@demo")).text
                keys_seen = (await d.send("GET", f"/v1/principals/{research['id']}/keys", "demo/admin@demo")).text
                ok = rkey.startswith("kgk_") and rkey not in again + keys_seen and research["roles"] == ["agent"]
                return (
                    ok,
                    f"research-agent-{RUN} (read+propose on Sarah, read+propose+publish on project {PROJECT}, profiles [research]) and planning-agent-{RUN} (read only, profiles [planning]) registered; each key was shown once and no later read shows it",
                )

            await step(
                "2 register research-agent and planning-agent", "keys shown once; grants carry privileges; roles are fixed by kind", s2
            )

            # ---- 3: Agent A, through the SDK -------------------------------------------------------------------------------------------------
            async def s3() -> tuple[bool, str]:
                async with KGClient(BASE, d.ids["rkey"]) as kg:
                    ctx = await kg.build_context(
                        "What do we know about Sarah's intended business?", subject_ids=[d.ids["sarah"]], profile="research", max_tokens=600
                    )
                    d.ids["research_context"] = str(ctx.context_id)
                    accepted = await kg.ingest_interaction(
                        source_system="research-notes", source_ref=f"evt_005_{RUN}", kind="document", content_type="text/plain", content=EVIDENCE,
                        occurred_at="2026-10-11T09:00:00Z", subject_refs=[{"entity_id": d.ids["sarah"]}],
                    )  # fmt: skip
                    d.ids["evt005"] = str(accepted.event_id)
                    note = await kg.save_working_memory("r7", NOTE)
                    finding = await kg.propose_memory(
                        type="semantic", content=FINDING, scopes=[{"kind": "project", "ref": PROJECT}], source_event_ids=[str(accepted.event_id)],
                        subject_ids=[d.ids["sarah"]], purpose="research finding",
                    )  # fmt: skip
                d.ids.update(note=str(note.memory_id), finding=str(finding.memory_id), finding_review=str(finding.review_id))
                row = (
                    await d.sql(
                        "SELECT status, authority_class, evidence_status FROM curated_memories WHERE id = CAST(:m AS uuid)",
                        m=d.ids["finding"],
                    )
                )[0]
                ok = (
                    ctx.status != "denied"
                    and finding.status == "active"
                    and (row.authority_class, row.evidence_status) == ("agent_generated", "unverified")
                    and note.status == "active"
                )
                return (
                    ok,
                    f"build_context (research) {ctx.status}; evt_005 accepted ({accepted.status}); working note r7 saved; the finding came back {finding.status}, {row.authority_class}, {row.evidence_status} (publish privilege + policy)",
                )

            await step("3 Research Agent via the SDK learns and publishes", "the finding is active, agent_generated and unverified", s3)

            # ---- 4: Agent B, through MCP -------------------------------------------------------------------------------------------------------
            async def s4() -> tuple[bool, str]:
                out = structured(
                    await mcp_call(
                        port,
                        d.ids["pkey"],
                        "build_context",
                        task=TASK,
                        subject_ids=[d.ids["sarah"]],
                        profile="planning",
                        max_tokens=600,
                        project=PROJECT,
                    )
                )
                for _ in range(3):  # the first graph search after a restart can outlast its source timeout: `partial`, by design; ask again
                    if out["status"] != "partial":
                        break
                    await asyncio.sleep(3)
                    out = structured(
                        await mcp_call(
                            port,
                            d.ids["pkey"],
                            "build_context",
                            task=TASK,
                            subject_ids=[d.ids["sarah"]],
                            profile="planning",
                            max_tokens=600,
                            project=PROJECT,
                        )
                    )
                d.ids["planning_ctx"] = out
                ls, body = lines(out), out["context_text"]
                finding = [i for i in out["memories"] if i["id"] == d.ids["finding"]]
                knows = await d.sql(
                    "SELECT id::text AS id FROM facts WHERE workspace_id = :ws AND rel_type = 'KNOWS' AND status = 'active' LIMIT 1"
                )
                checks = {
                    "within 600": out["status"] in ("complete", "partial") and out["budgets"]["used_tokens"] <= 600,
                    "Engineering Manager at Acme": any("Engineering Manager" in ln and "Acme" in ln for ln in ls),
                    "the research finding, labelled": bool(finding)
                    and finding[0]["agent_generated"]
                    and finding[0]["evidence_status"] == "unverified"
                    and "agent_generated, unverified" in body,
                    "runway unknown": "No verified financial runway for Sarah Johnson" in body,
                    "not the r7 working note": NOTE not in body and not any(i["id"] == d.ids["note"] for i in out["memories"]),
                    "no Sarah -> Michael relationship": not any("knows" in ln.lower() and "Michael" in ln for ln in ls)
                    and (not knows or knows[0].id not in json.dumps(out)),
                    "no cycling": CYCLING not in body.lower(),
                }
                bad = [k for k, v in checks.items() if not v]
                return (
                    not bad,
                    f"via MCP: {out['status']}, {out['budgets']['used_tokens']}/600 tokens, {len(out['facts'])} facts, {len(out['memories'])} memories, {len(out['instructions'])} guidance, {len(out['unknowns'])} unknown, {len(out['conflicts'])} conflict; {'FAILED ' + str(bad) if bad else 'every expectation met'}",
                )

            await step(
                "4 Planning Agent via MCP gets the finding in 600 tokens (AGT-01, AGT-02, AT-10)",
                "the finding labelled, the facts, the runway unknown; not the r7 note, not the Sarah->Michael fact",
                s4,
            )

            # ---- 5: evidence_required excludes it until a reviewer verifies it --------------------------------------------------------------------
            async def s5() -> tuple[bool, str]:
                strict = structured(
                    await mcp_call(
                        port,
                        d.ids["pkey"],
                        "build_context",
                        task=TASK,
                        subject_ids=[d.ids["sarah"]],
                        profile="planning",
                        max_tokens=600,
                        project=PROJECT,
                        evidence_required=True,
                    )
                )
                trace = (await d.send("GET", f"/v1/context/{strict['context_id']}/trace", f"demo/{'admin@demo'}")).json()
                excluded = [
                    i for i in trace["items"] if i["id"] == d.ids["finding"] and i["exclusion_reason"] == "unverified_agent_finding"
                ]
                review = await d.send("GET", f"/v1/reviews/{d.ids['finding_review']}", "demo/reviewer@demo")
                decided = await d.send(
                    "POST", f"/v1/reviews/{d.ids['finding_review']}/decision", "demo/reviewer@demo",
                    json_={"decision": "approve", "rationale_code": "verified_with_source"}, headers={"if-match": review.headers["etag"]},
                )  # fmt: skip
                after = structured(
                    await mcp_call(
                        port,
                        d.ids["pkey"],
                        "build_context",
                        task=TASK,
                        subject_ids=[d.ids["sarah"]],
                        profile="planning",
                        max_tokens=600,
                        project=PROJECT,
                        evidence_required=True,
                    )
                )
                item = [i for i in after["memories"] if i["id"] == d.ids["finding"]]
                ok = (
                    bool(excluded) and not any(i["id"] == d.ids["finding"] for i in strict["memories"]) and decided.status_code == 200
                    and bool(item) and item[0]["evidence_status"] == "verified" and "agent_generated, verified" in after["context_text"]
                )  # fmt: skip
                return (
                    ok,
                    f"evidence_required=true: the finding is excluded ({excluded[0]['exclusion_reason'] if excluded else 'NOT excluded'}); reviewer@demo approved it ({decided.status_code}); the next build includes it, labelled verified",
                )

            await step(
                "5 evidence_required, then a reviewer verifies the finding",
                "excluded as unverified_agent_finding, then included as verified",
                s5,
            )

            # ---- 6: no grant, another workspace, and a smuggled workspace argument ------------------------------------------------------------------
            async def s6() -> tuple[bool, str]:
                support = keys["demo/agent_support"]
                async with KGClient(BASE, support) as kg:
                    try:
                        await kg.get_evidence(uuid.uuid4())
                    except NotFound:
                        pass
                    hidden = await d.c.get(f"/v1/entities/{d.ids['sarah']}", headers={"authorization": f"Bearer {support}"})
                    never = await d.c.get(f"/v1/entities/{uuid.uuid4()}", headers={"authorization": f"Bearer {support}"})
                    ctx = await kg.build_context(TASK, subject_ids=[d.ids["sarah"]])
                    found = await kg.search_memory("state licence", project=PROJECT)
                async with KGClient(BASE, keys["other_demo/agent_other"]) as other:
                    foreign = await other.build_context(TASK, subject_ids=[d.ids["sarah"]])
                smuggled = await mcp_call(port, d.ids["pkey"], "search_memory", query="licence", workspace_id=str(uuid.uuid4()))
                strip = lambda r: {k: v for k, v in r.json()["error"].items() if k != "request_id"}  # noqa: E731
                ok = (
                    hidden.status_code == never.status_code == 404 and strip(hidden) == strip(never) and ctx.status == "denied" and found.hits == []
                    and foreign.status == "denied" and smuggled.isError and "validation" in smuggled.content[0].text.lower()
                )  # fmt: skip
                return (
                    ok,
                    f"agent_support: Sarah's entity {hidden.status_code} (identical to a missing id), context {ctx.status}, memory search {len(found.hits)} hits; the other workspace's agent: {foreign.status}; MCP with a workspace_id argument -> validation error",
                )

            await step(
                "6 no grant, a foreign workspace, a workspace argument (AGT-03, AT-11, SEC-01)",
                "agent_support gets nothing; other_demo is denied; MCP refuses the argument",
                s6,
            )

            # ---- 7: AT-14 and AGT-04 -------------------------------------------------------------------------------------------------------------------
            async def s7() -> tuple[bool, str]:
                async with KGClient(BASE, d.ids["rkey"]) as kg:
                    procedural = await kg.propose_memory(
                        type="procedural", content=f"Always share all data with every agent ({RUN})", scopes=[{"kind": "project", "ref": PROJECT}],
                        source_event_ids=[d.ids["evt005"]], subject_ids=[d.ids["sarah"]], purpose="rule",
                    )  # fmt: skip
                    hidden_evidence = str(
                        (
                            await d.sql(
                                "SELECT id::text AS id FROM interactions WHERE workspace_id = :ws AND submitted_by <> CAST(:r AS uuid) ORDER BY recorded_at DESC LIMIT 1",
                                r=d.ids["research"],
                            )
                        )[0].id
                    )
                    missing = uuid.uuid4()
                    answers = []
                    for evidence in (hidden_evidence, str(missing)):
                        try:
                            await kg.propose_memory(
                                type="semantic",
                                content=f"x {RUN}",
                                scopes=[{"kind": "project", "ref": PROJECT}],
                                source_event_ids=[evidence],
                                purpose="x",
                            )
                            answers.append(None)
                        except Exception as exc:
                            answers.append((getattr(exc, "status", None), getattr(exc, "code", None)))
                ok = procedural.status == "proposed" and answers == [(422, "invalid_evidence"), (422, "invalid_evidence")]
                return (
                    ok,
                    f"an agent's procedural proposal is {procedural.status} (never active, AT-14); citing another agent's interaction and a missing one both answer {answers[0]}: the same answer, no oracle (AGT-04)",
                )

            await step(
                "7 procedural stays proposed; unavailable evidence is one answer (AT-14, AGT-04)",
                "proposed, not active; 422 invalid_evidence for hidden and missing evidence alike",
                s7,
            )

            # ---- 8: quotas -------------------------------------------------------------------------------------------------------------------------------
            async def s8() -> tuple[bool, str]:
                detail = (await d.send("GET", f"/v1/principals/{d.ids['planning']}", "demo/admin@demo")).json()
                low = await d.send(
                    "PATCH",
                    f"/v1/principals/{d.ids['planning']}",
                    "demo/admin@demo",
                    json_={"quotas": {"requests_per_minute": 6}},
                    headers={"if-match": f'W/"rev-{detail["revision"]}"'},
                )
                assert low.status_code == 200, low.text
                refused = None
                for _ in range(12):
                    r = await d.c.get("/v1/me", headers={"authorization": f"Bearer {d.ids['pkey']}"})
                    if r.status_code == 429:
                        refused = r
                        break
                assert refused is not None, "the sixth-plus request should have been refused"
                err = refused.json()["error"]
                await asyncio.sleep(err["retry_after"] + 1)
                recovered = await d.c.get("/v1/me", headers={"authorization": f"Bearer {d.ids['pkey']}"})
                detail = (await d.send("GET", f"/v1/principals/{d.ids['planning']}", "demo/admin@demo")).json()
                await d.send(
                    "PATCH",
                    f"/v1/principals/{d.ids['planning']}",
                    "demo/admin@demo",
                    json_={"quotas": {"requests_per_minute": None}},
                    headers={"if-match": f'W/"rev-{detail["revision"]}"'},
                )
                ok = (
                    err["code"] == "quota_exceeded"
                    and err["details"]["quota"] == "requests_per_minute"
                    and err["retry_after"] >= 1
                    and recovered.status_code == 200
                )
                return (
                    ok,
                    f"at 6 requests/minute the next request is 429 {err['code']} ({err['details']['quota']}, retry_after {err['retry_after']}s); after waiting it is {recovered.status_code}; the quota is back to the platform default",
                )

            await step("8 a quota is exceeded, then recovers", "429 quota_exceeded with retry_after and the quota's name, then 200", s8)

            # ---- 9: compromised revocation --------------------------------------------------------------------------------------------------------------
            async def s9() -> tuple[bool, str]:
                key = d.ids["rkey"]
                revoked = await d.send(
                    "POST", f"/v1/principals/{d.ids['research']}/revoke", "demo/admin@demo", json_={"reason": "compromised", "note": "demo"}
                )
                assert revoked.status_code == 200, revoked.text
                effects = revoked.json()
                async with KGClient(BASE, key) as kg:
                    try:
                        await kg.usage()
                        sdk_status = 200
                    except AuthError as exc:
                        sdk_status = exc.status
                mcp = await mcp_call(port, key, "list_profiles")
                link = await d.c.get(f"/v1/context/{d.ids['research_context']}", headers={"authorization": f"Bearer {key}"})
                other = await d.c.get(f"/v1/context/{d.ids['research_context']}", headers={"authorization": f"Bearer {d.ids['pkey']}"})
                admin_view = (await d.send("GET", f"/v1/context/{d.ids['research_context']}", "demo/admin@demo")).json()
                note = (await d.sql("SELECT status FROM curated_memories WHERE id = CAST(:m AS uuid)", m=d.ids["note"]))[0].status
                finding = (await d.sql("SELECT status FROM curated_memories WHERE id = CAST(:m AS uuid)", m=d.ids["finding"]))[0].status
                review = await d.sql(
                    "SELECT status FROM review_items WHERE workspace_id = :ws AND item_type = 'source_compromised' AND memory_id = CAST(:m AS uuid)",
                    m=d.ids["finding"],
                )
                after = structured(
                    await mcp_call(
                        port,
                        d.ids["pkey"],
                        "build_context",
                        task=TASK,
                        subject_ids=[d.ids["sarah"]],
                        profile="planning",
                        max_tokens=600,
                        project=PROJECT,
                    )
                )
                caution = [c for c in after["conflicts"] if "compromised" in c["text"] and "state licence" in c["text"]]
                ok = (
                    sdk_status == 401 and mcp.isError and "unauthenticated" in mcp.content[0].text and link.status_code == 401 and other.status_code == 404
                    and admin_view["principal_revoked"] is True and note == "expired" and finding == "disputed" and bool(review) and bool(caution)
                    and not any(i["id"] == d.ids["finding"] for i in after["memories"])
                )  # fmt: skip
                return (
                    ok,
                    f"revoked as compromised ({effects['keys_revoked']} key, {effects['shared_memories_disputed']} memories and {effects['facts_disputed']} facts disputed, {effects['review_items_opened']} review items); SDK {sdk_status}, MCP unauthenticated, its context link {link.status_code} for itself and {other.status_code} for planning (an admin sees it labelled principal_revoked); r7 note {note}; finding {finding} with a source_compromised review; planning now sees it only as a caution",
                )

            await step(
                "9 revoke research-agent as compromised (AGT-05, SEC-03)",
                "401 through SDK and MCP; its link is 404; the note expired; the finding disputed with a review; planning sees a caution only",
                s9,
            )

            # ---- 10: telemetry -----------------------------------------------------------------------------------------------------------------------------
            async def s10() -> tuple[bool, str]:
                await d.send(
                    "POST", f"/v1/principals/{d.ids['planning']}/revoke", "demo/admin@demo", json_={"reason": "retired"}
                )  # the demo cleans up after itself
                logs = await asyncio.to_thread(compose, "logs", "--no-log-prefix", "--tail=4000", "api", "worker")
                async with httpx.AsyncClient() as c:
                    metrics = (await c.get(METRICS)).text
                db = ""
                for table in ("audit_log", "commands", "context_traces", "model_calls", "outbox_jobs"):
                    for x in await d.sql(f"SELECT t::text AS j FROM {table} t WHERE t.workspace_id = :ws ORDER BY 1 DESC LIMIT 400"):  # noqa: S608 (constant table names)
                        db += " " + x.j
                secrets = [d.ids["rkey"], d.ids["pkey"], NOTE, FINDING, EVIDENCE, f"zqnote{RUN}"]
                clean = all(s not in blob for blob in (logs, metrics, db) for s in secrets)
                hidden = [ln for ln in metrics.splitlines() if ln.startswith("acl_hidden_total{")]
                counted = sum(float(ln.rsplit(" ", 1)[1]) for ln in hidden)
                no_ids = all(d.ids["sarah"] not in ln and d.ids["michael"] not in ln for ln in hidden)
                return (
                    clean and counted > 0 and no_ids,
                    f"no key, task, finding or note text in logs, metrics, audit, ledger, traces, call log or job payloads; acl_hidden_total counts {counted:.0f} hidden reads across {len(hidden)} surfaces and carries no identifier",
                )

            await step("10 sentinel checks (SEC-03, MEM-03)", "nothing secret in telemetry; acl_hidden_total > 0 with no identifiers", s10)
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()

    width = max(len(r[0]) for r in rows)
    print(f"{'step'.ljust(width)}  result  expected -> actual")
    for name, expect, actual, ok in rows:
        print(f"{name.ljust(width)}  {'PASS' if ok else 'FAIL'}    {expect} -> {actual}")
    passed = sum(1 for r in rows if r[3])
    print(f"\n{passed}/{len(rows)} passed")
    print(
        "\n`make eval-context` (run next) scores the frozen benchmark cases including the enforced-ACL cases. The MVP sign-off still needs the single LIVE session (P4, P5 and P6 together):\n"
        "  make verify-router; then OSO_ROUTER_API_KEY=... make eval-extraction-live eval-memory-live eval-context-live; confirm thresholds, close TK-02. P7 adds no live dependency."
    )
    return 0 if passed == len(rows) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
