"""Phase 5 demo: curated memory as a first-class, governed store. Exits non-zero if any assertion fails.

Run after `make up seed` (`make demo-p5` publishes the v1.2.0 example ontology, runs the demo, then the memory evaluation). Re-runnable: every write carries a fixed
Idempotency-Key (a re-run replays it and the assertions read the state) except the working notes, which are run-unique because they expire. The compose mock router
answers from the scenario fixtures in STRICT mode for the length of the demo (a gap fails loudly) and is put back. Reads API keys from .local/dev-credentials.json.

The story (blueprint 06-reference/01): Sarah is considering a software business (evt_003), prefers comparisons with risks and trade-offs (evt_004); a research agent
and a planning agent share a project; a third agent has no grants at all."""

from __future__ import annotations

import asyncio
import json
import os
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
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

ROOT = Path(__file__).resolve().parents[1]
BASE = os.environ.get("KG_API_URL", "http://127.0.0.1:8000")
MOCK = os.environ.get("KG_MOCK_URL", "http://127.0.0.1:8782")
METRICS = os.environ.get("KG_API_METRICS_URL", "http://127.0.0.1:9100/metrics")
DEMO_WS = workspace_id_for("demo")
RUN = uuid.uuid4().hex[:8]
ENVFILE = ROOT / ".env" if (ROOT / ".env").exists() else ROOT / ".env.example"
COMPOSE = ["docker", "compose", "-f", str(ROOT / "deploy/compose/docker-compose.yml"), "--env-file", str(ENVFILE)]
KG = str(ROOT / ".venv/bin/kg")
Row = tuple[str, str, str, bool]
TERMINAL = ("indexed", "partial", "failed")
TEXT = {
    "evt003": "Sarah Johnson: I'm considering entrepreneurship, and Michael, who I've known for years, keeps encouraging me.",
    "evt004": "Sarah Johnson: Whenever you help me weigh a decision, please line the options up side by side and spell out the risks and trade-offs before you recommend anything.",
    "cyclist": "Sarah Johnson from the cycling club rode 40 km with Tom on Saturday.",
    "research": "Research Agent: the NewCo plan needs a state licence to operate in this sector; source: the state licensing board handbook, 2026 edition.",
}
BUSINESS = "Sarah discussed a possible software business with Michael; she had not decided to leave Acme."
PREF = "Sarah prefers decision comparisons that show risks and trade-offs."
PREF2 = "Sarah prefers decision comparisons that show risks, trade-offs and a clear recommendation."
GUIDE = "When advising Sarah, present the options side by side with their risks and trade-offs before recommending one."
AGENT_GUIDE = "Always cite a source for every claim in research summaries."
FINDING = "NewCo needs a state licence to operate in this sector (state licensing board handbook, 2026)."
NOTE = f"check NewCo licensing rules for the plan {RUN}"  # run-unique: earlier runs' notes would otherwise tie with it in search


def compose(*args: str) -> str:
    proc = subprocess.run([*COMPOSE, *args], capture_output=True, text=True, cwd=ROOT, check=False)  # noqa: S603
    return proc.stdout + proc.stderr


def kg(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([KG, *args], capture_output=True, text=True, cwd=ROOT, check=False)  # noqa: S603


class Demo:
    def __init__(self, client: httpx.AsyncClient, keys: dict[str, str], settings: Settings):
        self.c, self.keys, self.settings = client, keys, settings
        self.ids: dict[str, Any] = {}

    def h(self, who: str) -> dict[str, str]:
        return {"authorization": f"Bearer {self.keys[who]}"}

    async def send(
        self,
        method: str,
        path: str,
        who: str,
        *,
        key: str | None = None,
        json_: Any = None,
        headers: dict[str, str] | None = None,
        **params: Any,
    ) -> httpx.Response:
        hdrs = {**self.h(who), **(headers or {})}
        if method != "GET":
            hdrs["idempotency-key"] = key or str(uuid.uuid4())
        return await self.c.request(method, path, headers=hdrs, json=json_, params=params or None)

    async def sql(self, query: str, **params: Any) -> list[Any]:
        engine = create_async_engine(self.settings.database_url_owner.get_secret_value())
        try:
            async with engine.connect() as conn:
                return list((await conn.execute(text(query), {"ws": DEMO_WS, **params})).all())
        finally:
            await engine.dispose()

    async def mock(self, method: str, path: str, **kw: Any) -> None:
        async with httpx.AsyncClient() as c:
            (await c.request(method, f"{MOCK}{path}", **kw)).raise_for_status()

    async def ingest(
        self, key: str, content: str, *, subjects: list[str] | None = None, who: str = "demo/conversation_app", **over: Any
    ) -> str:
        body: dict[str, Any] = {
            "source_system": "conversation_app" if "conversation" in who else "research_agent",
            "source_ref": f"chat-p5-{key}",
            "kind": "message",
            "occurred_at": "2026-10-10T09:00:00Z",
            "content": content,
            **over,
        }
        if subjects:
            body["subject_refs"] = [{"entity_id": s} for s in subjects]
        r = await self.send("POST", "/v1/interactions", who, key=key, json_=body)
        assert r.status_code in (200, 202), f"ingest {key}: {r.status_code} {r.text[:200]}"
        return str(r.json()["event_id"])

    async def wait(self, event_id: str, *, timeout: float = 150, heal: bool = True) -> dict[str, Any]:  # noqa: ASYNC109
        """Waits until nothing of the event is in flight. A stage that died earlier (a re-run after a fixture was missing, say) is replayed ONCE, audited."""
        deadline = time.monotonic() + timeout
        healed = False
        while time.monotonic() < deadline:
            ev = (await self.send("GET", f"/v1/interactions/{event_id}", "demo/admin@demo")).json()
            if ev.get("processing_status") in TERMINAL:  # `partial` includes a stage blocked behind a dead one
                if heal and not healed and any(s["status"] == "dead" for s in ev["stages"]):
                    healed = True
                    proc = kg(
                        "interactions", "replay", event_id, "--workspace", "demo", "--actor", "admin@demo", "--reason", "source_fixed"
                    )
                    assert proc.returncode == 0, proc.stdout + proc.stderr
                    await asyncio.sleep(1)
                    continue
                return ev  # type: ignore[no-any-return]
            await asyncio.sleep(0.5)
        raise AssertionError(f"event {event_id} did not finish in {timeout}s")

    async def ensure_entity(self, type_: str, name: str, ext: str | None, key: str) -> str:
        rows = await self.sql(
            "SELECT e.id::text AS id FROM entities e WHERE e.workspace_id = :ws AND e.type_key = :t AND e.display_name = :n AND e.origin = 'command' AND e.status = 'active' ORDER BY e.recorded_at LIMIT 1",
            t=type_,
            n=name,
        )
        if rows:
            return str(rows[0].id)
        body: dict[str, Any] = {"type": type_, "name": name, "source_system": "hr", "source_ref": ext or name}
        if ext:
            body["external_ids"] = [{"system": "hr", "value": ext}]
        r = await self.send("POST", "/v1/entities", "demo/conversation_app", key=key, json_=body)
        assert r.status_code in (200, 201, 202), r.text
        return str(r.json()["entity_id"])

    async def search(self, who: str, query: str, **extra: Any) -> dict[str, Any]:
        r = await self.send("POST", "/v1/memories/search", who, json_={"query": query, **extra})
        assert r.status_code == 200, r.text
        return r.json()  # type: ignore[no-any-return]

    async def embedded(self, *ids: str, timeout: float = 90) -> None:  # noqa: ASYNC109
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            rows = await self.sql(
                "SELECT count(*) AS n FROM curated_memories WHERE id = ANY(CAST(:ids AS uuid[])) AND embedding_status = 'pending'",
                ids=list(ids),
            )
            if rows[0].n == 0:
                return
            await asyncio.sleep(0.5)
        raise AssertionError("memories were not embedded in time")

    async def memory_of(self, event_id: str) -> list[Any]:
        """The memories the pipeline (or an app citing the same event with the same words) made from an event: a re-run in a dirty workspace may find the proposal first."""
        return await self.sql(
            "SELECT m.id::text AS id, m.type, m.status, m.evidence_status, m.content, m.authority_class FROM curated_memories m JOIN memory_evidence e ON e.memory_id = m.id "
            "WHERE m.workspace_id = :ws AND e.source_kind = 'interaction' AND e.interaction_id = :i AND m.origin IN ('extracted', 'proposal') ORDER BY m.recorded_at, m.id",
            i=uuid.UUID(event_id),
        )

    async def decide(self, review_id: str, decision: str, who: str = "demo/reviewer@demo") -> httpx.Response:
        got = await self.send("GET", f"/v1/reviews/{review_id}", who)
        return await self.send(
            "POST",
            f"/v1/reviews/{review_id}/decision",
            who,
            json_={"decision": decision, "rationale_code": "verified_with_source"},
            headers={"if-match": got.headers["etag"]},
        )

    async def open_review(self, memory_id: str) -> str | None:
        rows = await self.sql(
            "SELECT id::text AS id FROM review_items WHERE workspace_id = :ws AND memory_id = CAST(:m AS uuid) AND status = 'open' LIMIT 1",
            m=memory_id,
        )
        return str(rows[0].id) if rows else None


def stage(d: dict[str, Any], name: str) -> dict[str, Any]:
    return next(s for s in d["stages"] if s["stage"] == name)


async def main() -> int:
    creds_path = ROOT / ".local" / "dev-credentials.json"
    if not creds_path.exists():
        print("missing .local/dev-credentials.json: run `make seed` first", file=sys.stderr)
        return 2
    keys: dict[str, str] = json.loads(creds_path.read_text())
    if "demo/agent_support" not in keys:
        print("missing demo/agent_support: run `make seed` (it mints the key once) or `uv run kg seed demo --rotate-keys`", file=sys.stderr)
        return 2
    rows: list[Row] = []
    async with httpx.AsyncClient(base_url=BASE, timeout=90) as client:
        d = Demo(client, keys, Settings())

        async def step(name: str, expect: str, fn: Callable[[], Awaitable[tuple[bool, str]]]) -> None:
            try:
                ok, actual = await fn()
            except Exception as exc:
                ok, actual = False, f"{type(exc).__name__}: {str(exc)[:240]}"
            rows.append((name, expect, actual, ok))

        await d.mock("POST", "/__mock/strict", json={"enabled": True})
        await d.mock("POST", "/__mock/reload")
        try:
            # ---- 1: ontology v1.2.0 and the grants ---------------------------------------------------------------------------------------
            async def s1() -> tuple[bool, str]:
                d.ids["sarah"] = await d.ensure_entity("Person", "Sarah Johnson", "E-1001", "demo-p5-sarah")
                d.ids["michael"] = await d.ensure_entity("Person", "Michael", None, "demo-p5-michael")
                d.ids["topic"] = await d.ensure_entity("Topic", "Entrepreneurship", None, "demo-p5-topic")
                active = (await d.send("GET", "/v1/ontologies/active", "demo/admin@demo")).json()
                spec = (await d.send("GET", f"/v1/ontologies/versions/{active['version']['id']}", "demo/admin@demo")).json()["spec"]
                policy = spec["memory_policies"]["default"]
                made = []
                for agent in ("agent_research", "agent_planning"):
                    proc = kg(
                        "scopes",
                        "grant",
                        "--workspace",
                        "demo",
                        "--principal",
                        agent,
                        "--kind",
                        "subject",
                        "--ref",
                        d.ids["sarah"],
                        "--actor",
                        "admin@demo",
                    )
                    assert proc.returncode == 0, proc.stderr
                    made.append(f"{agent}: {proc.stdout.strip()}")
                grants = (await d.send("GET", "/v1/scopes", "demo/admin@demo")).json()
                support = [
                    g
                    for g in grants
                    if g["principal_id"]
                    == (await d.sql("SELECT id::text AS id FROM principals WHERE workspace_id = :ws AND handle = 'agent_support'"))[0].id
                ]
                ok = policy["extraction"] == "enabled" and policy["auto_activate"] == ["episodic", "semantic"] and support == []
                return (
                    ok,
                    f"active ontology {active['version']['version']} with the typed memory policy (extraction {policy['extraction']}, auto_activate {policy['auto_activate']}); grants: {'; '.join(made)}; agent_support has {len(support)} grants",
                )

            await step(
                "1 typed memory policy and grants",
                "the typed memory policy (example ontology v1.2.0, or the next free minor) is active; the two agents may see Sarah and the project; agent_support has no grants",
                s1,
            )

            # ---- 2: evt_003 -> memory_propose (extracted) --------------------------------------------------------------------------------
            async def s2() -> tuple[bool, str]:
                d.ids["evt003"] = await d.ingest(  # the SAME request demo-p4 made, so this is a replay of that event
                    "demo-p4-evt-003",
                    TEXT["evt003"],
                    subjects=[d.ids["sarah"], d.ids["michael"], d.ids["topic"]],
                    source_ref="chat-p4-demo-p4-evt-003",
                )
                ev = await d.wait(d.ids["evt003"])
                upgraded = "already enabled"
                status = stage(ev, "memory_propose")["status"]
                if status in ("skipped_disabled", "skipped_by_policy"):
                    # accepted under p4.v1 (stage disabled then) or while memory extraction was off (demo-p2..p4 seed with --no-memory-extraction): ask for the stage, audited
                    flags = ["--upgrade-pipeline"] if status == "skipped_disabled" else ["--stage", "memory_propose", "--repin"]
                    proc = kg(
                        "interactions",
                        "replay",
                        d.ids["evt003"],
                        "--workspace",
                        "demo",
                        "--actor",
                        "admin@demo",
                        "--reason",
                        "enable_memory",
                        *flags,
                    )
                    assert proc.returncode == 0, proc.stdout + proc.stderr
                    ev = await d.wait(d.ids["evt003"])
                    upgraded = "enabled by an audited replay (" + " ".join(flags) + ")"
                mp = stage(ev, "memory_propose")
                (m,) = await d.memory_of(d.ids["evt003"])
                full = (await d.send("GET", f"/v1/memories/{m.id}", "demo/admin@demo")).json()
                kinds = sorted(e["source_kind"] for e in full["evidence"])
                fact_rel = (
                    await d.sql(
                        "SELECT rel_type FROM facts WHERE id = CAST(:f AS uuid)",
                        f=[e["fact_id"] for e in full["evidence"] if e["source_kind"] == "fact"][0],
                    )
                )[0].rel_type
                ok = (
                    mp["status"] == "succeeded"
                    and (m.type, m.status, m.evidence_status, m.authority_class)
                    == ("episodic", "active", "unverified", "conversation_report")
                    and m.content == BUSINESS
                    and full["scopes"] == [{"kind": "subject", "ref": d.ids["sarah"]}]
                    and kinds == ["episode", "fact", "interaction"]
                    and fact_rel == "CONSIDERING"
                )
                d.ids["business"] = m.id
                return (
                    ok,
                    f"evt_003 {upgraded}: one episodic memory, active/unverified, scope subject:Sarah, evidence {kinds} (the {fact_rel} fact), model calls {mp['result']['chat_calls']}",
                )

            await step(
                "2 evt_003 yields an episodic memory",
                "exactly one episodic memory linked to the interaction, the episode and the CONSIDERING fact",
                s2,
            )

            # ---- 3: evt_004: the extracted guideline waits, the explicit preference is stored separately -------------------------------------
            async def s3() -> tuple[bool, str]:
                d.ids["evt004"] = await d.ingest("demo-p5-evt-004", TEXT["evt004"], subjects=[d.ids["sarah"]])
                ev = await d.wait(d.ids["evt004"])
                (guide,) = await d.memory_of(d.ids["evt004"])
                r = await d.send(
                    "POST",
                    "/v1/memories",
                    "demo/conversation_app",
                    key="demo-p5-pref",
                    json_={
                        "type": "semantic",
                        "content": PREF,
                        "subject_ids": [d.ids["sarah"]],
                        "source_interaction_id": d.ids["evt004"],
                        "purpose": "advice style",
                    },
                )
                assert r.status_code == 201, r.text
                pref = (await d.send("GET", f"/v1/memories/{r.json()['memory_id']}", "demo/admin@demo")).json()
                d.ids["pref"], d.ids["guide"] = pref["id"], guide.id
                ok = (
                    stage(ev, "memory_propose")["status"] == "succeeded"
                    and (guide.type, guide.content) == ("procedural", GUIDE)
                    and guide.status in ("proposed", "active")
                    and (pref["type"], pref["evidence_status"], pref["authority_class"]) == ("semantic", "verified", "user_asserted")
                    and pref["status"] in ("active", "superseded")  # superseded by step 9 of an earlier run
                    and pref["purpose"] == "advice style"
                    and [e["interaction_id"] for e in pref["evidence"]] == [d.ids["evt004"]]
                )
                return (
                    ok,
                    f"extraction proposed the procedural guideline ({guide.status}); conversation_app saved the semantic preference explicitly: {pref['status']}/{pref['evidence_status']} with its source and purpose",
                )

            await step(
                "3 explicit preference and extracted guideline",
                "the preference is active+verified with attribution; the guideline from evt_004 waits as a procedural proposal",
                s3,
            )

            # ---- 4: procedural memory needs review; an agent can never activate it ----------------------------------------------------------
            async def s4() -> tuple[bool, str]:
                d.ids["research_event"] = await d.ingest("demo-p5-research", TEXT["research"], who="demo/agent_research")
                await d.wait(d.ids["research_event"])
                dup = await d.send(
                    "POST",
                    "/v1/memories/proposals",
                    "demo/conversation_app",
                    key="demo-p5-guide-proposal",
                    json_={
                        "type": "procedural",
                        "content": GUIDE,
                        "subject_ids": [d.ids["sarah"]],
                        "requested_scope": {"kind": "subject", "ref": d.ids["sarah"]},
                        "source_event_ids": [d.ids["evt004"]],
                    },
                )
                review = await d.open_review(d.ids["guide"])
                before = (await d.sql("SELECT status FROM curated_memories WHERE id = CAST(:m AS uuid)", m=d.ids["guide"]))[0].status
                if review:
                    assert (await d.decide(review, "approve")).status_code == 200
                after = (await d.sql("SELECT status FROM curated_memories WHERE id = CAST(:m AS uuid)", m=d.ids["guide"]))[0].status
                agent = await d.send(
                    "POST",
                    "/v1/memories/proposals",
                    "demo/agent_research",
                    key="demo-p5-agent-guide",
                    json_={
                        "type": "procedural",
                        "content": AGENT_GUIDE,
                        "requested_scope": {"kind": "workspace"},
                        "source_event_ids": [d.ids["research_event"]],
                    },
                )
                a_review = agent.json().get("review_id")
                denied = await d.send(
                    "POST",
                    f"/v1/reviews/{a_review}/decision",
                    "demo/agent_research",
                    json_={"decision": "approve", "rationale_code": "other"},
                    headers={"if-match": 'W/"rev-1"'},
                )
                still = (await d.sql("SELECT status FROM curated_memories WHERE id = CAST(:m AS uuid)", m=agent.json()["memory_id"]))[
                    0
                ].status
                ok = (
                    dup.status_code in (200, 202)
                    and after == "active"
                    and agent.status_code in (200, 202)
                    and agent.json()["status"] == "proposed"
                    and denied.status_code == 403
                    and still == "proposed"
                )
                return (
                    ok,
                    f"app re-proposed the guideline -> {dup.status_code} (same content: it reinforces, no second memory); reviewer approved it ({before} -> {after}); agent_research's procedural proposal -> {agent.json()['status']}, its own approval attempt -> {denied.status_code}, still {still} (AT-14)",
                )

            await step(
                "4 procedural memory needs a person",
                "the guideline becomes active only through the reviewer; an agent's procedural proposal stays proposed and the agent cannot approve it",
                s4,
            )

            # ---- 5: working memory is private to one agent and one run ----------------------------------------------------------------------
            async def s5() -> tuple[bool, str]:
                run = f"r1-{RUN}"
                r = await d.send(
                    "POST",
                    "/v1/memories",
                    "demo/agent_research",
                    key=f"demo-p5-note-{RUN}",
                    json_={"type": "working", "content": NOTE, "run_id": run},
                )
                assert r.status_code == 201, r.text
                d.ids["note"], d.ids["run"] = r.json()["memory_id"], run
                await d.embedded(d.ids["note"])
                own = await d.search("demo/agent_research", f"NewCo licensing {RUN}", run_id=run)
                other_run = await d.search("demo/agent_research", f"NewCo licensing {RUN}", run_id=f"r2-{RUN}")
                all_runs = await d.search("demo/agent_research", f"NewCo licensing {RUN}", all_runs=True)
                peers = [
                    await d.search(w, f"NewCo licensing {RUN}", run_id=run, all_runs=True)
                    for w in ("demo/agent_planning", "demo/conversation_app", "demo/reviewer@demo")
                ]
                gets = [
                    (await d.send("GET", f"/v1/memories/{d.ids['note']}", w)).status_code
                    for w in ("demo/agent_planning", "demo/conversation_app", "demo/reviewer@demo", "demo/admin@demo")
                ]
                revealed = await d.send("GET", f"/v1/memories/{d.ids['note']}", "demo/admin@demo", reveal_private="true")
                audit = (await d.send("GET", "/v1/audit", "demo/admin@demo", action="memory.private_read", limit=5)).json()["items"]
                ids_of = lambda out: [h["memory_id"] for h in out["hits"]]  # noqa: E731
                note = d.ids["note"]
                checks = {
                    "its agent finds it in its run": note in ids_of(own),
                    "not in another run": note not in ids_of(other_run),
                    "all_runs finds it": note
                    in ids_of(all_runs),  # (other shared memories may match the words too, and earlier runs' notes)
                    "peers do not find it": all(note not in ids_of(p) for p in peers),
                    "others get 404": gets == [404, 404, 404, 404],
                    "the explicit reveal works": revealed.status_code == 200,
                    "and is audited": any(a["resource_id"] == note for a in audit),
                }
                ok = all(checks.values())
                if not ok:
                    return (
                        False,
                        f"FAILED {[k for k, v in checks.items() if not v]}; gets {gets}; audit {[a['resource_id'] for a in audit]}",
                    )
                return (
                    ok,
                    f"the note is found by its agent in {run} only; peers, the app, the reviewer and (without asking) the admin get 404; the admin's explicit reveal -> {revealed.status_code} and is audited",
                )

            await step(
                "5 working memory is isolated (MEM-02)",
                "visible to its agent in its run; invisible to everyone else; an admin sees it only by asking, audited",
                s5,
            )

            # ---- 6: an approved project finding is shared, the private note is not --------------------------------------------------------
            async def s6() -> tuple[bool, str]:
                r = await d.send(
                    "POST",
                    "/v1/memories/proposals",
                    "demo/agent_research",
                    key="demo-p5-finding",
                    json_={
                        "type": "semantic",
                        "content": FINDING,
                        "requested_scope": {"kind": "project", "ref": "newco-plan"},
                        "source_event_ids": [d.ids["research_event"]],
                    },
                )
                assert r.status_code in (200, 202), r.text
                d.ids["finding"] = r.json()["memory_id"]
                before = (await d.search("demo/agent_planning", "state licence NewCo", include=[]))["hits"]
                review = await d.open_review(d.ids["finding"])
                if review:
                    assert (await d.decide(review, "approve")).status_code == 200
                await d.embedded(d.ids["finding"])
                found = await d.search("demo/agent_planning", "state licence NewCo")
                note = await d.search("demo/agent_planning", f"check NewCo licensing rules for the plan {RUN}", all_runs=True)
                ok = d.ids["finding"] in [h["memory_id"] for h in found["hits"]] and d.ids["note"] not in [
                    h["memory_id"] for h in found["hits"] + note["hits"]
                ]
                return (
                    ok,
                    f"the finding was invisible to agent_planning while proposed ({len(before)} hits), visible once the reviewer approved it; the research agent's working note never appears for agent_planning",
                )

            await step(
                "6 an approved project finding is shared (MEM-03)",
                "agent_planning finds the approved project:newco-plan finding and never the working note",
                s6,
            )

            # ---- 7: relevance (RET-02) -------------------------------------------------------------------------------------------------------
            async def s7() -> tuple[bool, str]:
                d.ids["cyclist"] = await d.ingest("demo-p5-cyclist", TEXT["cyclist"], subjects=[d.ids["sarah"]])
                await d.wait(d.ids["cyclist"])
                (cycling,) = await d.memory_of(d.ids["cyclist"])
                await d.embedded(d.ids["business"], cycling.id)
                out = await d.search("demo/agent_planning", "starting a business", subject_ids=[d.ids["sarah"]])
                ids = [h["memory_id"] for h in out["hits"]]
                ok = ids[:1] == [d.ids["business"]] and cycling.id not in ids and out["degraded"] == []
                top = out["hits"][0]
                return (
                    ok,
                    f"'starting a business' for Sarah -> the business memory first (matched by {top['matched_by']}, score {top['score']}); the cycling memory is not returned; coverage {out['embedding_coverage']}, {out['candidates_considered']} authorised candidates",
                )

            await step(
                "7 relevant memory ranks first, the distractor is excluded (RET-02)",
                "the business memory first; the cycling memory excluded",
                s7,
            )

            # ---- 8: the unauthorised agent and another workspace --------------------------------------------------------------------------
            async def s8() -> tuple[bool, str]:
                out = await d.search("demo/agent_support", "starting a business entrepreneurship", subject_ids=[d.ids["sarah"]])
                real = await d.send("GET", f"/v1/memories/{d.ids['business']}", "demo/agent_support")
                ghost = await d.send("GET", f"/v1/memories/{uuid.uuid4()}", "demo/agent_support")
                strip = lambda r: {k: v for k, v in r.json()["error"].items() if k != "request_id"}  # noqa: E731
                other = await d.search("other_demo/admin@other", "starting a business entrepreneurship")
                other_get = await d.send("GET", f"/v1/memories/{d.ids['business']}", "other_demo/admin@other")
                ok = (
                    out["hits"] == []
                    and out["candidates_considered"] == 0
                    and real.status_code == ghost.status_code == 404
                    and strip(real) == strip(ghost)
                    and other["hits"] == []
                    and other["candidates_considered"] == 0
                    and other_get.status_code == 404
                )
                return (
                    ok,
                    f"agent_support: {len(out['hits'])} hits, candidates_considered {out['candidates_considered']}, the memory's id -> {real.status_code} identical to a random id; admin@other: {len(other['hits'])} hits, id -> {other_get.status_code}",
                )

            await step(
                "8 no grant, no memory (MEM-05, SEC-02)",
                "an agent with no subject grant gets nothing, not even candidates or counts; another workspace gets nothing",
                s8,
            )

            # ---- 9: supersede keeps the lineage -------------------------------------------------------------------------------------------
            async def s9() -> tuple[bool, str]:
                cur = await d.send("GET", f"/v1/memories/{d.ids['pref']}", "demo/reviewer@demo")
                if cur.json()["status"] == "active":
                    r = await d.send(
                        "POST",
                        f"/v1/memories/{d.ids['pref']}/supersede",
                        "demo/reviewer@demo",
                        key="demo-p5-supersede",
                        json_={"content": PREF2, "reason_code": "clarified"},
                        headers={"if-match": cur.headers["etag"]},
                    )
                    assert r.status_code == 201, r.text
                (new,) = await d.sql(
                    "SELECT m.id::text AS id FROM memory_lineage l JOIN curated_memories m ON m.id = l.memory_id WHERE l.workspace_id = :ws AND l.related_id = CAST(:o AS uuid) AND l.relation = 'replaces'",
                    o=d.ids["pref"],
                )
                await d.embedded(new.id)
                old = (await d.send("GET", f"/v1/memories/{d.ids['pref']}", "demo/reviewer@demo")).json()
                fresh = (await d.send("GET", f"/v1/memories/{new.id}", "demo/reviewer@demo")).json()
                hits = [
                    h["memory_id"]
                    for h in (
                        await d.search("demo/agent_planning", "comparisons risks trade-offs recommendation", subject_ids=[d.ids["sarah"]])
                    )["hits"]
                ]
                ok = (
                    old["status"] == "superseded"
                    and old["content"] == PREF
                    and fresh["status"] == "active"
                    and fresh["content"] == PREF2
                    and [e["interaction_id"] for e in old["evidence"]] == [d.ids["evt004"]]
                    and any(e["source_kind"] == "interaction" and e["interaction_id"] == d.ids["evt004"] for e in fresh["evidence"])
                    and {(x["direction"], x["memory_id"]) for x in old["lineage"]} == {("replaced_by", new.id)}
                    and new.id in hits
                    and d.ids["pref"] not in hits
                )
                return (
                    ok,
                    f"the preference was superseded: old row kept ({old['status']}, evidence intact), new active; lineage {[x['direction'] for x in old['lineage']]}; search returns only the new one",
                )

            await step(
                "9 supersede keeps the lineage (MEM-04)",
                "old memory kept as superseded with its evidence; the new one replaces it in search",
                s9,
            )

            # ---- 10: TTL -----------------------------------------------------------------------------------------------------------------------
            async def s10() -> tuple[bool, str]:
                run = f"ttl-{RUN}"
                r = await d.send(
                    "POST",
                    "/v1/memories",
                    "demo/agent_research",
                    json_={"type": "working", "content": f"short lived scratch note {RUN}", "run_id": run, "ttl_hours": 0.0008},
                )
                assert r.status_code == 201, r.text
                mid = r.json()["memory_id"]
                await d.embedded(mid)
                early = await d.search("demo/agent_research", f"short lived scratch note {RUN}", run_id=run)
                await asyncio.sleep(4)
                late = await d.search("demo/agent_research", f"short lived scratch note {RUN}", run_id=run)
                status = "?"
                for _ in range(60):
                    status = (await d.sql("SELECT status FROM curated_memories WHERE id = CAST(:m AS uuid)", m=mid))[0].status
                    if status == "expired":
                        break
                    await asyncio.sleep(1)
                ok = [h["memory_id"] for h in early["hits"]] == [mid] and late["hits"] == [] and status == "expired"
                return (
                    ok,
                    f"a ~3 s working note: found at {len(early['hits'])} hit(s), gone from search when its time passed ({len(late['hits'])}), and the delayed outbox job marked it {status}",
                )

            await step(
                "10 a working note expires on time", "visible until its deadline, then excluded; the delayed job records the expiry", s10
            )

            # ---- 11: embedding outage ---------------------------------------------------------------------------------------------------------
            async def s11() -> tuple[bool, str]:
                await d.mock(
                    "POST", "/__mock/faults", json={"faults": [{"endpoint": "embeddings", "code": "provider_unavailable", "count": 60}]}
                )
                try:
                    r = await d.send(
                        "POST",
                        "/v1/memories",
                        "demo/conversation_app",
                        key=f"demo-p5-outage-{RUN}",
                        json_={
                            "type": "semantic",
                            "content": f"Sarah keeps planning notes in the shared drive {RUN}.",
                            "subject_ids": [d.ids["sarah"]],
                            "source_ref": "demo",
                            "purpose": "outage drill",
                        },
                    )
                    assert r.status_code == 201, r.text
                    mid = r.json()["memory_id"]
                    await asyncio.sleep(1.5)
                    out = await d.search("demo/agent_planning", f"shared drive planning notes {RUN}", subject_ids=[d.ids["sarah"]])
                    pending = (await d.sql("SELECT embedding_status FROM curated_memories WHERE id = CAST(:m AS uuid)", m=mid))[
                        0
                    ].embedding_status
                finally:
                    await d.mock("DELETE", "/__mock/requests")  # clears the faults
                await d.embedded(mid, timeout=120)
                ready = (await d.sql("SELECT embedding_status FROM curated_memories WHERE id = CAST(:m AS uuid)", m=mid))[
                    0
                ].embedding_status
                healthy = await d.search("demo/agent_planning", f"shared drive planning notes {RUN}", subject_ids=[d.ids["sarah"]])
                ok = (
                    pending == "pending"
                    and out["degraded"] == ["vector_unavailable"]
                    and mid in [h["memory_id"] for h in out["hits"]]
                    and ready == "ready"
                    and healthy["degraded"] == []
                )
                return (
                    ok,
                    f"router embeddings down: the memory was saved ({pending}), search answered lexical-only with degraded {out['degraded']} and still found it; after recovery it is {ready} and search is healthy again",
                )

            await step(
                "11 an embedding outage degrades, then recovers",
                "saved as pending; search says vector_unavailable and still returns lexical hits; the job heals itself",
                s11,
            )

            # ---- 12: telemetry holds no content -------------------------------------------------------------------------------------------
            async def s12() -> tuple[bool, str]:
                sentinel = f"zq{uuid.uuid4().hex}"
                r = await d.send(
                    "POST",
                    "/v1/memories",
                    "demo/conversation_app",
                    json_={
                        "type": "semantic",
                        "content": f"Sarah's codename is {sentinel}.",
                        "subject_ids": [d.ids["sarah"]],
                        "source_ref": "demo",
                        "purpose": "sentinel drill",
                    },
                )
                assert r.status_code == 201, r.text
                await d.embedded(r.json()["memory_id"])
                await d.search("demo/agent_planning", sentinel, subject_ids=[d.ids["sarah"]])
                found = (await d.search("demo/agent_planning", sentinel, subject_ids=[d.ids["sarah"]]))["hits"]
                logs = await asyncio.to_thread(compose, "logs", "--no-log-prefix", "--tail=3000", "api", "worker")
                async with httpx.AsyncClient() as c:
                    metrics = (await c.get(METRICS)).text
                audit = " ".join(
                    x.m
                    for x in await d.sql(
                        "SELECT metadata::text || coalesce(resource_id, '') AS m FROM audit_log WHERE workspace_id = :ws ORDER BY created_at DESC LIMIT 400"
                    )
                )
                calls = " ".join(
                    str(tuple(x))
                    for x in await d.sql("SELECT * FROM model_calls WHERE workspace_id = :ws ORDER BY created_at DESC LIMIT 200")
                )
                jobs = " ".join(
                    x.m
                    for x in await d.sql(
                        "SELECT payload::text || dedupe_key AS m FROM outbox_jobs WHERE workspace_id = :ws ORDER BY created_at DESC LIMIT 200"
                    )
                )
                clean = all(sentinel not in blob for blob in (logs, metrics, audit, calls, jobs))
                return (
                    bool(found) and clean,
                    f"a memory containing {sentinel[:8]}… was saved, embedded and found by search; it appears in no log line, metric, audit entry, model-call row or job payload (only in the memory itself)",
                )

            await step(
                "12 memory content never reaches telemetry", "no memory text in logs, metrics, audit, the call log or job payloads", s12
            )
        finally:
            await d.mock("DELETE", "/__mock/fixtures")
            await d.mock("DELETE", "/__mock/requests")
            await d.mock("POST", "/__mock/strict", json={"enabled": False})

    width = max(len(r[0]) for r in rows)
    print(f"{'step'.ljust(width)}  result  expected -> actual")
    for name, expect, actual, ok in rows:
        print(f"{name.ljust(width)}  {'PASS' if ok else 'FAIL'}    {expect} -> {actual}")
    passed = sum(1 for r in rows if r[3])
    print(f"\n{passed}/{len(rows)} passed")
    print(
        "\nstep 13 (make eval-memory) scores extraction and retrieval against the scenario gold set; for the real router run:\n  OSO_ROUTER_API_KEY=... make eval-memory-live"
    )
    return 0 if passed == len(rows) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
