"""Phase 2 demo: a configurable ontology and deterministic, auditable, zero-LLM writes. Exits non-zero if any assertion fails.

Run after `make up seed` (or `make demo-p2`). Re-runnable: every write uses a fixed Idempotency-Key and ontology versions are looked up first, so a second run
replays what was done and re-checks the end state. For the full first-run output start from `make reset up seed demo-p2`.
Reads API keys from .local/dev-credentials.json.
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
import neo4j
from kg_cli.seed import workspace_id_for
from kg_common.settings import Settings
from kg_db.engine import create_app_engine
from kg_db.session import tenant_session
from kg_domain.principal import WorkspaceContext
from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[1]
BASE = os.environ.get("KG_API_URL", "http://127.0.0.1:8000")
DEMO_WS = workspace_id_for("demo")
YAML_PATH = ROOT / "examples" / "ontologies" / "personal-knowledge.yaml"
HR = lambda v: [{"system": "hr", "value": v}]  # noqa: E731
Row = tuple[str, str, str, bool]


def bearer(key: str, **extra: str) -> dict[str, str]:
    return {"authorization": f"Bearer {key}", **extra}


def strip(r: httpx.Response) -> dict[str, Any]:
    return {k: v for k, v in r.json()["error"].items() if k != "request_id"}


class Demo:
    def __init__(self, client: httpx.AsyncClient, keys: dict[str, str], settings: Settings):
        self.c, self.keys, self.settings = client, keys, settings
        self.ids: dict[str, Any] = {}

    def h(self, who: str, **extra: str) -> dict[str, str]:
        return bearer(self.keys[who], **extra)

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
        hdrs = self.h(who, **(headers or {}))
        if method != "GET":
            hdrs["idempotency-key"] = key or str(uuid.uuid4())
        return await self.c.request(method, path, headers=hdrs, json=json_, params=params or None)

    async def versions(self) -> list[dict[str, Any]]:
        return (await self.send("GET", "/v1/ontologies/versions", "demo/admin@demo", limit=100)).json()["items"]  # type: ignore[no-any-return]

    async def version(self, number: str) -> dict[str, Any] | None:
        return next((v for v in await self.versions() if v["version"] == number), None)


async def main() -> int:
    creds_path = ROOT / ".local" / "dev-credentials.json"
    if not creds_path.exists():
        print("missing .local/dev-credentials.json: run `make seed` first", file=sys.stderr)
        return 2
    keys: dict[str, str] = json.loads(creds_path.read_text())
    settings = Settings()
    rows: list[Row] = []
    yaml_text = YAML_PATH.read_text()

    async with httpx.AsyncClient(base_url=BASE, timeout=60) as client:
        d = Demo(client, keys, settings)

        async def step(name: str, expect: str, fn: Callable[[], Awaitable[tuple[bool, str]]]) -> None:
            try:
                ok, actual = await fn()
            except Exception as exc:
                ok, actual = False, f"{type(exc).__name__}: {str(exc)[:120]}"
            rows.append((name, expect, actual, ok))

        # ---- 1-2: the ontology registry ---------------------------------------------------------------------------------------------------
        async def s1() -> tuple[bool, str]:
            existing = await d.version("1.0.0")
            if existing and existing["state"] != "draft":
                return True, "v1.0.0 already published (skipped; ONT-01 is covered by the first run)"
            bad = yaml_text.replace(
                '    target: Organization\n    temporal: true\n    fact_template: "{source} works at',
                '    target: Company\n    temporal: true\n    fact_template: "{source} works at',
                1,
            )
            if existing:  # a draft left by an interrupted run
                r = await d.send(
                    "PUT",
                    f"/v1/ontologies/versions/{existing['id']}",
                    "demo/admin@demo",
                    json_={"content": bad},
                    headers={"If-Match": f'W/"rev-{existing["revision"]}"'},
                )
            else:
                r = await d.send("POST", "/v1/ontologies/versions", "demo/admin@demo", key="demo-p2-draft-bad", json_={"content": bad})
            errs = {e["path"]: e["code"] for e in r.json()["validation_report"]["errors"]}
            d.ids["draft"] = r.json()["id"]
            d.ids["draft_etag"] = r.headers["etag"]
            return errs == {"relationship_types.WORKS_AT.target": "unknown_entity_type"}, f"{r.status_code} errors at {list(errs)}"

        async def s2() -> tuple[bool, str]:
            v = await d.version("1.0.0")
            assert v is not None
            if v["state"] == "draft":
                fixed = await d.send(
                    "PUT",
                    f"/v1/ontologies/versions/{v['id']}",
                    "demo/admin@demo",
                    json_={"content": yaml_text},
                    headers={"If-Match": d.ids["draft_etag"]},
                )
                assert fixed.json()["validation_report"]["errors"] == [], fixed.text
                pub = await d.send(
                    "POST",
                    f"/v1/ontologies/versions/{v['id']}/publish",
                    "demo/admin@demo",
                    key="demo-p2-publish-1",
                    json_={"notes": "demo release"},
                )
                assert pub.status_code == 200, pub.text
                v = (await d.send("GET", f"/v1/ontologies/versions/{v['id']}", "demo/admin@demo")).json()
            d.ids["v1"] = v["id"]
            full = (await d.send("GET", f"/v1/ontologies/versions/{v['id']}", "demo/admin@demo")).json()
            put = await d.send(
                "PUT",
                f"/v1/ontologies/versions/{v['id']}",
                "demo/admin@demo",
                json_={"content": yaml_text},
                headers={"If-Match": f'W/"rev-{full["revision"]}"'},
            )
            audit = (await d.send("GET", "/v1/audit", "demo/admin@demo", limit=100, action="ontology.published")).json()["items"]
            logged = any(a["action"] == "ontology.published" and a["metadata"].get("content_hash") == full["content_hash"] for a in audit)
            ok = (
                full["state"] == "published"
                and len(full["content_hash"]) == 64
                and put.status_code == 409
                and put.json()["error"]["code"] == "immutable_version"
                and logged
            )
            return (
                ok,
                f"published, hash {full['content_hash'][:12]}…, PUT -> {put.status_code} {put.json()['error']['code']}, audit entry={logged}",
            )

        # ---- 3-8: deterministic commands --------------------------------------------------------------------------------------------------
        async def s3() -> tuple[bool, str]:
            made = {}
            for label, body in {
                "sarah": {
                    "type": "Person",
                    "name": "Sarah Johnson",
                    "external_ids": HR("E-1001"),
                    "attributes": {"birth_date": "1985-03-02"},
                    "source_system": "hr",
                    "source_ref": "E-1001",
                },
                "acme": {"type": "Organization", "name": "Acme", "external_ids": HR("ORG-1"), "source_system": "hr", "source_ref": "ORG-1"},
                "michael": {"type": "Person", "name": "Michael", "source_system": "conversation_app", "source_ref": "chat-2091"},
                "topic": {"type": "Topic", "name": "Entrepreneurship", "source_system": "conversation_app", "source_ref": "chat-2091"},
            }.items():
                r = await d.send("POST", "/v1/entities", "demo/conversation_app", key=f"demo-p2-{label}", json_=body)
                assert r.status_code in (200, 201), (label, r.status_code, r.text)
                made[label] = r.json()
                d.ids[label] = r.json()["entity_id"]
            repeat = await d.send(
                "POST",
                "/v1/entities",
                "demo/conversation_app",
                key="demo-p2-sarah-again",
                json_={"type": "Person", "name": "Sarah J.", "external_ids": HR("E-1001")},
            )
            ok = repeat.status_code == 200 and repeat.json()["matched_existing"] and repeat.json()["entity_id"] == d.ids["sarah"]
            return (
                ok,
                f"4 entities ({', '.join(str(m['status']) for m in made.values())}); repeat with hr:E-1001 -> {repeat.status_code} matched_existing={repeat.json().get('matched_existing')}",
            )

        async def assert_fact(ref: str, start: str, role: str, **extra: Any) -> dict[str, Any]:
            r = await d.send(
                "POST",
                "/v1/relationships",
                "demo/conversation_app",
                key=f"demo-p2-{ref}",
                json_={
                    "type": "WORKS_AT",
                    "source": {"entity_id": d.ids["sarah"]},
                    "target": {"entity_id": d.ids["acme"]},
                    "valid_from": start,
                    "attributes": {"role": role},
                    "source_system": "hr",
                    "source_ref": ref,
                    **extra,
                },
            )
            assert r.status_code in (200, 201, 202), r.text
            return r.json()  # type: ignore[no-any-return]

        async def s4() -> tuple[bool, str]:
            r = await assert_fact("evt_001", "2022-01-10", "Software Engineer")
            d.ids["fact1"] = r["fact_id"]
            return r["status"] == "applied" and r["fact_id"] is not None, f"WORKS_AT {r['status']}, fact {r['fact_id'][:8]}…"

        async def s5() -> tuple[bool, str]:
            r = await assert_fact("evt_002", "2024-06-01", "Engineering Manager", source_recorded_at="2024-06-10")
            d.ids["fact2"] = r["fact_id"]
            first = (await d.send("GET", f"/v1/relationships/{d.ids['fact1']}", "demo/agent_planning")).json()
            ok = r["ended_fact_ids"] == [d.ids["fact1"]] and first["valid_to"].startswith("2024-06-01") and first["status"] == "active"
            return ok, f"predecessor ended at {first['valid_to'][:10]}, still status={first['status']} (succession, not supersession)"

        async def s6() -> tuple[bool, str]:
            now = (await d.send("GET", f"/v1/entities/{d.ids['sarah']}", "demo/agent_planning")).json()["relationships"]
            past = (await d.send("GET", f"/v1/entities/{d.ids['sarah']}", "demo/agent_planning", as_of="2023-01-01")).json()[
                "relationships"
            ]
            hist = (await d.send("GET", f"/v1/entities/{d.ids['sarah']}", "demo/agent_planning", include="history")).json()["relationships"]
            second = next(f for f in hist if f["id"] == d.ids["fact2"])
            ok = (
                [f["attributes"]["role"] for f in now] == ["Engineering Manager"]
                and [f["attributes"]["role"] for f in past] == ["Software Engineer"]
                and [f["attributes"]["role"] for f in hist] == ["Software Engineer", "Engineering Manager"]
                and second["source_recorded_at"].startswith("2024-06-10")
                and not second["recorded_at"].startswith("2024")
            )
            return (
                ok,
                f"now={now[0]['attributes']['role']!r}; as_of 2023-01-01={past[0]['attributes']['role']!r}; evt_002 source_recorded_at={second['source_recorded_at'][:10]} recorded_at={second['recorded_at'][:10]}",
            )

        async def s7() -> tuple[bool, str]:
            r = await d.send(
                "POST",
                "/v1/relationships",
                "demo/conversation_app",
                key="demo-p2-bad-edge",
                json_={
                    "type": "WORKS_AT",
                    "source": {"entity_id": d.ids["sarah"]},
                    "target": {"entity_id": d.ids["topic"]},
                    "valid_from": "2025-01-01",
                    "source_system": "hr",
                    "source_ref": "bad",
                },
            )
            v = r.json()["error"]["details"]["violations"]
            return (
                r.status_code == 422
                and r.json()["error"]["code"] == "rule_violation"
                and v[0]["code"] == "disallowed_endpoint"
                and "Entrepreneurship" not in r.text,
                f"{r.status_code} rule_violation {v[0]['code']} at {v[0]['path']} (no values echoed)",
            )

        async def s8() -> tuple[bool, str]:
            body = {
                "type": "Person",
                "name": "Sarah Johnson",
                "external_ids": HR("E-1001"),
                "attributes": {"birth_date": "1985-03-02"},
                "source_system": "hr",
                "source_ref": "E-1001",
            }
            replay = await d.send("POST", "/v1/entities", "demo/conversation_app", key="demo-p2-sarah", json_=body)
            clash = await d.send(
                "POST", "/v1/entities", "demo/conversation_app", key="demo-p2-sarah", json_={**body, "name": "Somebody Else"}
            )
            ok = (
                replay.headers.get("idempotent-replay") == "true"
                and replay.json()["entity_id"] == d.ids["sarah"]
                and clash.status_code == 409
                and clash.json()["error"]["code"] == "idempotency_mismatch"
            )
            return (
                ok,
                f"replay -> {replay.status_code} (same command), different body -> {clash.status_code} {clash.json()['error']['code']}",
            )

        # ---- 9-10: the graph and the zero-LLM gate ----------------------------------------------------------------------------------------
        async def s9() -> tuple[bool, str]:
            await wait_for_projection(d)
            group = f"ws-{DEMO_WS}"
            driver = neo4j.AsyncGraphDatabase.driver(
                settings.neo4j_uri, auth=(settings.neo4j_user, settings.neo4j_password.get_secret_value())
            )
            try:
                async with driver.session(database="neo4j") as session:
                    nodes = [
                        dict(r)
                        async for r in await session.run(
                            "MATCH (n:Entity {group_id: $g}) WHERE n.uuid IN $ids RETURN n.uuid AS u, n.name AS name",
                            g=group,
                            ids=[d.ids[k] for k in ("sarah", "acme", "michael", "topic")],
                        )
                    ]
                    edges = {
                        r["u"]: r
                        async for r in await session.run(
                            "MATCH (:Entity {group_id: $g})-[e:RELATES_TO {name: 'WORKS_AT'}]->(:Entity) WHERE e.uuid IN $ids RETURN e.uuid AS u, e.valid_at AS va, e.invalid_at AS ia, e.created_at AS ca, e.fact AS fact",
                            g=group,
                            ids=[d.ids["fact1"], d.ids["fact2"]],
                        )
                    }
            finally:
                await driver.close()
            f1, f2 = edges[d.ids["fact1"]], edges[d.ids["fact2"]]
            rec2 = (await d.send("GET", f"/v1/relationships/{d.ids['fact2']}", "demo/agent_planning")).json()["recorded_at"]
            shape = (
                len(nodes) == 4
                and len(edges) == 2
                and str(f1["va"])[:10] == "2022-01-10"
                and str(f1["ia"])[:10] == "2024-06-01"
                and str(f2["va"])[:10] == "2024-06-01"
                and f2["ia"] is None
                and f2["ca"].to_native().isoformat()[:19] == rec2[:19]
            )
            kg = str(Path(sys.executable).parent / "kg")
            proc = await asyncio.to_thread(
                subprocess.run, [kg, "graph", "reconcile", "--workspace", "demo"], capture_output=True, text=True, check=False
            )  # noqa: S603
            report = json.loads(proc.stdout.strip().splitlines()[-1]) if proc.stdout.strip() else {}
            ok = shape and proc.returncode == 0 and report.get("drift") == []
            return (
                ok,
                f"4 nodes, 2 WORKS_AT edges (valid_at/invalid_at/created_at as expected); reconcile: {report.get('entities')} entities, {report.get('facts')} facts, drift={report.get('drift')}",
            )

        async def s10() -> tuple[bool, str]:
            engine = create_app_engine(settings, pool_size=2, max_overflow=0)
            ctx = WorkspaceContext.for_system_task(DEMO_WS, reason="demo-p2 gate")
            try:
                for _ in range(15):  # the call log is flushed in batches
                    async with tenant_session(engine, ctx) as conn:
                        by_op = {
                            r.operation: r.n
                            for r in (
                                await conn.execute(
                                    text(
                                        "SELECT operation, count(*) AS n FROM model_calls WHERE workspace_id = :w AND purpose = 'graph_projection' GROUP BY operation"
                                    ),
                                    {"w": DEMO_WS},
                                )
                            ).all()
                        }
                        chat = (
                            await conn.execute(
                                text(
                                    "SELECT count(*) FROM model_calls WHERE workspace_id = :w AND operation = 'chat' AND created_at >= (SELECT min(created_at) FROM commands WHERE workspace_id = :w)"
                                ),
                                {"w": DEMO_WS},
                            )
                        ).scalar_one()
                    if by_op.get("embeddings"):
                        break
                    await asyncio.sleep(1)
            finally:
                await engine.dispose()
            return set(by_op) == {"embeddings"} and by_op[
                "embeddings"
            ] > 0 and chat == 0, f"chat calls = {chat}, embedding calls = {by_op.get('embeddings', 0)} (purpose graph_projection)"

        # ---- 11: versions, rollback and the version each command ran under ----------------------------------------------------------------
        async def s11() -> tuple[bool, str]:
            v2 = await d.version("1.1.0")
            if v2 is None:
                cp = await d.send("POST", "/v1/ontologies/versions", "demo/admin@demo", key="demo-p2-copy", json_={"copy_of": d.ids["v1"]})
                doc = cp.json()["spec"]
                doc["entity_types"]["Person"]["attributes"]["nickname"] = {
                    "type": "string",
                    "max_length": 40,
                    "description": "What people call them",
                }
                put = await d.send(
                    "PUT",
                    f"/v1/ontologies/versions/{cp.json()['id']}",
                    "demo/admin@demo",
                    json_={"content": doc},
                    headers={"If-Match": cp.headers["etag"]},
                )
                assert put.json()["validation_report"]["errors"] == [], put.text
                diff = (await d.send("GET", f"/v1/ontologies/versions/{cp.json()['id']}/diff", "demo/admin@demo", against="active")).json()
                assert [c["breaking"] for c in diff["changes"] if c["path"].endswith("nickname")] == [False]
                pub = await d.send(
                    "POST",
                    f"/v1/ontologies/versions/{cp.json()['id']}/publish",
                    "demo/admin@demo",
                    key="demo-p2-publish-2",
                    json_={"notes": "adds Person.nickname"},
                )
                assert pub.status_code == 200 and pub.json()["version"] == "1.1.0", pub.text
                etag = (await d.send("GET", f"/v1/entities/{d.ids['michael']}", "demo/conversation_app")).headers["etag"]
                upd = await d.send(
                    "PATCH",
                    f"/v1/entities/{d.ids['michael']}",
                    "demo/conversation_app",
                    key="demo-p2-michael-nick",
                    json_={"attributes": {"nickname": "Mike"}},
                    headers={"If-Match": etag},
                )
                assert upd.status_code in (200, 201, 202), upd.text
                v2 = await d.version("1.1.0")
            assert v2 is not None
            active = (await d.send("GET", "/v1/ontologies/active", "demo/admin@demo")).json()["version"]
            if active["version"] != "1.0.0":
                rb = await d.send(
                    "POST",
                    "/v1/ontologies/active",
                    "demo/admin@demo",
                    key=f"demo-p2-rollback-from-{active['id']}",
                    json_={"version_id": d.ids["v1"], "reason_code": "demo_rollback"},
                )
                assert rb.status_code == 200, rb.text
            now_active = (await d.send("GET", "/v1/ontologies/active", "demo/admin@demo")).json()["version"]
            cmds = (await d.send("GET", "/v1/commands", "demo/admin@demo", limit=100)).json()["items"]
            used = {c["ontology_version_id"] for c in cmds}
            v1_after = (await d.send("GET", f"/v1/ontologies/versions/{d.ids['v1']}", "demo/admin@demo")).json()
            ok = now_active["version"] == "1.0.0" and {d.ids["v1"], v2["id"]} <= used and v1_after["state"] == "published"
            return ok, f"v1.1.0 published then v1.0.0 re-activated; commands ran under {len(used)} versions; no version was edited"

        # ---- 12-13: isolation and roles ---------------------------------------------------------------------------------------------------
        async def s12() -> tuple[bool, str]:
            who = "other_demo/admin@other"
            probes = {
                "entity": f"/v1/entities/{d.ids['sarah']}",
                "fact": f"/v1/relationships/{d.ids['fact1']}",
                "command": f"/v1/commands/{(await d.send('GET', '/v1/commands', 'demo/admin@demo', limit=1)).json()['items'][0]['id']}",
                "version": f"/v1/ontologies/versions/{d.ids['v1']}",
            }
            same = True
            for path in probes.values():
                foreign = await d.send("GET", path, who)
                same &= foreign.status_code == 404 and strip(foreign) == strip(
                    await d.send("GET", path.rsplit("/", 1)[0] + f"/{uuid.uuid4()}", who)
                )
            empty = True
            for p in ("/v1/entities", "/v1/commands", "/v1/ontologies/versions"):
                empty &= (await d.send("GET", p, who)).json()["items"] == []
            return same and empty, f"entity/fact/command/version ids -> 404 identical to random ids; lists empty={empty}"

        async def s13() -> tuple[bool, str]:
            denied = await d.send("POST", "/v1/entities", "demo/agent_planning", json_={"type": "Person", "name": "Nope"})
            read = await d.send("GET", f"/v1/entities/{d.ids['sarah']}", "demo/agent_planning")
            admin = await d.send("GET", f"/v1/entities/{d.ids['sarah']}", "demo/admin@demo")
            ok = (
                denied.status_code == 403
                and read.status_code == 200
                and read.json()["attributes"]["birth_date"] == {"redacted": True}
                and admin.json()["attributes"]["birth_date"] == "1985-03-02"
            )
            return (
                ok,
                f"agent POST -> {denied.status_code}; agent GET -> {read.status_code} birth_date={read.json()['attributes']['birth_date']}; admin sees {admin.json()['attributes']['birth_date']}",
            )

        await step("1 draft with a deliberate error", "validation error at the field's path", s1)
        await step("2 fix, publish v1.0.0", "immutable (PUT -> 409), hash + audit present", s2)
        await step("3 conversation_app creates 4 entities", "201s; a repeat of hr:E-1001 -> 200 matched_existing", s3)
        await step("4 evt_001 Software Engineer from 2022-01-10", "applied", s4)
        await step("5 evt_002 Engineering Manager from 2024-06-01", "predecessor ended 2024-06-01, still active", s5)
        await step("6 current / as_of / history", "EM now, SE as of 2023, both in history with valid + record times", s6)
        await step("7 WORKS_AT Sarah -> Entrepreneurship", "422 rule_violation (ONT-04)", s7)
        await step("8 idempotent replay / key reuse", "replay, then 409 idempotency_mismatch", s8)
        await step("9 graph equals Postgres", "4 nodes, 2 edges with expected times; reconcile drift = 0", s9)
        await step("10 zero LLM calls", "chat = 0, embeddings > 0", s10)
        await step("11 publish v1.1.0, roll back to v1.0.0", "pointer moves, versions unchanged", s11)
        await step("12 admin@other probes demo ids", "404 identical to random; lists empty", s12)
        await step("13 agent_planning", "POST 403; GET 200 with birth_date redacted", s13)

    width = max(len(r[0]) for r in rows)
    print(f"{'step'.ljust(width)}  result  expected -> actual")
    for name, expect, actual, ok in rows:
        print(f"{name.ljust(width)}  {'PASS' if ok else 'FAIL'}    {expect} -> {actual}")
    failed = [r for r in rows if not r[3]]
    print(f"\n{len(rows) - len(failed)}/{len(rows)} passed")
    return 1 if failed else 0


async def wait_for_projection(d: Demo, seconds: float = 30.0) -> None:
    """The API answers 201 only when the projection finished inside its budget; otherwise the worker's sweeper finishes it. Wait for both."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        items = (await d.send("GET", "/v1/entities", "demo/admin@demo", limit=100)).json()["items"]
        mine = [e for e in items if e["id"] in {d.ids[k] for k in ("sarah", "acme", "michael", "topic")}]
        facts = [(await d.send("GET", f"/v1/relationships/{d.ids[k]}", "demo/admin@demo")).json() for k in ("fact1", "fact2")]
        if all(e["projection_status"] == "projected" for e in mine) and all(f["projection_status"] == "projected" for f in facts):
            return
        await asyncio.sleep(1)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
