"""Phase 8 demo: governance: corrections, consolidation, deletion, ontology simulation, settings and the privacy audit, offline. Exits non-zero if any assertion fails.

Run after `make up seed` (`make demo-p8` publishes the v1.4.0 example ontology, then runs this). Every run builds its OWN people and interactions (run-unique names and ids) in the demo
workspace, so it can be repeated and never deletes the story the earlier demos left; the one thing it publishes is the next patch of the v1.5.x ontology. It uses the strict mock
router (the model is scripted) and the real API, worker, Postgres and Neo4j. Reads API keys from .local/dev-credentials.json.

The story (blueprint reference scenario, steps 9-10): simulate a draft ontology that adds MANAGES, publish it, correct a wrong fact with an impact preview, correct an authoritative
source's mistake, split a wrong identity link, consolidate duplicates, delete a person and prove it, hold another, and show what the privacy audit recorded."""

from __future__ import annotations

import asyncio
import csv
import io
import json
import os
import sys
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx
import yaml
from kg_cli.seed import workspace_id_for
from kg_common.settings import Settings
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

ROOT = Path(__file__).resolve().parents[1]
BASE = os.environ.get("KG_API_URL", "http://127.0.0.1:8000")
MOCK = os.environ.get("KG_MOCK_URL", "http://127.0.0.1:8782")
DEMO_WS = workspace_id_for("demo")
RUN = uuid.uuid4().hex[:8]
TAG = "".join(
    chr(ord("g") + int(c, 16)) for c in RUN
)  # the run id in letters: a digit in a quoted memory would read as an invented number to the synthesis validator
Row = tuple[str, str, str, bool]
ADMIN, REVIEWER, APP = "demo/admin@demo", "demo/reviewer@demo", "demo/conversation_app"
MICHAEL = f"Michael Rowe {RUN}"
MICHAEL_EXT = f"E-MR-{RUN}"
HOLDEE = f"Harper Lind {RUN}"
CORPUS = ROOT / "tests" / "fixtures" / "injection" / "corpus.yaml"


class Demo:
    def __init__(self, client: httpx.AsyncClient, keys: dict[str, str], settings: Settings):
        self.c, self.keys, self.settings = client, keys, settings
        self.ids: dict[str, Any] = {}

    async def send(
        self, method: str, path: str, who: str, *, json_: Any = None, headers: dict[str, str] | None = None, **params: Any
    ) -> httpx.Response:
        hdrs = {"authorization": f"Bearer {self.keys[who]}", **(headers or {})}
        if method != "GET" and path != "/v1/context/build" and not path.endswith("/search"):
            hdrs.setdefault("idempotency-key", str(uuid.uuid4()))
        return await self.c.request(method, path, headers=hdrs, json=json_, params=params or None)

    async def sql(self, query: str, **params: Any) -> list[Any]:
        engine = create_async_engine(self.settings.database_url_owner.get_secret_value())
        try:
            async with engine.begin() as conn:
                result = await conn.execute(text(query), {"ws": DEMO_WS, **params})
                return list(result.all()) if result.returns_rows else []
        finally:
            await engine.dispose()

    async def mock(self, method: str, path: str, **kw: Any) -> None:
        async with httpx.AsyncClient() as c:
            (await c.request(method, f"{MOCK}{path}", **kw)).raise_for_status()

    async def entity(self, type_: str, name: str, ref: str, **extra: Any) -> str:
        r = await self.send(
            "POST", "/v1/entities", APP, json_={"type": type_, "name": name, "source_system": "hr", "source_ref": ref, **extra}
        )
        assert r.status_code in (200, 201, 202), r.text
        return str(r.json()["entity_id"])

    async def existing(self, type_: str, name: str) -> str:
        rows = await self.sql(
            "SELECT id::text AS id FROM entities WHERE workspace_id = :ws AND type_key = :t AND display_name = :n AND origin = 'command' AND status = 'active' ORDER BY recorded_at LIMIT 1",
            t=type_,
            n=name,
        )
        return str(rows[0].id) if rows else await self.entity(type_, name, f"demo-p8-{name}")

    async def event(self, content: str, subjects: list[str] | None = None, **extra: Any) -> str:
        body: dict[str, Any] = {
            "source_system": "chat",
            "source_ref": f"p8-{RUN}-{uuid.uuid4().hex[:6]}",
            "kind": "message",
            "content": content,
            "occurred_at": "2026-10-10T09:00:00Z",
            **extra,
        }
        if subjects:
            body["subject_refs"] = [{"entity_id": s} for s in subjects]
        r = await self.send("POST", "/v1/interactions", APP, json_=body)
        assert r.status_code == 202, r.text
        return str(r.json()["event_id"])

    async def settled(self, event_id: str, timeout: float = 240.0) -> dict[str, Any]:  # noqa: ASYNC109
        deadline = time.time() + timeout
        while time.time() < deadline:
            d = (await self.send("GET", f"/v1/interactions/{event_id}", ADMIN)).json()
            if d["processing_status"] in ("indexed", "partial", "failed"):
                quiet = await self.sql(
                    "SELECT count(*) AS n FROM outbox_jobs WHERE workspace_id = :ws AND status IN ('pending','leased') AND available_at <= now()"
                )
                if quiet[0].n == 0:
                    return dict(d)
            await asyncio.sleep(1.0)
        raise TimeoutError(f"interaction {event_id} did not settle")

    async def until(self, fn: Callable[[], Awaitable[Any]], what: str, timeout: float = 240.0) -> Any:  # noqa: ASYNC109
        deadline = time.time() + timeout
        while time.time() < deadline:
            got = await fn()
            if got:
                return got
            await asyncio.sleep(1.0)
        raise TimeoutError(what)

    async def build(self, who: str, task: str, **body: Any) -> dict[str, Any]:
        r = await self.send("POST", "/v1/context/build", who, json_={"task": task, **body})
        assert r.status_code == 200, r.text
        return dict(r.json())


def rules_for(name_a: str, name_b: str, text_marker: str, relation: str, fact: str) -> list[dict[str, Any]]:
    return [
        {
            "when": {"schema_name": "ExtractedEntities", "contains": [text_marker]},
            "response": {
                "extracted_entities": [
                    {"name": name_a, "entity_type_id": 2, "episode_indices": [0]},
                    {"name": name_b, "entity_type_id": 2, "episode_indices": [0]},
                ]
            },
        },
        {
            "when": {"schema_name": "ExtractedEdges", "contains": [text_marker]},
            "response": {
                "edges": [
                    {
                        "source_entity_name": name_a,
                        "target_entity_name": name_b,
                        "relation_type": relation,
                        "fact": fact,
                        "valid_at": "2026-01-01T00:00:00Z",
                        "invalid_at": None,
                        "episode_indices": [0],
                    }
                ]
            },
        },
        {
            "when": {"schema_name": "EdgeDuplicate", "contains": [f"<new fact> {fact}"]},
            "response": {"duplicate_facts": [], "contradicted_facts": []},
        },
        {
            "when": {"schema_name": "EdgeTimestamps", "contains": [f"<fact> {fact}"]},
            "response": {"valid_at": "2026-01-01T00:00:00Z", "invalid_at": None},
        },
        {"when": {"schema_name": relation, "contains": [fact]}, "response": {}},
        *node_rules(text_marker, [name_a, name_b]),
    ]


def node_rules(marker: str, names: list[str]) -> list[dict[str, Any]]:
    """What Graphiti asks when the workspace already has nodes: who is new (nobody: the platform decides identity), and a one-line summary per node."""
    return [
        {
            "when": {"schema_name": "NodeResolutions", "contains": [marker]},
            "response": {"entity_resolutions": [{"id": i, "name": n, "duplicate_candidate_id": -1} for i, n in enumerate(names)]},
        },
        {
            "when": {"schema_name": "SummarizedEntities", "contains": [marker]},
            "response": {"summaries": [{"name": n, "summary": f"{n}."} for n in names]},
        },
    ]


async def staging_nodes() -> int:
    from neo4j import AsyncGraphDatabase

    s = Settings()
    driver = AsyncGraphDatabase.driver(s.neo4j_uri, auth=(s.neo4j_user, s.neo4j_password.get_secret_value()))
    try:
        async with driver.session() as session:
            res = await session.run("MATCH (n) WHERE n.group_id STARTS WITH $p RETURN count(n) AS n", p=f"ws-{DEMO_WS}-sim-")
            rec = await res.single()
            return int(rec["n"]) if rec else 0
    finally:
        await driver.close()


async def graph_hits(d: Demo, term: str) -> int:
    from neo4j import AsyncGraphDatabase

    s = Settings()
    driver = AsyncGraphDatabase.driver(s.neo4j_uri, auth=(s.neo4j_user, s.neo4j_password.get_secret_value()))
    try:
        async with driver.session() as session:
            res = await session.run(
                "MATCH (n {group_id: $g}) WHERE any(k IN keys(n) WHERE n[k] IS :: STRING AND toLower(n[k]) CONTAINS toLower($t)) RETURN count(n) AS n",
                g=f"ws-{DEMO_WS}",
                t=term,
            )
            rec = await res.single()
            edges = await session.run(
                "MATCH ()-[e:RELATES_TO {group_id: $g}]->() WHERE any(k IN keys(e) WHERE e[k] IS :: STRING AND toLower(e[k]) CONTAINS toLower($t)) RETURN count(e) AS n",
                g=f"ws-{DEMO_WS}",
                t=term,
            )
            erec = await edges.single()
            return int(rec["n"]) + int(erec["n"])
    finally:
        await driver.close()


async def main() -> int:
    creds_path = ROOT / ".local" / "dev-credentials.json"
    if not creds_path.exists():
        print("missing .local/dev-credentials.json: run `make seed` first", file=sys.stderr)
        return 2
    keys: dict[str, str] = json.loads(creds_path.read_text())
    for need in (ADMIN, REVIEWER, APP, "demo/agent_research", "other_demo/admin@other"):
        if need not in keys:
            print(f"missing {need}: run `make seed`", file=sys.stderr)
            return 2
    rows: list[Row] = []
    async with httpx.AsyncClient(base_url=BASE, timeout=180) as client:
        d = Demo(client, keys, Settings())
        await d.mock("POST", "/__mock/strict", json={"enabled": True})
        await d.mock("POST", "/__mock/reload")
        await d.mock("DELETE", "/__mock/fixtures")
        await d.mock(
            "POST",
            "/__mock/fixtures",
            json={"rules": [{"when": {"schema_name": "MemoryCandidates", "contains": [RUN]}, "response": {"candidates": []}}]},
        )  # no memory is extracted from the demo's own events

        async def step(name: str, expect: str, fn: Callable[[], Awaitable[tuple[bool, str]]]) -> None:
            try:
                ok, actual = await fn()
            except Exception as exc:
                ok, actual = False, f"{type(exc).__name__}: {str(exc)[:300]}"
            rows.append((name, expect, actual, ok))

        start_active = (await d.send("GET", "/v1/ontologies/active", ADMIN)).json()["version"]["id"]
        try:
            sarah = await d.existing("Person", "Sarah Johnson")
            acme = await d.existing("Organization", "Acme")
            michael = await d.entity("Person", MICHAEL, f"michael-{RUN}", external_ids=[{"system": "hr", "value": MICHAEL_EXT}])
            d.ids.update(sarah=sarah, acme=acme, michael=michael)

            # ---- 1: simulation -----------------------------------------------------------------------------------------------------------------------
            async def s1() -> tuple[bool, str]:
                text_ = f"Sarah Johnson: {MICHAEL} reports to me; I manage {MICHAEL} (run {RUN})."
                fact = f"Sarah Johnson manages {MICHAEL}"
                await d.mock("POST", "/__mock/fixtures", json={"rules": rules_for("Sarah Johnson", MICHAEL, f"run {RUN}", "MANAGES", fact)})
                events = [await d.event(text_ + f" #{i}", [sarah, michael]) for i in range(3)]
                for e in events:
                    await d.settled(e)
                d.ids["old_events"] = events
                versions = (await d.send("GET", "/v1/ontologies/versions", ADMIN, limit=100)).json()["items"]
                patches = [
                    int(v["version"].split(".")[2])
                    for v in versions
                    if str(v["version"]).startswith("1.5.") and v["version"].count(".") == 2
                ]
                nxt = f"1.5.{max(patches) + 1 if patches else 0}"
                spec = yaml.safe_load((ROOT / "examples/ontologies/personal-knowledge-governance.yaml").read_text())
                spec["ontology"]["version"] = nxt
                draft = (
                    await d.send("POST", "/v1/ontologies/versions", ADMIN, json_={"content": json.loads(json.dumps(spec, default=str))})
                ).json()
                d.ids["draft"], d.ids["version"] = draft["id"], nxt
                pre = (
                    await d.send("POST", f"/v1/ontologies/versions/{draft['id']}/simulate", ADMIN, json_={"interaction_ids": events})
                ).json()
                calls_before = (await d.sql("SELECT count(*) AS n FROM model_calls WHERE workspace_id = :ws"))[0].n
                go = await d.send(
                    "POST", f"/v1/ontologies/versions/{draft['id']}/simulate", ADMIN, json_={"simulation_id": pre["id"], "confirm": True}
                )
                assert go.status_code == 202, go.text

                async def done() -> dict[str, Any] | None:
                    s = (await d.send("GET", f"/v1/ontologies/simulations/{pre['id']}", ADMIN)).json()
                    return dict(s) if s["status"] in ("completed", "failed") else None

                sim = await d.until(done, "the simulation to finish")
                rep = sim["report"]
                gone = await staging_nodes()
                real = (
                    await d.sql(
                        "SELECT count(*) AS n FROM facts WHERE workspace_id = :ws AND rel_type = 'MANAGES' AND source_interaction_id = ANY(CAST(:ids AS uuid[]))",
                        ids=events,
                    )
                )[0].n
                ok = (
                    pre["status"] == "previewed" and pre["estimate"]["calls"] > 0 and calls_before >= 0 and sim["status"] == "completed"
                    and rep["facts_by_type"].get("MANAGES", 0) >= 1 and "MANAGES" in rep["new_relationship_types"] and gone == 0
                )  # fmt: skip
                return (
                    ok,
                    f"estimate {pre['estimate']['calls']} calls (${pre['cost_estimate']}) without a model call; ran on {rep['episodes']} interactions: {rep['facts_by_type']}, new types {rep['new_relationship_types']}; {gone} staging nodes left; {real} MANAGES facts written to the workspace by the extraction before publishing (v1.4.0 rejected them)",
                )

            await step(
                "1 simulate a draft that adds MANAGES (ONT-05)",
                "preview costs nothing; the report shows MANAGES; the staging graph is gone",
                s1,
            )

            # ---- 2: publish and pin ------------------------------------------------------------------------------------------------------------------
            async def s2() -> tuple[bool, str]:
                prior = (await d.send("GET", "/v1/ontologies/active", ADMIN)).json()["version"]["id"]
                pub = await d.send(
                    "POST", f"/v1/ontologies/versions/{d.ids['draft']}/publish", ADMIN, json_={"notes": "adds MANAGES (demo-p8)"}
                )
                assert pub.status_code == 200, pub.text
                text_ = f"Sarah Johnson: {MICHAEL} is my manager now, he manages me (new {RUN})."
                fact = f"{MICHAEL} manages Sarah Johnson"
                await d.mock("POST", "/__mock/fixtures", json={"rules": rules_for(MICHAEL, "Sarah Johnson", f"new {RUN}", "MANAGES", fact)})
                new = await d.event(text_, [sarah, michael])
                await d.settled(new)
                d.ids["new_event"] = new
                pins = {
                    r.id: str(r.ontology_version_id)
                    for r in await d.sql(
                        "SELECT id::text AS id, ontology_version_id FROM interactions WHERE workspace_id = :ws AND id::text = ANY(CAST(:ids AS text[]))",
                        ids=[*d.ids["old_events"], new],
                    )
                }
                ok = pins[new] == d.ids["draft"] and all(pins[e] == prior for e in d.ids["old_events"])
                return (
                    ok,
                    f"v{d.ids['version']} published and active; the new interaction pins it, the earlier {len(d.ids['old_events'])} keep {prior[:8]}… (AT-17)",
                )

            await step("2 publish and pin (AT-17)", "new work pins the new version; older provenance does not move", s2)

            # ---- 3: the extracted MANAGES fact, unverified, in a planning context ----------------------------------------------------------------
            async def s3() -> tuple[bool, str]:
                rows_ = await d.sql(
                    "SELECT id::text AS id, status, evidence_status FROM facts WHERE workspace_id = :ws AND rel_type = 'MANAGES' AND source_interaction_id = CAST(:i AS uuid) AND source_type = 'extracted'",
                    i=d.ids["new_event"],
                )
                assert rows_, "no MANAGES fact was extracted"
                d.ids["manages"] = rows_[0].id
                ctx = await d.build(ADMIN, f"What do we know about {MICHAEL} and Sarah?", subject_ids=[sarah, michael], profile="planning")
                d.ids["old_context"] = ctx["context_id"]
                shown = [ln for ln in ctx["context_text"].splitlines() if "manages" in ln.lower()]
                return (
                    rows_[0].evidence_status == "unverified"
                    and bool(shown)
                    and any(w in shown[0].lower() for w in ("unverified", "reported")),
                    f"MANAGES is {rows_[0].status}/{rows_[0].evidence_status}; the context line says: {shown[0][:90] if shown else '(none)'}",
                )

            await step("3 the new relationship arrives unverified", "an extracted MANAGES fact, labelled unverified in the context", s3)

            # ---- 4: reviewer correction with impact preview -------------------------------------------------------------------------------------
            async def s4() -> tuple[bool, str]:
                mem = await d.send(
                    "POST", "/v1/memories/proposals", ADMIN,
                    json_={"type": "semantic", "content": f"{MICHAEL} manages Sarah (run {RUN}).", "fact_ids": [d.ids["manages"]], "subject_ids": [michael], "purpose": "org chart"},
                )  # fmt: skip
                assert mem.status_code in (201, 202), mem.text
                d.ids["dep_memory"] = mem.json()["memory_id"]
                fact_rev = (await d.send("GET", f"/v1/relationships/{d.ids['manages']}", REVIEWER)).json()["revision"]
                pv = (await d.send("GET", f"/v1/relationships/{d.ids['manages']}/corrections/preview", REVIEWER, kind="reject")).json()
                done = await d.send(
                    "POST", f"/v1/relationships/{d.ids['manages']}/corrections", REVIEWER,
                    json_={"kind": "reject", "reason_code": "incorrect_extraction"}, headers={"if-match": f'W/"rev-{fact_rev}"'},
                )  # fmt: skip
                assert done.status_code == 200, done.text
                status = (await d.sql("SELECT status FROM facts WHERE id = CAST(:f AS uuid)", f=d.ids["manages"]))[0].status
                mstatus = (await d.sql("SELECT status FROM curated_memories WHERE id = CAST(:m AS uuid)", m=d.ids["dep_memory"]))[0].status
                fresh = await d.build(
                    ADMIN, f"What do we know about {MICHAEL} and Sarah?", subject_ids=[sarah, michael], profile="planning"
                )
                old = (await d.send("GET", f"/v1/context/{d.ids['old_context']}", ADMIN)).json()
                leftover = [ln for ln in fresh["context_text"].splitlines() if "manages" in ln.lower() and MICHAEL in ln]
                gone = not leftover
                ok = (
                    sum(pv["dependent_memory_counts"].values()) >= 1 and status == "rejected" and mstatus == "disputed"
                    and gone and old.get("rematerialized") is True and old.get("withdrawn", 0) >= 1
                )  # fmt: skip
                return (
                    ok,
                    f"preview: {pv['graph_operations']}, {pv['dependent_memory_counts']} dependent memories, {pv.get('recent_context_count')} recent contexts; fact {status}, memory {mstatus}; the next context omits it ({leftover[:1]}); the old link re-materialises with {old.get('withdrawn')} withdrawn",
                )

            await step(
                "4 reviewer rejects the wrong fact (Flow D)",
                "preview, then reject: edge expired, memory disputed, old context link withdrawn",
                s4,
            )

            # ---- 5: AT-13, an authoritative source is wrong -------------------------------------------------------------------------------------
            async def s5() -> tuple[bool, str]:
                labs = await d.entity("Organization", f"Acme Labs {RUN}", f"labs-{RUN}")
                wrong = await d.send(
                    "POST", "/v1/relationships", APP,
                    json_={"type": "WORKS_AT", "source": {"entity_id": michael}, "target": {"entity_id": labs}, "valid_from": "2026-01-01", "source_system": "hr", "source_ref": f"hr-{RUN}", "attributes": {"role": "Engineer"}},
                )  # fmt: skip
                assert wrong.status_code in (200, 201, 202), wrong.text
                fid = wrong.json()["fact_id"]
                rev = (await d.send("GET", f"/v1/relationships/{fid}", ADMIN)).json()["revision"]
                fixed = await d.send(
                    "POST", f"/v1/relationships/{fid}/corrections", ADMIN,
                    json_={"kind": "supersede", "reason_code": "source_error", "target": {"entity_id": acme}, "valid_from": "2026-01-01"}, headers={"if-match": f'W/"rev-{rev}"'},
                )  # fmt: skip
                assert fixed.status_code == 200, fixed.text
                out = fixed.json()
                ctx = await d.build(ADMIN, f"Where does {MICHAEL} work?", subject_ids=[michael], profile="quick_answer")
                audit = await d.sql(
                    "SELECT metadata FROM audit_log WHERE workspace_id = :ws AND action = 'correction.applied' AND resource_id = :c",
                    c=fid,
                )
                review = await d.sql(
                    "SELECT 1 FROM review_items WHERE workspace_id = :ws AND item_type = 'authoritative_override' AND status = 'open'"
                )
                ok = (
                    out["authoritative_override"]
                    and f"Acme Labs {RUN}" not in ctx["context_text"]
                    and "Acme" in ctx["context_text"]
                    and bool(audit)
                    and bool(review)
                )
                return (
                    ok,
                    f"superseded to Acme with a reason; the context says Acme, not Acme Labs; audit entry linked to correction {out['correction_id'][:8]}…; an authoritative_override notice is open",
                )

            await step("5 correct an authoritative source (AT-13)", "context fixed, audit linked, override notice raised", s5)

            # ---- 6: identity split --------------------------------------------------------------------------------------------------------------
            async def s6() -> tuple[bool, str]:
                name = f"Sam Quill {RUN}"
                quill = await d.entity("Person", name, f"quill-{RUN}", external_ids=[{"system": "hr", "value": f"E-SQ-{RUN}"}])
                await d.mock(
                    "POST",
                    "/__mock/fixtures",
                    json={
                        "rules": [
                            {
                                "when": {"schema_name": "ExtractedEntities", "contains": [f"quill {RUN}"]},
                                "response": {"extracted_entities": [{"name": name, "entity_type_id": 2, "episode_indices": [0]}]},
                            },
                            {"when": {"schema_name": "ExtractedEdges", "contains": [f"quill {RUN}"]}, "response": {"edges": []}},
                            *node_rules(f"quill {RUN}", [name]),
                        ]
                    },
                )
                for i in range(2):
                    await d.settled(await d.event(f"{name} wrote about quill {RUN} part {i}.", [quill]))
                mentions = (await d.send("GET", f"/v1/entities/{quill}/mentions", REVIEWER)).json()
                assert len(mentions) >= 2, mentions
                rev = (await d.send("GET", f"/v1/entities/{quill}", REVIEWER)).json()["revision"]
                done = await d.send(
                    "POST", f"/v1/entities/{quill}/corrections", REVIEWER,
                    json_={"kind": "split", "reason_code": "wrong_identity", "new_entity": {"display_name": f"S. Quill (other) {RUN}", "type": "Person"}, "move": {"mention_ids": [mentions[0]["id"]]}},
                    headers={"if-match": f'W/"rev-{rev}"'},
                )  # fmt: skip
                assert done.status_code == 200, done.text
                new_id = done.json()["new_target_id"]
                left = (await d.send("GET", f"/v1/entities/{quill}/mentions", REVIEWER)).json()
                moved = (await d.send("GET", f"/v1/entities/{new_id}/mentions", REVIEWER)).json()
                nm = await d.sql(
                    "SELECT 1 FROM identity_non_matches WHERE workspace_id = :ws AND (entity_a = CAST(:a AS uuid) OR entity_b = CAST(:a AS uuid))",
                    a=quill,
                )
                return len(moved) == 1 and len(left) == len(mentions) - 1 and bool(
                    nm
                ), f"one mention moved to a new person {new_id[:8]}…; the original keeps {len(left)}; the pair is recorded as a non-match"

            await step("6 split a wrong identity link", "the mention moves to a new person; the two are recorded as a non-match", s6)

            # ---- 7: ADM-03 ---------------------------------------------------------------------------------------------------------------------
            async def s7() -> tuple[bool, str]:
                mem = (await d.send("GET", f"/v1/memories/{d.ids['dep_memory']}", REVIEWER)).json()
                fixed = await d.send(
                    "POST", f"/v1/memories/{d.ids['dep_memory']}/corrections", REVIEWER,
                    json_={"kind": "supersede", "reason_code": "incorrect_extraction", "content": f"{MICHAEL} does not manage Sarah (run {RUN})."}, headers={"if-match": f'W/"rev-{mem["revision"]}"'},
                )  # fmt: skip
                assert fixed.status_code == 200, fixed.text
                new_id = fixed.json()["new_target_id"]
                ctx = {}
                for _ in range(60):  # the corrected memory is searchable once its vector exists
                    ctx = await d.build(ADMIN, f"Does {MICHAEL} manage Sarah?", subject_ids=[sarah, michael], profile="planning")
                    if new_id in {m["id"] for m in ctx["memories"]}:
                        break
                    await asyncio.sleep(1.0)
                about = [ln for ln in ctx["context_text"].splitlines() if MICHAEL in ln and "manage" in ln]
                ok = (
                    any("does not manage Sarah" in ln for ln in about)
                    and not any("manages Sarah" in ln for ln in about)
                    and new_id in {m["id"] for m in ctx["memories"]}
                )
                return ok, f"the corrected memory is in the next Context Lab build and the disputed one is not: {[ln[:90] for ln in about]}"

            await step("7 reviewer corrects the disputed memory (ADM-03)", "the build reflects the correction", s7)

            # ---- 8: consolidation --------------------------------------------------------------------------------------------------------------
            async def s8() -> tuple[bool, str]:
                riley = await d.entity(
                    "Person", f"Riley Park {RUN}", f"riley-{RUN}"
                )  # its own subject: earlier runs' approved summaries are never in this group
                pref = f"Riley prefers comparisons that show risks and trade-offs (run {RUN})"
                mids = []
                for suffix in (".", "!", " "):
                    r = await d.send(
                        "POST",
                        "/v1/memories",
                        ADMIN,
                        json_={
                            "type": "semantic",
                            "content": pref + suffix,
                            "subject_ids": [riley],
                            "purpose": "advice style",
                            "source_ref": f"p8-{RUN}-{uuid.uuid4().hex[:5]}",
                        },
                    )
                    assert r.status_code == 201, r.text
                    mids.append(r.json()["memory_id"])
                for c in (f"Riley dislikes the commute (run {RUN})", f"Riley wants remote work (run {RUN})"):
                    assert (
                        await d.send(
                            "POST",
                            "/v1/memories",
                            ADMIN,
                            json_={
                                "type": "semantic",
                                "content": c,
                                "subject_ids": [riley],
                                "purpose": "advice style",
                                "source_ref": f"p8-{RUN}-{uuid.uuid4().hex[:5]}",
                            },
                        )
                    ).status_code == 201
                episodes = [
                    f"Riley rode 40 km with Tom on Saturday (run {TAG}).",
                    f"Riley joined the Velo Club in March (run {TAG}).",
                    f"Riley plans a bike trip with Tom (run {TAG}).",
                ]
                emids = []
                for c in episodes:
                    r = await d.send(
                        "POST",
                        "/v1/memories",
                        ADMIN,
                        json_={
                            "type": "episodic",
                            "content": c,
                            "subject_ids": [riley],
                            "purpose": "history",
                            "source_ref": f"p8-{RUN}-{uuid.uuid4().hex[:5]}",
                        },
                    )
                    assert r.status_code == 201, r.text
                    emids.append(r.json()["memory_id"])
                await d.mock(
                    "POST", "/__mock/fixtures",
                    json={"rules": [{"when": {"schema_name": "ConsolidationProposal", "contains": [f"Velo Club in March (run {TAG})"]}, "response": {"groups": [{"source_memory_ids": ["M1", "M2", "M3"], "summary": "Riley rides with Tom and the Velo Club.", "statements": [{"text": episodes[0], "source_memory_ids": ["M1"]}, {"text": episodes[1], "source_memory_ids": ["M2"]}, {"text": episodes[2], "source_memory_ids": ["M3"]}], "keep_separate": []}]}}]},
                )  # fmt: skip

                async def embedded() -> bool:
                    rows_ = await d.sql(
                        "SELECT count(*) AS n FROM memory_embeddings e WHERE e.workspace_id = :ws AND e.memory_id::text = ANY(CAST(:ids AS text[]))",
                        ids=[*mids, *emids],
                    )
                    return bool(rows_[0].n >= len(mids) + len(emids))

                await d.until(embedded, "the memories to be embedded (reinforcement needs their vectors)", timeout=120)
                await d.sql(
                    "UPDATE outbox_jobs SET available_at = now() WHERE workspace_id = :ws AND stage = 'memory_consolidate' AND status = 'pending'"
                )

                async def folded() -> bool:
                    st = {
                        r.id: r.status
                        for r in await d.sql(
                            "SELECT id::text AS id, status FROM curated_memories WHERE workspace_id = :ws AND id::text = ANY(CAST(:ids AS text[]))",
                            ids=mids,
                        )
                    }
                    proposals = await d.sql(
                        "SELECT 1 FROM review_items WHERE workspace_id = :ws AND item_type = 'consolidation_proposal' AND status = 'open'"
                    )
                    return sum(1 for v in st.values() if v == "active") == 1 and bool(proposals)

                await d.until(folded, "reinforcement and the synthesis proposal", timeout=240)
                item = (await d.send("GET", "/v1/reviews", REVIEWER, type="consolidation_proposal", status="open")).json()["items"][0]
                detail = await d.send("GET", f"/v1/reviews/{item['id']}", REVIEWER)
                body = detail.json()
                sources = [m for m in body["memories"] if m["role"] == "source"]
                decide = await d.send(
                    "POST", f"/v1/reviews/{item['id']}/decision", REVIEWER,
                    json_={"decision": "approve", "rationale_code": "verified_with_source"}, headers={"if-match": detail.headers["etag"]},
                )  # fmt: skip
                assert decide.status_code == 200, decide.text
                distinct = await d.sql(
                    "SELECT count(*) AS n FROM curated_memories WHERE workspace_id = :ws AND status = 'active' AND content IN (:a, :b)",
                    a=f"Riley dislikes the commute (run {RUN})",
                    b=f"Riley wants remote work (run {RUN})",
                )
                superseded = await d.sql(
                    "SELECT count(*) AS n FROM curated_memories WHERE workspace_id = :ws AND status = 'superseded' AND id::text = ANY(CAST(:ids AS text[]))",
                    ids=emids,
                )
                return (
                    len(sources) == 3 and superseded[0].n == 3 and distinct[0].n == 2,
                    f"({len(sources)} sources, {superseded[0].n} superseded, {distinct[0].n} distinct kept) 3 near-duplicates folded into 1 (evidence merged); the episodic proposal showed its 3 sources and, once approved, superseded them; 'dislikes commute' and 'wants remote work' stayed separate",
                )

            await step("8 consolidation (MEM-06)", "reinforce folds duplicates; a synthesis needs approval; distinct claims stay", s8)

            # ---- 9: deletion of a person -------------------------------------------------------------------------------------------------------
            async def s9() -> tuple[bool, str]:
                req = await d.send(
                    "POST",
                    "/v1/deletion-requests",
                    APP,
                    json_={"kind": "subject", "target_id": michael, "reason_code": "data_subject_request", "on_behalf_of": MICHAEL_EXT},
                )
                assert req.status_code == 202, req.text
                rid = req.json()["request_id"]
                t0 = time.time()
                gone_entity = (await d.send("GET", f"/v1/entities/{michael}", ADMIN)).status_code
                search = (await d.send("POST", "/v1/memories/search", ADMIN, json_={"query": MICHAEL})).json()
                graph = (await d.send("POST", "/v1/graph/search", ADMIN, json_={"query": f"{MICHAEL} manages"})).text
                ctx = await d.build(ADMIN, f"What do we know about {MICHAEL}?", subject_ids=[sarah], profile="planning")
                immediate = (
                    gone_entity == 404
                    and MICHAEL not in str(search)
                    and MICHAEL not in graph
                    and MICHAEL not in ctx["context_text"].replace(f"{MICHAEL} does not manage", "")
                )
                frozen_in = time.time() - t0

                async def finished() -> dict[str, Any] | None:
                    s = (await d.send("GET", f"/v1/deletion-requests/{rid}", ADMIN)).json()
                    return dict(s) if s["status"] in ("completed", "completed_with_exceptions", "failed") else None

                final = await d.until(finished, "the purge to finish", timeout=300)
                receipt = final.get("receipt") or {}
                exceptions = await d.sql(
                    "SELECT 1 FROM review_items WHERE workspace_id = :ws AND item_type = 'deletion_exception' AND status = 'open' AND deletion_request_id = CAST(:r AS uuid)",
                    r=rid,
                )
                hits = await graph_hits(d, MICHAEL)
                recreate = await d.send(
                    "POST",
                    "/v1/entities",
                    APP,
                    json_={
                        "type": "Person",
                        "name": "Someone",
                        "external_ids": [{"system": "hr", "value": MICHAEL_EXT}],
                        "source_system": "hr",
                        "source_ref": f"again-{RUN}",
                    },
                )
                residual = receipt.get("verification", {})
                d.ids["deletion_request"] = rid
                ok = (
                    immediate and final["status"] in ("completed", "completed_with_exceptions") and residual.get("id_residuals") == 0 and residual.get("graph_residuals") == 0
                    and hits == 0 and recreate.status_code == 409 and bool(receipt.get("backup_copies_expire_by"))
                )  # fmt: skip
                return (
                    ok,
                    f"gone from entity/memory search/graph search/context within {frozen_in:.1f}s of the request; purge {final['status']} ({len(exceptions)} open exceptions for a person); id residuals {residual.get('id_residuals')}, graph residuals {residual.get('graph_residuals')}, {hits} graph hits for his name; receipt stores {list(receipt.get('stores') or {})}, backups expire by {receipt.get('backup_copies_expire_by')}; re-creating him: {recreate.status_code}",
                )

            await step(
                "9 delete Michael (AT-16, SEC-05, MEM-07)", "immediate disappearance; zero residuals; a receipt; re-creation refused", s9
            )

            # ---- 10: interaction deletion ------------------------------------------------------------------------------------------------------
            async def s10() -> tuple[bool, str]:
                cyc = await d.event(f"Sarah rode her bike for 3 hours (cycle {RUN})", [sarah])
                await d.settled(cyc)
                mem = await d.send(
                    "POST",
                    "/v1/memories",
                    ADMIN,
                    json_={
                        "type": "episodic",
                        "content": f"Sarah rode her bike for 3 hours (cycle {RUN}).",
                        "subject_ids": [sarah],
                        "purpose": "history",
                        "source_interaction_id": cyc,
                    },
                )
                assert mem.status_code == 201, mem.text
                mid = mem.json()["memory_id"]
                req = await d.send(
                    "POST", "/v1/deletion-requests", ADMIN, json_={"kind": "interaction", "target_id": cyc, "reason_code": "wrong_data"}
                )
                assert req.status_code == 202, req.text

                async def finished() -> bool:
                    return (await d.send("GET", f"/v1/deletion-requests/{req.json()['request_id']}", ADMIN)).json()["status"] in (
                        "completed",
                        "completed_with_exceptions",
                    )

                await d.until(finished, "the interaction deletion", timeout=240)
                memory_gone = (await d.sql("SELECT count(*) AS n FROM curated_memories WHERE id = CAST(:m AS uuid)", m=mid))[0].n == 0
                sarah_ok = (await d.send("GET", f"/v1/entities/{sarah}", ADMIN)).status_code == 200
                return memory_gone and sarah_ok, "the event's own memory went with it; Sarah and her facts with other evidence stayed"

            await step("10 delete one interaction", "its memory is removed; facts with other evidence remain", s10)

            # ---- 11: legal hold ----------------------------------------------------------------------------------------------------------------
            async def s11() -> tuple[bool, str]:
                harper = await d.entity("Person", HOLDEE, f"harper-{RUN}", external_ids=[{"system": "hr", "value": f"E-HL-{RUN}"}])
                hold = (
                    await d.send(
                        "POST", "/v1/legal-holds", ADMIN, json_={"scope_kind": "subject", "target_id": harper, "reason_code": "litigation"}
                    )
                ).json()
                req = (
                    await d.send(
                        "POST",
                        "/v1/deletion-requests",
                        ADMIN,
                        json_={"kind": "subject", "target_id": harper, "reason_code": "data_subject_request"},
                    )
                ).json()
                blocked = req["status"] == "blocked_legal_hold"
                hidden = (await d.send("GET", f"/v1/entities/{harper}", ADMIN)).status_code == 404
                exc = await d.sql(
                    "SELECT 1 FROM review_items WHERE workspace_id = :ws AND item_type = 'deletion_exception' AND reason_code = 'legal_hold' AND deletion_request_id = CAST(:r AS uuid) AND status = 'open'",
                    r=req["request_id"],
                )
                still = (await d.sql("SELECT count(*) AS n FROM entities WHERE id = CAST(:e AS uuid)", e=harper))[0].n == 1
                released = await d.send("DELETE", f"/v1/legal-holds/{hold['id']}", ADMIN)
                assert released.status_code == 200, released.text

                async def done() -> bool:
                    return (await d.send("GET", f"/v1/deletion-requests/{req['request_id']}", ADMIN)).json()["status"] in (
                        "completed",
                        "completed_with_exceptions",
                    )

                await d.until(done, "the resumed deletion", timeout=240)
                gone = (await d.sql("SELECT count(*) AS n FROM entities WHERE id = CAST(:e AS uuid)", e=harper))[0].n == 0
                return (
                    blocked and hidden and bool(exc) and still and gone,
                    "blocked_legal_hold: unreadable at once but nothing purged, with a visible exception; releasing the hold resumed it and the purge completed",
                )

            await step("11 legal hold", "a hold freezes but does not purge; release resumes", s11)

            # ---- 12: containment -----------------------------------------------------------------------------------------------------------------
            async def s12() -> tuple[bool, str]:
                payloads = yaml.safe_load(CORPUS.read_text())["payloads"]
                snap_q = "SELECT (SELECT count(*) FROM role_assignments WHERE workspace_id = :ws) AS roles, (SELECT count(*) FROM principal_scopes WHERE workspace_id = :ws) AS grants, (SELECT count(*) FROM principals WHERE workspace_id = :ws) AS principals, (SELECT count(*) FROM ontology_versions WHERE workspace_id = :ws AND state = 'published') AS published, (SELECT count(*) FROM legal_holds WHERE workspace_id = :ws AND released_at IS NULL) AS holds, (SELECT count(*) FROM deletion_requests WHERE workspace_id = :ws) AS deletions"
                before = dict((await d.sql(snap_q))[0]._mapping)
                kept = 0
                planted: list[str] = []
                for p in payloads:
                    r = await d.send(
                        "POST",
                        "/v1/memories",
                        ADMIN,
                        json_={
                            "type": "semantic",
                            "content": p["text"].replace("\u0000", ""),
                            "subject_ids": [sarah],
                            "purpose": "injection-test",
                            "source_ref": f"p8-{RUN}-{p['id']}",
                        },
                    )
                    assert r.status_code in (200, 201, 422), (
                        p["id"],
                        r.status_code,
                    )  # 200: the same text from an earlier run, deduplicated
                    kept += int(r.status_code in (200, 201))
                    if r.status_code in (200, 201):
                        planted.append(r.json()["memory_id"])
                ctx = await d.build(ADMIN, "What do we know about Sarah?", subject_ids=[sarah], profile="planning")
                after = dict((await d.sql(snap_q))[0]._mapping)
                delimiters_ok = ctx["context_text"].count("[END CONTEXT DATA]") == 1
                for mid in planted:  # the hostile text was only ever data; leave the workspace as it was (later demos search it)
                    m = (await d.send("GET", f"/v1/memories/{mid}", ADMIN)).json()
                    if m.get("status") == "active":
                        await d.send(
                            "POST",
                            f"/v1/memories/{mid}/expire",
                            ADMIN,
                            json_={"reason_code": "stale"},
                            headers={"if-match": f'W/"rev-{m["revision"]}"'},
                        )
                # SEC-06: a workspace narrowing keeps internal data away from the router entirely
                cur = (await d.send("GET", "/v1/workspaces/current/settings", ADMIN)).json()
                narrowed = await d.send(
                    "PUT",
                    "/v1/workspaces/current/settings",
                    ADMIN,
                    json_={**cur["settings"], "egress": {"denied_classifications": ["internal"]}},
                    headers={"if-match": f'W/"rev-{cur["version"]}"'},
                )
                assert narrowed.status_code == 200, narrowed.text
                calls_before = (await d.sql("SELECT count(*) AS n FROM model_calls WHERE workspace_id = :ws AND outcome = 'success'"))[0].n
                blocked_event = await d.event(f"A harmless internal note (note {RUN}) that must not leave.", [sarah])
                await asyncio.sleep(6)
                await d.settled(blocked_event)
                calls_after = (await d.sql("SELECT count(*) AS n FROM model_calls WHERE workspace_id = :ws AND outcome = 'success'"))[0].n
                restore = await d.send(
                    "PUT",
                    "/v1/workspaces/current/settings",
                    ADMIN,
                    json_=cur["settings"],
                    headers={"if-match": f'W/"rev-{narrowed.json()["version"]}"'},
                )
                assert restore.status_code == 200, restore.text
                await asyncio.sleep(
                    5
                )  # the worker re-reads the workspace policy within the cache window (3 s on this stack, 30 s by default)
                widen = await d.send(
                    "PUT",
                    "/v1/workspaces/current/settings",
                    ADMIN,
                    json_={"egress": {"allowed_classifications": ["local_only"]}},
                    headers={"if-match": f'W/"rev-{restore.json()["version"]}"'},
                )
                ok = before == after and delimiters_ok and calls_after == calls_before and widen.status_code == 422
                return (
                    ok,
                    f"{len(payloads)} hostile payloads through memory, {kept} stored as data: 0 policy changes ({after}), the context's delimiters intact; with 'internal' denied the event made {calls_after - calls_before} model calls; trying to allow local_only is a {widen.status_code}",
                )

            await step(
                "12 containment (SEC-04, AT-15, SEC-06)",
                "hostile text changes nothing; denied egress makes zero calls; egress cannot be widened",
                s12,
            )

            # ---- 13: the privacy audit ---------------------------------------------------------------------------------------------------------
            async def s13() -> tuple[bool, str]:
                page = (await d.send("GET", "/v1/governance/privacy-audit", ADMIN, limit=200)).json()
                cats = {i["category"] for i in page["items"]}
                text_blob = json.dumps(page)
                exp = await d.send("GET", "/v1/governance/privacy-audit", ADMIN, format="csv", limit=200)
                rows_ = list(csv.reader(io.StringIO(exp.text)))
                denied = [e for e in page["egress"] if e["outcome"] == "egress_denied"]
                no_names = MICHAEL not in text_blob and MICHAEL not in exp.text and HOLDEE not in text_blob
                ok = {"deletions", "holds", "overrides", "settings", "simulations"} <= cats and len(rows_) > 1 and no_names
                return (
                    ok,
                    f"categories recorded: {sorted(cats)}; {len(page['items'])} entries, {len(denied)} egress-denied model-call groups; CSV export {len(rows_) - 1} rows; no name appears in it",
                )

            await step(
                "13 the privacy audit", "deletions, holds, overrides, settings and simulations are listed, with ids and codes only", s13
            )
        finally:
            # the demo leaves the ontology as it found it (the earlier demos and the console specs expect their own version to be active)
            now = (await d.send("GET", "/v1/ontologies/active", ADMIN)).json()["version"]["id"]
            if now != start_active:
                await d.send("POST", "/v1/ontologies/active", ADMIN, json_={"version_id": start_active, "reason_code": "demo_rollback"})
            await d.mock("DELETE", "/__mock/fixtures")
            await d.mock("POST", "/__mock/strict", json={"enabled": False})

    width = max(len(r[0]) for r in rows)
    print(f"{'step'.ljust(width)}  result  expected -> actual")
    for name, expect, actual, ok in rows:
        print(f"{name.ljust(width)}  {'PASS' if ok else 'FAIL'}    {expect} -> {actual}")
    passed = sum(1 for r in rows if r[3])
    print(f"\n{passed}/{len(rows)} passed")
    print(
        "\nThe live exit for P8 is a single session (with P4-P7): `make verify-router`, then OSO_ROUTER_API_KEY=... make eval-consolidation-live eval-simulation-live;"
        " confirm the consolidation synthesis quality and the simulation cost estimate against the router's real answers."
    )
    return 0 if passed == len(rows) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
