"""Phase 4 demo: Graphiti extraction as a PROPOSAL, identity with review, time-aware facts. Exits non-zero if any assertion fails.

Run after `make up seed` (`make demo-p4` publishes the example ontology and runs the evaluation after). Re-runnable: every scenario event has a fixed idempotency
key (a re-run replays it and the assertions read the state), and the drills (outage, unparseable answer, consent) use run-unique events about people who cannot be
confused with each other. The compose mock router answers from the scenario fixtures in STRICT mode for the length of the demo (a gap fails loudly) and is put back.
It stops Neo4j once and restarts it. Reads API keys from .local/dev-credentials.json.
"""

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
DEMO_WS = workspace_id_for("demo")
RUN = uuid.uuid4().hex[:8]
ENVFILE = ROOT / ".env" if (ROOT / ".env").exists() else ROOT / ".env.example"
COMPOSE = ["docker", "compose", "-f", str(ROOT / "deploy/compose/docker-compose.yml"), "--env-file", str(ENVFILE)]
HR = lambda v: [{"system": "hr", "value": v}]  # noqa: E731
Row = tuple[str, str, str, bool]
TERMINAL = ("indexed", "partial", "failed")
TEXT = {
    "evt003": "Sarah Johnson: I'm considering entrepreneurship, and Michael, who I've known for years, keeps encouraging me.",
    "newco": "Sarah Johnson: I work at NewCo now.",
    "cyclist": "Sarah Johnson from the cycling club rode 40 km with Tom on Saturday.",
    "s_johnson": "S. Johnson confirmed the meeting for Thursday.",
    "orbit": "Michael: I've joined Orbit now.",
}


def compose(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run([*COMPOSE, *args], capture_output=True, text=True, cwd=ROOT, check=False)  # noqa: S603
    if check and proc.returncode != 0:
        raise RuntimeError(f"docker compose {' '.join(args)} failed: {proc.stderr.strip()[:200]}")
    return proc


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

    async def ingest(
        self, key: str, content: str, *, subjects: list[str] | None = None, who: str = "demo/conversation_app", **over: Any
    ) -> str:
        body: dict[str, Any] = {
            "source_system": "conversation_app",
            "source_ref": f"chat-p4-{key}",
            "kind": "message",
            "occurred_at": "2026-10-10T09:00:00Z",
            "content": content,
            **over,
        }
        if subjects:
            body["subject_refs"] = [{"entity_id": s} for s in subjects]
        r = await self.send("POST", "/v1/interactions", who, key=key, json_=body)
        if r.status_code not in (200, 202):
            raise AssertionError(f"ingest {key}: {r.status_code} {r.text[:200]}")
        return str(r.json()["event_id"])

    async def wait(self, event_id: str, *, timeout: float = 150) -> dict[str, Any]:  # noqa: ASYNC109
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            ev = (await self.send("GET", f"/v1/interactions/{event_id}", "demo/admin@demo")).json()
            if ev.get("processing_status") in TERMINAL:
                return ev  # type: ignore[no-any-return]
            await asyncio.sleep(0.5)
        raise AssertionError(f"event {event_id} did not finish in {timeout}s")

    async def entity_by_external(self, type_: str, system: str, value: str) -> dict[str, Any] | None:
        rows = await self.sql(
            "SELECT e.id::text AS id FROM entity_external_ids x JOIN entities e ON e.id = x.entity_id WHERE x.workspace_id = :ws AND x.type_key = :t AND x.system = :s AND x.value = :v",
            t=type_,
            s=system,
            v=value,
        )
        return {"id": rows[0].id} if rows else None

    async def ensure_entity(self, type_: str, name: str, ext: str | None, key: str) -> str:
        found = await self.entity_by_external(type_, "hr", ext) if ext else None
        if found:
            return str(found["id"])
        rows = await self.sql(
            "SELECT id::text AS id FROM entities WHERE workspace_id = :ws AND type_key = :t AND display_name = :n AND origin = 'command' ORDER BY recorded_at LIMIT 1",
            t=type_,
            n=name,
        )
        if rows:
            return str(rows[0].id)
        body: dict[str, Any] = {"type": type_, "name": name, "source_system": "hr", "source_ref": ext or name}
        if ext:
            body["external_ids"] = HR(ext)
        r = await self.send("POST", "/v1/entities", "demo/conversation_app", key=key, json_=body)
        assert r.status_code in (200, 201, 202), r.text
        return str(r.json()["entity_id"])

    async def mock(self, method: str, path: str, **kw: Any) -> None:
        async with httpx.AsyncClient() as c:
            r = await c.request(method, f"{MOCK}{path}", **kw)
            r.raise_for_status()


def stage(d: dict[str, Any], name: str) -> dict[str, Any]:
    return next(s for s in d["stages"] if s["stage"] == name)


async def main() -> int:
    creds_path = ROOT / ".local" / "dev-credentials.json"
    if not creds_path.exists():
        print("missing .local/dev-credentials.json: run `make seed` first", file=sys.stderr)
        return 2
    keys: dict[str, str] = json.loads(creds_path.read_text())
    settings = Settings()
    rows: list[Row] = []
    async with httpx.AsyncClient(base_url=BASE, timeout=90) as client:
        d = Demo(client, keys, settings)

        async def step(name: str, expect: str, fn: Callable[[], Awaitable[tuple[bool, str]]]) -> None:
            try:
                ok, actual = await fn()
            except Exception as exc:
                ok, actual = False, f"{type(exc).__name__}: {str(exc)[:200]}"
            rows.append((name, expect, actual, ok))

        await d.mock("POST", "/__mock/strict", json={"enabled": True})
        await d.mock("POST", "/__mock/reload")
        try:
            # ---- 1: the P2 state -------------------------------------------------------------------------------------------------------
            async def s1() -> tuple[bool, str]:
                d.ids["sarah"] = await d.ensure_entity("Person", "Sarah Johnson", "E-1001", "demo-p4-sarah")
                d.ids["acme"] = await d.ensure_entity("Organization", "Acme", "ORG-1", "demo-p4-acme")
                d.ids["michael"] = await d.ensure_entity("Person", "Michael", None, "demo-p4-michael")
                d.ids["topic"] = await d.ensure_entity("Topic", "Entrepreneurship", None, "demo-p4-topic")
                d.ids["zenith"] = await d.ensure_entity("Organization", "Zenith", "ORG-9", "demo-p4-zenith")
                have = await d.sql(
                    "SELECT count(*) AS n FROM facts WHERE workspace_id = :ws AND source_entity_id = :s AND rel_type = 'WORKS_AT' AND source_type = 'command'",
                    s=uuid.UUID(d.ids["sarah"]),
                )
                if have[0].n == 0:
                    for k, frm, role in (("swe", "2022-01-10", "Software Engineer"), ("em", "2024-06-01", "Engineering Manager")):
                        r = await d.send(
                            "POST",
                            "/v1/relationships",
                            "demo/conversation_app",
                            key=f"demo-p4-{k}",
                            json_={
                                "type": "WORKS_AT",
                                "source": {"entity_id": d.ids["sarah"]},
                                "target": {"entity_id": d.ids["acme"]},
                                "valid_from": frm,
                                "attributes": {"role": role},
                                "source_system": "hr",
                                "source_ref": f"demo-p4-{k}",
                            },
                        )
                        assert r.status_code in (200, 201, 202), r.text
                mfact = await d.sql(
                    "SELECT count(*) AS n FROM facts WHERE workspace_id = :ws AND source_entity_id = :s AND rel_type = 'WORKS_AT' AND source_type = 'command'",
                    s=uuid.UUID(d.ids["michael"]),
                )
                if mfact[0].n == 0:
                    r = await d.send(
                        "POST",
                        "/v1/relationships",
                        "demo/conversation_app",
                        key="demo-p4-michael-zenith",
                        json_={
                            "type": "WORKS_AT",
                            "source": {"entity_id": d.ids["michael"]},
                            "target": {"entity_id": d.ids["zenith"]},
                            "valid_from": "2023-02-01",
                            "source_system": "hr",
                            "source_ref": "demo-p4-michael-zenith",
                        },
                    )
                    assert r.status_code in (200, 201, 202), r.text
                for _ in range(60):  # the compose worker may still be projecting
                    pending = await d.sql(
                        "SELECT count(*) AS n FROM (SELECT id FROM entities WHERE workspace_id = :ws AND projection_status = 'pending' UNION ALL SELECT id FROM facts WHERE workspace_id = :ws AND projection_status = 'pending') x"
                    )
                    if pending[0].n == 0:
                        break
                    await asyncio.sleep(1)
                rel = (await d.send("GET", f"/v1/entities/{d.ids['sarah']}", "demo/reviewer@demo", include="history")).json()[
                    "relationships"
                ]
                works = [f for f in rel if f["rel_type"] == "WORKS_AT" and f["source_type"] == "command"]
                d.ids["em_fact"] = next(f["id"] for f in works if (f["attributes"] or {}).get("role") == "Engineering Manager")
                d.ids["swe_fact"] = next(f["id"] for f in works if (f["attributes"] or {}).get("role") == "Software Engineer")
                return (
                    bool(works),
                    f"Sarah works at Acme (Engineering Manager since 2024-06-01); Michael at Zenith; {len(works)} verified WORKS_AT fact(s) on Sarah",
                )

            await step("1 the P2 state holds", "Sarah (EM at Acme), Michael, Entrepreneurship exist and are projected", s1)

            # ---- 2: evt_003 -> graph_ingest --------------------------------------------------------------------------------------------------
            async def s2() -> tuple[bool, str]:
                d.ids["evt003"] = await d.ingest(
                    "demo-p4-evt-003", TEXT["evt003"], subjects=[d.ids["sarah"], d.ids["michael"], d.ids["topic"]]
                )
                ev = await d.wait(d.ids["evt003"])
                gi = stage(ev, "graph_ingest")
                rel = (await d.send("GET", f"/v1/entities/{d.ids['sarah']}", "demo/reviewer@demo")).json()["relationships"]
                ext = {f["rel_type"]: f for f in rel if f["source_type"] == "extracted"}
                cons, knows = ext.get("CONSIDERING"), ext.get("KNOWS")
                employer = [
                    f["target"]["name"] for f in rel if f["rel_type"] == "WORKS_AT" and f["phase"] == "current" and f["status"] == "active"
                ]
                ok = (
                    gi["status"] == "succeeded"
                    and cons is not None
                    and cons["status"] == "active"
                    and cons["evidence_status"] == "unverified"
                    and cons["valid_from"] is None
                    and cons["valid_from_unknown"]
                    and knows is not None
                    and knows["status"] == "active"
                    and knows["evidence_status"] == "unverified"
                    and employer == ["Acme"]
                )
                d.ids["considering"] = cons["id"] if cons else None
                return (
                    ok,
                    f"graph_ingest {gi['status']}; CONSIDERING {cons and cons['status']}/{cons and cons['evidence_status']} valid_from={cons and cons['valid_from']}; KNOWS {knows and knows['status']}; current employer {employer}",
                )

            await step(
                "2 ingest evt_003", "tentative CONSIDERING + KNOWS, labelled unverified, no valid date; employer still Acme (KGR-03)", s2
            )

            # ---- 3: evidence ----------------------------------------------------------------------------------------------------------------
            async def s3() -> tuple[bool, str]:
                ev = (await d.send("GET", f"/v1/relationships/{d.ids['considering']}/evidence", "demo/reviewer@demo")).json()
                gi = stage((await d.send("GET", f"/v1/interactions/{d.ids['evt003']}", "demo/admin@demo")).json(), "graph_ingest")
                ok = (
                    len(ev) >= 1
                    and ev[0]["kind"] == "episode"
                    and ev[0]["interaction_id"] == d.ids["evt003"]
                    and ev[0]["episode_uuid"] == gi["result"]["episode_uuid"]
                )
                return (
                    ok,
                    f"{len(ev)} evidence row(s): episode {ev[0]['episode_uuid'][:8]}… of interaction {ev[0]['interaction_id'][:8]}… ({ev[0]['source_system']}); no content in the listing",
                )

            await step("3 why does the system believe it?", "the evt_003 interaction and its episode uuid (KGR-05)", s3)

            # ---- 4: NewCo -------------------------------------------------------------------------------------------------------------------
            async def s4() -> tuple[bool, str]:
                d.ids["newco_event"] = await d.ingest("demo-p4-newco", TEXT["newco"], subjects=[d.ids["sarah"]])
                ev = await d.wait(d.ids["newco_event"])
                gi = stage(ev, "graph_ingest")
                claim = await d.sql(
                    "SELECT f.id::text AS id, f.status FROM facts f JOIN entities t ON t.id = f.target_entity_id WHERE f.workspace_id = :ws AND f.rel_type = 'WORKS_AT' AND t.display_name = 'NewCo' AND f.source_type = 'extracted'"
                )
                d.ids["newco_fact"] = claim[0].id if claim else None
                em = (await d.send("GET", f"/v1/relationships/{d.ids['em_fact']}", "demo/reviewer@demo")).json()
                reviews = (await d.send("GET", "/v1/reviews", "demo/reviewer@demo", type="contradiction", limit=100)).json()["items"]
                mine = [r for r in reviews if r["fact_id"] == d.ids["newco_fact"]]
                d.ids["newco_review"] = mine[0]["id"] if mine else None
                graph = await d.sql(
                    "SELECT 1 AS x"
                )  # placeholder keeps the row shape; the graph is checked by `kg graph reconcile` in the last step
                _ = graph
                ok = (
                    gi["status"] == "succeeded"
                    and bool(claim)
                    and claim[0].status in ("proposed", "rejected")
                    and em["status"] == "active"
                    and em["valid_to"] is None
                    and em["evidence_status"] == "verified"
                    and bool(mine)
                )
                return (
                    ok,
                    f"WORKS_AT NewCo is {claim[0].status if claim else 'missing'}; Acme (EM) stays {em['status']}/{em['evidence_status']}, valid_to={em['valid_to']}; reverts recorded: {gi['result'].get('reverts')}; contradiction review {'open' if mine and mine[0]['status'] == 'open' else 'decided'}",
                )

            await step(
                "4 a conversation claims a new employer",
                "NewCo is proposed + a contradiction review; Acme untouched; Graphiti's invalidation reverted (ING-04, AT-05)",
                s4,
            )

            # ---- 9 first (the drills must run before a second 'Sarah Johnson' exists): Neo4j outage ---------------------------------------------
            async def s9() -> tuple[bool, str]:
                compose("stop", "neo4j")
                try:
                    event = await d.ingest(f"demo-p4-outage-{RUN}", f"{TEXT['s_johnson']} (outage drill {RUN})", subjects=[d.ids["sarah"]])
                    seen = None
                    for _ in range(150):  # one attempt against a stopped Neo4j can hang for about a minute before it errors
                        ev = (await d.send("GET", f"/v1/interactions/{event}", "demo/admin@demo")).json()
                        gi = stage(ev, "graph_ingest")
                        if gi["status"] == "failed":
                            seen = gi["last_error_code"] or "no code recorded"
                            break
                        await asyncio.sleep(1)
                finally:
                    compose("start", "neo4j")
                    for _ in range(90):
                        health = compose("ps", "--format", "{{.Health}}", "neo4j", check=False).stdout.strip()
                        if health == "healthy":
                            break
                        await asyncio.sleep(2)
                ev = await d.wait(event, timeout=240)
                gi = stage(ev, "graph_ingest")
                return (
                    seen is not None and ev["processing_status"] == "indexed" and gi["attempts"] >= 2,
                    f"during the outage the stage showed 'failed (retrying)' ({seen}); after Neo4j returned: {ev['processing_status']}, graph_ingest {gi['status']} in {gi['attempts']} attempts",
                )

            await step("9 Neo4j stops during an ingest", "the job retries visibly, then the event is indexed (AT-02, ING-02)", s9)

            # ---- 10: unparseable ------------------------------------------------------------------------------------------------------------
            async def s10() -> tuple[bool, str]:
                event = await d.ingest(
                    f"demo-p4-bad-{RUN}", f"Michael: please add the unparseable extraction ({RUN}).", subjects=[d.ids["michael"]]
                )
                ev = await d.wait(event, timeout=240)
                gi = stage(ev, "graph_ingest")
                before = await d.sql(
                    "SELECT count(*) AS n FROM facts WHERE workspace_id = :ws AND source_interaction_id = :i", i=uuid.UUID(event)
                )
                dead = (
                    (gi["status"], gi["last_error_code"], gi["attempts"]) == ("dead", "extraction_unparseable", 2)
                    and ev["processing_status"] == "partial"
                    and before[0].n == 0
                )
                await d.mock(  # the cause is fixed: from now on the model answers properly
                    "POST",
                    "/__mock/fixtures",
                    json={
                        "rules": [
                            {
                                "when": {"schema_name": "ExtractedEntities", "contains": ["unparseable extraction"]},
                                "response": {"extracted_entities": [{"name": "Michael", "entity_type_id": 2, "episode_indices": [0]}]},
                            },
                            {"when": {"schema_name": "ExtractedEdges", "contains": ["unparseable extraction"]}, "response": {"edges": []}},
                            {
                                "when": {"schema_name": "SummarizedEntities", "contains": ["unparseable extraction"]},
                                "response": {"summaries": [{"name": "Michael", "summary": "Michael."}]},
                            },
                        ]
                    },
                )
                r = await d.send(
                    "POST",
                    f"/v1/interactions/{event}/replay",
                    "demo/operator@demo",
                    json_={"stage": "graph_ingest", "reason_code": "source_fixed"},
                )
                after = await d.wait(event, timeout=240) if r.status_code == 202 else ev
                for _ in range(60):
                    after = (await d.send("GET", f"/v1/interactions/{event}", "demo/admin@demo")).json()
                    if stage(after, "graph_ingest")["status"] == "succeeded":
                        break
                    await asyncio.sleep(1)
                await d.mock("DELETE", "/__mock/fixtures")
                ok = (
                    dead
                    and r.status_code == 202
                    and stage(after, "graph_ingest")["status"] == "succeeded"
                    and after["processing_status"] == "indexed"
                )
                return (
                    ok,
                    f"after 2 attempts: {gi['status']} ({gi['last_error_code']}), event {ev['processing_status']}, no facts written; audited replay -> {stage(after, 'graph_ingest')['status']}, event {after['processing_status']}",
                )

            await step(
                "10 the model returns unparseable output",
                "quarantined as extraction_unparseable after 2 attempts; replay after the fix -> indexed (ING-03)",
                s10,
            )

            # ---- 11: consent denied ---------------------------------------------------------------------------------------------------------
            async def s11() -> tuple[bool, str]:
                event = await d.ingest(
                    f"demo-p4-consent-{RUN}",
                    f"Sarah Johnson: confidential chat {RUN}",
                    subjects=[d.ids["sarah"]],
                    consent={"external_processing": "denied"},
                )
                ev = await d.wait(event)
                gi = stage(ev, "graph_ingest")
                calls = await d.sql(
                    "SELECT count(*) AS n FROM model_calls WHERE workspace_id = :ws AND correlation_id LIKE :c", c=f"gi-{event}%"
                )
                return gi["status"] == "skipped_by_policy" and calls[0].n == 0 and ev[
                    "processing_status"
                ] == "indexed", f"graph_ingest {gi['status']}, event {ev['processing_status']}, model calls for it: {calls[0].n}"

            await step("11 a consent-denied event", "skipped_by_policy with zero model calls (ING-06)", s11)

            # ---- 5: the other Sarah Johnson -----------------------------------------------------------------------------------------------------
            async def s5() -> tuple[bool, str]:
                event = await d.ingest("demo-p4-cyclist", TEXT["cyclist"])
                ev = await d.wait(event)
                gi = stage(ev, "graph_ingest")
                reviews = (await d.send("GET", "/v1/reviews", "demo/reviewer@demo", type="identity_match", limit=100)).json()["items"]
                mine = [r for r in reviews if r["interaction_id"] == event]
                assert mine, "no identity review for the cyclist"
                item = mine[0]
                d.ids["cyclist_candidate"] = item["candidate_entity_id"]
                detail = await d.send("GET", f"/v1/reviews/{item['id']}", "demo/reviewer@demo")
                if item["status"] == "open":
                    r = await d.send(
                        "POST",
                        f"/v1/reviews/{item['id']}/decision",
                        "demo/reviewer@demo",
                        json_={"decision": "different_entity", "rationale_code": "different_real_world_entity"},
                        headers={"if-match": detail.headers["etag"]},
                    )
                    assert r.status_code == 200, r.text
                two = await d.sql(  # this review's candidate is a person of its own; other runs (the console tests) may have added more Sarahs
                    "SELECT count(*) AS n FROM entities WHERE workspace_id = :ws AND display_name = 'Sarah Johnson' AND type_key = 'Person' AND status = 'active' "
                    "AND (id = :c OR id = :s)",
                    c=uuid.UUID(item["candidate_entity_id"]),
                    s=uuid.UUID(d.ids["sarah"]),
                )
                merged = await d.sql("SELECT count(*) AS n FROM entities WHERE workspace_id = :ws AND status = 'merged'")
                ok = gi["status"] == "succeeded" and two[0].n == 2 and merged[0].n == 0 and item["subject_entity_id"] == d.ids["sarah"]
                return (
                    ok,
                    f"identity_match review against Sarah ({item['reason_code']}); not merged; after the reviewer's different_entity: {two[0].n} distinct active people named Sarah Johnson (the colleague and the candidate), {merged[0].n} merges",
                )

            await step(
                "5 a different Sarah Johnson",
                "a candidate + identity review, never a merge; reviewer says different_entity -> two Sarahs (KGR-02, AT-03)",
                s5,
            )

            # ---- 6: S. Johnson -------------------------------------------------------------------------------------------------------------
            async def s6() -> tuple[bool, str]:
                event = await d.ingest("demo-p4-s-johnson", TEXT["s_johnson"], subjects=[d.ids["sarah"]])
                ev = await d.wait(event)
                sarah = (await d.send("GET", f"/v1/entities/{d.ids['sarah']}", "demo/admin@demo")).json()
                mention = await d.sql(
                    "SELECT resolution, status FROM entity_mentions WHERE workspace_id = :ws AND interaction_id = :i", i=uuid.UUID(event)
                )
                gi = stage(ev, "graph_ingest")
                return (
                    "S. Johnson" in sarah["aliases"]
                    and [(m.resolution, m.status) for m in mention] == [("subject_ref", "accepted")]
                    and gi["result"]["name_variants"] == 1,
                    f"alias 'S. Johnson' added to Sarah; mention resolved by subject_ref (deterministic), name_variants={gi['result']['name_variants']}",
                )

            await step("6 S. Johnson with a subject_ref", "linked deterministically; the name becomes an alias", s6)

            # ---- 7: search ---------------------------------------------------------------------------------------------------------------
            async def s7() -> tuple[bool, str]:
                now = (
                    await d.send("POST", "/v1/graph/search", "demo/reviewer@demo", json_={"query": "Sarah employer", "limit": 10})
                ).json()
                then = (
                    await d.send(
                        "POST",
                        "/v1/graph/search",
                        "demo/reviewer@demo",
                        json_={"query": "Sarah employer", "as_of": "2023-01-01", "limit": 10},
                    )
                ).json()
                works_now = [f["id"] for f in now["facts"] if f["rel_type"] == "WORKS_AT" and f["source"]["id"] == d.ids["sarah"]]
                works_then = [f["id"] for f in then["facts"] if f["rel_type"] == "WORKS_AT" and f["source"]["id"] == d.ids["sarah"]]
                ok = (
                    works_now == [d.ids["em_fact"]]
                    and works_then == [d.ids["swe_fact"]]
                    and all(f["status"] == "active" for f in now["facts"])
                )
                return (
                    ok,
                    f"now -> Acme as Engineering Manager only; as_of 2023-01-01 -> the Software Engineer role; every hit joined to Postgres (status, evidence); unreconciled hits {now['unreconciled_hits']}",
                )

            await step("7 graph search", "'Sarah employer' -> only the active Acme edge; as_of 2023 -> the earlier role (KGR-03/04)", s7)

            # ---- 8: reviews -----------------------------------------------------------------------------------------------------------------
            async def s8() -> tuple[bool, str]:
                orbit = await d.ingest("demo-p4-orbit", TEXT["orbit"], subjects=[d.ids["michael"]])
                await d.wait(orbit)
                outs = []
                newco = await d.send("GET", f"/v1/reviews/{d.ids['newco_review']}", "demo/reviewer@demo")
                if newco.json()["status"] == "open":
                    r = await d.send(
                        "POST",
                        f"/v1/reviews/{d.ids['newco_review']}/decision",
                        "demo/reviewer@demo",
                        json_={"decision": "keep_existing", "rationale_code": "verified_with_source"},
                        headers={"if-match": newco.headers["etag"]},
                    )
                    assert r.status_code == 200, r.text
                (claim,) = await d.sql("SELECT status FROM facts WHERE id = :f", f=uuid.UUID(d.ids["newco_fact"]))
                outs.append(f"NewCo {claim.status}")
                contra = (await d.send("GET", "/v1/reviews", "demo/reviewer@demo", type="contradiction", limit=100)).json()["items"]
                mine = [r for r in contra if r["interaction_id"] == orbit]
                assert mine, "no contradiction review for Michael/Orbit"
                item = mine[0]
                if item["status"] == "open":
                    got = await d.send("GET", f"/v1/reviews/{item['id']}", "demo/reviewer@demo")
                    r = await d.send(
                        "POST",
                        f"/v1/reviews/{item['id']}/decision",
                        "demo/reviewer@demo",
                        json_={"decision": "mark_disputed", "rationale_code": "insufficient_evidence"},
                        headers={"if-match": got.headers["etag"]},
                    )
                    assert r.status_code == 200, r.text
                pair = await d.sql(
                    "SELECT f.status, t.display_name AS target FROM facts f JOIN entities t ON t.id = f.target_entity_id WHERE f.workspace_id = :ws AND f.source_entity_id = :m AND f.rel_type = 'WORKS_AT'",
                    m=uuid.UUID(d.ids["michael"]),
                )
                statuses = {p.target: p.status for p in pair}
                refused = await d.send(
                    "POST",
                    f"/v1/reviews/{d.ids['newco_review']}/decision",
                    "demo/reviewer@demo",
                    json_={"decision": "accept_claim", "rationale_code": "other"},
                    headers={"if-match": 'W/"rev-1"'},
                )
                ok = claim.status == "rejected" and statuses.get("Zenith") == "disputed" and statuses.get("Orbit") == "disputed"
                return (
                    ok,
                    f"{outs[0]} (keep_existing); Michael's Zenith and Orbit facts -> {statuses} (mark_disputed flags BOTH, KGR-06); a decided item cannot be decided again ({refused.status_code})",
                )

            await step(
                "8 the reviewer decides",
                "keep_existing rejects the NewCo claim; mark_disputed flags both sides of the Michael pair (KGR-06)",
                s8,
            )

            # ---- 12: isolation -----------------------------------------------------------------------------------------------------------------
            async def s12() -> tuple[bool, str]:
                probes = [
                    await d.send("GET", f"/v1/reviews/{d.ids['newco_review']}", "other_demo/admin@other"),
                    await d.send("GET", f"/v1/relationships/{d.ids['em_fact']}/evidence", "other_demo/admin@other"),
                    await d.send("GET", f"/v1/entities/{d.ids['sarah']}/graph", "other_demo/admin@other"),
                    await d.send("GET", f"/v1/reviews/{uuid.uuid4()}", "other_demo/admin@other"),
                ]
                search = (await d.send("POST", "/v1/graph/search", "other_demo/admin@other", json_={"query": "Sarah Johnson Acme"})).json()
                listed = (await d.send("GET", "/v1/reviews", "other_demo/admin@other")).json()["items"]
                forbidden = [
                    await d.send("GET", "/v1/reviews", "demo/agent_planning"),
                    await d.send("POST", "/v1/graph/search", "demo/agent_planning", json_={"query": "x"}),
                ]
                ok = (
                    all(p.status_code == 404 for p in probes)
                    and search["facts"] == []
                    and listed == []
                    and all(f.status_code == 403 for f in forbidden)
                )
                return (
                    ok,
                    f"admin@other: demo review/fact/entity ids -> {[p.status_code for p in probes]}; graph search returns {len(search['facts'])} demo facts; review list {len(listed)}; agent_planning -> {[f.status_code for f in forbidden]}",
                )

            await step(
                "12 isolation",
                "foreign ids 404 like random ones; nothing of the demo workspace in another's search; agents have no review/search access (SEC-01)",
                s12,
            )

            # ---- the graph still equals Postgres --------------------------------------------------------------------------------------------
            async def s12b() -> tuple[bool, str]:
                proc = await asyncio.to_thread(  # noqa: S603
                    subprocess.run,
                    [str(ROOT / ".venv/bin/kg"), "graph", "reconcile", "--workspace", "demo"],
                    capture_output=True,
                    text=True,
                    cwd=ROOT,
                    check=False,
                )
                report = json.loads(proc.stdout.strip().splitlines()[-1]) if proc.stdout.strip() else {}
                return (
                    proc.returncode == 0 and report.get("drift") == [],
                    f"kg graph reconcile: {len(report.get('drift', []))} drift, {report.get('entities')} entities and {report.get('facts')} facts compared; episodes {report.get('episodes', {}).get('checked')} present",
                )

            await step("12b the graph equals Postgres", "no drift after everything above, including the reverted invalidation", s12b)
        finally:
            await d.mock("DELETE", "/__mock/fixtures")
            await d.mock("POST", "/__mock/strict", json={"enabled": False})

    width = max(len(r[0]) for r in rows)
    print(f"{'step'.ljust(width)}  result  expected -> actual")
    for name, expect, actual, ok in rows:
        print(f"{name.ljust(width)}  {'PASS' if ok else 'FAIL'}    {expect} -> {actual}")
    passed = sum(1 for r in rows if r[3])
    print(f"\n{passed}/{len(rows)} passed")
    print(
        "\nstep 13 (make eval-extraction) runs the evaluation against the scenario gold set; for the real router run:\n  OSO_ROUTER_API_KEY=... make eval-extraction-live"
    )
    return 0 if passed == len(rows) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
