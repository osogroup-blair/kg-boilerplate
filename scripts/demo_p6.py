"""Phase 6 demo: the task-aware context engine, the MVP. Exits non-zero if any assertion fails.

Run after `make up seed` (`make demo-p6` publishes the v1.3.0 example ontology, runs the demo, then the context evaluation). It builds on the P2-P5 demo state (Sarah, Acme, the
aspiration, the NewCo claim, the approved guidance and project finding, the cycling distractor, the research agent's private note) and is re-runnable: builds are reads, so
every run makes new traces, and the one memory it writes carries a run-unique text. Reads API keys from .local/dev-credentials.json.

The story (blueprint 06-reference/01, Agent B): the planning agent is asked to help Sarah assess starting a business, with a hard token budget. It must get what is TRUE NOW
(Engineering Manager at Acme, with its evidence), what is REPORTED (she is considering entrepreneurship, unverified), the circumstances (the evt_003 memory), the person's
standing guidance, the project finding, the caution that the NewCo claim is only a claim, and the one thing nobody knows (her runway) - and none of the cycling, none of the
research agent's private notes."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
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
METRICS = os.environ.get("KG_API_METRICS_URL", "http://127.0.0.1:9100/metrics")
DEMO_WS = workspace_id_for("demo")
RUN = uuid.uuid4().hex[:8]
ENVFILE = ROOT / ".env" if (ROOT / ".env").exists() else ROOT / ".env.example"
COMPOSE = ["docker", "compose", "-f", str(ROOT / "deploy/compose/docker-compose.yml"), "--env-file", str(ENVFILE)]
Row = tuple[str, str, str, bool]
TASK = "Help Sarah assess the implications of starting a business"
BUSINESS = "Sarah discussed a possible software business with Michael; she had not decided to leave Acme."
CYCLIST = "Sarah Johnson from the cycling club rode 40 km with Tom on Saturday."
GUIDE = "When advising Sarah, present the options side by side with their risks and trade-offs before recommending one."
FINDING = "NewCo needs a state licence to operate in this sector (state licensing board handbook, 2026)."


def compose(*args: str) -> str:
    proc = subprocess.run([*COMPOSE, *args], capture_output=True, text=True, cwd=ROOT, check=False)  # noqa: S603
    return proc.stdout + proc.stderr


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
        if method != "GET" and path != "/v1/context/build":
            hdrs["idempotency-key"] = key or str(uuid.uuid4())
        return await self.c.request(method, path, headers=hdrs, json=json_, params=params or None)

    async def sql(self, query: str, **params: Any) -> list[Any]:
        engine = create_async_engine(self.settings.database_url_owner.get_secret_value())
        try:
            async with engine.connect() as conn:
                return list((await conn.execute(text(query), {"ws": DEMO_WS, **params})).all())
        finally:
            await engine.dispose()

    async def build(self, who: str, task: str = TASK, *, headers: dict[str, str] | None = None, **body: Any) -> httpx.Response:
        return await self.send("POST", "/v1/context/build", who, json_={"task": task, **body}, headers=headers)

    async def ok(self, who: str, task: str = TASK, **body: Any) -> dict[str, Any]:
        r = await self.build(who, task, **body)
        assert r.status_code == 200, f"{r.status_code} {r.text[:300]}"
        return r.json()  # type: ignore[no-any-return]

    async def principal(self, handle: str) -> str:
        rows = await self.sql("SELECT id::text AS id FROM principals WHERE workspace_id = :ws AND handle = :h", h=handle)
        return str(rows[0].id)

    async def memory(self, where: str, **params: Any) -> str | None:
        rows = await self.sql(
            f"SELECT id::text AS id FROM curated_memories WHERE workspace_id = :ws AND {where} ORDER BY recorded_at DESC LIMIT 1",  # noqa: S608 (constant fragments)
            **params,
        )
        return str(rows[0].id) if rows else None

    async def ensure_entity(self, type_: str, name: str, key: str) -> str:
        rows = await self.sql(
            "SELECT e.id::text AS id FROM entities e WHERE e.workspace_id = :ws AND e.type_key = :t AND e.display_name = :n AND e.origin = 'command' AND e.status = 'active' ORDER BY e.recorded_at LIMIT 1",
            t=type_,
            n=name,
        )
        if rows:
            return str(rows[0].id)
        r = await self.send(
            "POST",
            "/v1/entities",
            "demo/conversation_app",
            key=key,
            json_={"type": type_, "name": name, "source_system": "hr", "source_ref": name},
        )
        assert r.status_code in (200, 201, 202), r.text
        return str(r.json()["entity_id"])


def tokenizer(out: dict[str, Any]) -> str:
    return str(out["budgets"]["tokenizer_id"])


def ids_in(out: dict[str, Any]) -> set[str]:
    return {i["id"] for k in ("facts", "memories", "instructions", "conflicts") for i in out[k] if i["id"]}


def lines(out: dict[str, Any]) -> list[str]:
    return [ln for ln in out["context_text"].splitlines() if ln.startswith("- ")]


async def main() -> int:
    creds_path = ROOT / ".local" / "dev-credentials.json"
    if not creds_path.exists():
        print("missing .local/dev-credentials.json: run `make seed` first", file=sys.stderr)
        return 2
    keys: dict[str, str] = json.loads(creds_path.read_text())
    for need in ("demo/agent_support", "other_demo/agent_other", "demo/admin@demo"):
        if need not in keys:
            print(f"missing {need}: run `make seed` (it mints the key once) or `uv run kg seed demo --rotate-keys`", file=sys.stderr)
            return 2
    rows: list[Row] = []
    async with httpx.AsyncClient(base_url=BASE, timeout=120) as client:
        d = Demo(client, keys, Settings())

        async def step(name: str, expect: str, fn: Callable[[], Awaitable[tuple[bool, str]]]) -> None:
            try:
                ok, actual = await fn()
            except Exception as exc:
                ok, actual = False, f"{type(exc).__name__}: {str(exc)[:300]}"
            rows.append((name, expect, actual, ok))

        # ---- 1: the profile the engine will use ----------------------------------------------------------------------------------------
        async def s1() -> tuple[bool, str]:
            d.ids["sarah"] = await d.ensure_entity("Person", "Sarah Johnson", "demo-p6-sarah")
            d.ids["planning"], d.ids["admin"] = await d.principal("agent_planning"), await d.principal("admin@demo")
            profiles = (await d.send("GET", "/v1/context/profiles", "demo/agent_planning")).json()
            planning = next(p for p in profiles if p["name"] == "planning")
            active = (await d.send("GET", "/v1/ontologies/active", "demo/admin@demo")).json()
            spec = (await d.send("GET", f"/v1/ontologies/versions/{active['version']['id']}", "demo/admin@demo")).json()["spec"]
            signals = [s["key"] for s in spec["context_profiles"]["planning"].get("expected_signals", [])]
            ok = planning["source"] == "ontology" and planning["max_tokens"] == 1200 and signals == ["current_employer", "financial_runway"]
            return (
                ok,
                f"ontology {active['version']['version']} supplies the planning profile ({planning['max_tokens']} tokens max, modes {planning['compression']}) and its expected signals {signals}; {len(profiles)} profiles listed",
            )

        await step("1 context profile and expected signals", "planning comes from the active ontology with its two expected signals", s1)

        # ---- 2: the planning task at 600 tokens -----------------------------------------------------------------------------------------
        async def s2() -> tuple[bool, str]:
            out = await d.ok("demo/agent_planning", subject_ids=[d.ids["sarah"]], profile="planning", max_tokens=600, project="newco-plan")
            for _ in range(
                3
            ):  # the first graph search after a restart can outlast the 1.5 s source timeout while Neo4j warms its caches: that is `partial`, by design; ask again
                if out["status"] != "partial":
                    break
                await asyncio.sleep(3)
                out = await d.ok(
                    "demo/agent_planning", subject_ids=[d.ids["sarah"]], profile="planning", max_tokens=600, project="newco-plan"
                )
            d.ids["main"] = out
            ls = lines(out)
            text_ = out["context_text"]
            cycling = await d.memory("content ILIKE :c", c="%cycling%")
            notes = [
                r.id
                for r in await d.sql(
                    "SELECT id::text AS id FROM curated_memories WHERE workspace_id = :ws AND type = 'working' AND content LIKE 'check NewCo licensing%'"
                )
            ]
            claim = await d.sql(
                "SELECT f.status FROM facts f JOIN entities t ON t.id = f.target_entity_id WHERE f.workspace_id = :ws AND f.rel_type = 'WORKS_AT' AND t.display_name = 'NewCo' AND f.source_type = 'extracted' ORDER BY f.recorded_at DESC LIMIT 1"
            )
            claimed = bool(claim) and claim[0].status in ("proposed", "disputed")
            checks = {
                "complete within 600": out["status"] == "complete" and out["budgets"]["used_tokens"] <= 600,
                "Engineering Manager at Acme cites evt_002": any(
                    "Engineering Manager" in ln and "Acme" in ln and "E:evt_002" in ln for ln in ls
                ),
                "the ended role is not stated": not any("Software Engineer" in ln for ln in ls),
                "considering entrepreneurship, unverified": any("considering entrepreneurship" in ln.lower() for ln in ls),
                "the evt_003 memory": BUSINESS in text_ or any(m["memory_type"] == "episodic" for m in out["memories"]),
                "guidance": any(GUIDE[:30] in i["text"] for i in out["instructions"]),
                "project finding": "state licence" in text_.lower(),
                "NewCo only as a claim": (not claimed) or any("Unverified claim under review" in ln and "NewCo" in ln for ln in ls),
                "runway unknown": "No verified financial runway for Sarah Johnson" in text_,
                "no cycling": cycling is None or cycling not in ids_in(out),
                "no private notes": not set(notes) & ids_in(out),
            }
            bad = [k for k, v in checks.items() if not v]
            return (
                not bad,
                f"{out['status']}{[u['source'] + '/' + u['reason'] for u in out['unavailable_sources']] or ''}, {out['budgets']['used_tokens']}/600 tokens ({tokenizer(out)}), {len(out['facts'])} facts, {len(out['memories'])} memories, {len(out['instructions'])} guidance, {len(out['unknowns'])} unknown, {len(out['conflicts'])} conflict; {'FAILED ' + str(bad) if bad else 'every expectation met'}",
            )

        await step(
            "2 the planning task at 600 tokens (AT-06, AT-08, RET-02, RET-05)",
            "current verified + unverified + circumstances + guidance + finding + the NewCo caution + the runway unknown; no cycling, no private notes",
            s2,
        )

        # ---- 3: smaller budgets ------------------------------------------------------------------------------------------------------------
        async def s3() -> tuple[bool, str]:
            base = dict(subject_ids=[d.ids["sarah"]], profile="planning", project="newco-plan")
            big = await d.ok("demo/agent_planning", max_tokens=1200, **base)
            small = await d.ok("demo/agent_planning", max_tokens=big["budgets"]["required_tokens"] + 12, **base)
            tiny = await d.ok("demo/agent_planning", max_tokens=64, **base)
            widen = await d.build("demo/agent_planning", subject_ids=[d.ids["sarah"]], profile="planning", max_tokens=1201)
            ok = (
                small["status"] == "complete"
                and small["budgets"]["used_tokens"] < big["budgets"]["used_tokens"]
                and {u["handle"] for u in small["unknowns"] + small["conflicts"]}
                == {u["handle"] for u in big["unknowns"] + big["conflicts"]}
                and tiny["status"] == "insufficient_budget"
                and tiny["budgets"]["required_tokens"] > 64
                and tiny["budgets"]["used_tokens"] <= 64
                and tiny["facts"] == []
                and widen.status_code == 422
            )
            return (
                ok,
                f"1200 -> {big['budgets']['used_tokens']} tokens; {small['budgets']['max_tokens']} -> {small['budgets']['used_tokens']} (required tier intact, {small['budgets']['truncated']} dropped); 64 -> insufficient_budget, the required tier needs {tiny['budgets']['required_tokens']}; asking for 1201 on a 1200 profile -> {widen.status_code}",
            )

        await step(
            "3 a smaller budget, and too small (AT-08, CTX-05)",
            "the required tier survives a tight budget; below it the answer is insufficient_budget with the figure to ask for; a request cannot widen the profile",
            s3,
        )

        # ---- 4: time -----------------------------------------------------------------------------------------------------------------------
        async def s4() -> tuple[bool, str]:
            then = await d.ok(
                "demo/agent_planning", "Where does Sarah work", subject_ids=[d.ids["sarah"]], profile="quick_answer", as_of="2023-01-01"
            )
            now = await d.ok("demo/agent_planning", "Where does Sarah work", subject_ids=[d.ids["sarah"]], profile="quick_answer")
            em = await d.sql(
                "SELECT f.recorded_at FROM facts f WHERE f.workspace_id = :ws AND f.source_entity_id = CAST(:s AS uuid) AND f.rel_type = 'WORKS_AT' AND f.attributes->>'role' = 'Engineering Manager' ORDER BY f.recorded_at LIMIT 1",
                s=d.ids["sarah"],
            )
            before = (em[0].recorded_at.replace(microsecond=0)).isoformat() if em else "2020-01-01T00:00:00Z"
            unknown_then = await d.ok(
                "demo/agent_planning",
                "Where does Sarah work",
                subject_ids=[d.ids["sarah"]],
                profile="quick_answer",
                known_at="2020-01-01T00:00:00Z",
            )
            ok = (
                "Software Engineer" in then["context_text"]
                and "Engineering Manager" not in then["context_text"]
                and "VERIFIED FACTS AS OF 2023-01-01" in then["context_text"]
                and "Engineering Manager" in now["context_text"]
                and "Software Engineer" not in now["context_text"]
                and unknown_then["facts"] == []
            )
            return (
                ok,
                f"as_of 2023-01-01 -> {', '.join(f['text'] for f in then['facts'])}; now -> {', '.join(f['text'] for f in now['facts'])}; known_at 2020-01-01 (before the platform held either role; the manager role was recorded {before}) -> {unknown_then['status']}, no facts",
            )

        await step(
            "4 valid time and record time (RET-03, KGR-04)",
            "as_of brings the Software Engineer role back, labelled; today it is the Engineering Manager role only; before it was recorded, nothing",
            s4,
        )

        # ---- 5: relevance ---------------------------------------------------------------------------------------------------------------
        async def s5() -> tuple[bool, str]:
            out = await d.ok("demo/agent_planning", "Which cycling club is Sarah in", subject_ids=[d.ids["sarah"]], profile="quick_answer")
            cycling = await d.memory("content ILIKE :c", c="%cycling%")
            business = await d.memory("content = :c", c=BUSINESS)
            ok = (
                cycling in ids_in(out)
                and business not in ids_in(out)
                and "Engineering Manager" not in out["context_text"]
                and "NewCo" not in out["context_text"]
            )
            return (
                ok,
                f"{out['budgets']['used_tokens']}/{out['budgets']['max_tokens']} tokens: the cycling memory in; the career facts, the business memory, the guidance and the NewCo caution out ({out['excluded_source_counts']})",
            )

        await step(
            "5 an unrelated question (AT-07)", "the cycling memory is included and the career material is left out, caveats included", s5
        )

        # ---- 6: nothing known ------------------------------------------------------------------------------------------------------------
        async def s6() -> tuple[bool, str]:
            ghost = await d.ensure_entity("Person", "Priya Raman", "demo-p6-priya")
            out = await d.ok("demo/agent_planning", "Where does Priya work", subject_ids=[ghost], profile="planning")
            ok = (
                out["status"] == "insufficient_evidence"
                and out["facts"] == []
                and out["memories"] == []
                and "No verified current employer for Priya Raman" in out["context_text"]
                and "Acme" not in out["context_text"]
            )
            return ok, f"{out['status']}: {[u['text'] for u in out['unknowns']]}, nothing invented"

        await step("6 nothing is known (AT-09)", "insufficient_evidence, the unknowns, no fabricated statement", s6)

        # ---- 7: who may ask --------------------------------------------------------------------------------------------------------------
        async def s7() -> tuple[bool, str]:
            support = await d.ok("demo/agent_support", subject_ids=[d.ids["sarah"]], profile="planning", project="newco-plan")
            foreign = await d.build("other_demo/agent_other", subject_ids=[d.ids["sarah"]], profile="planning")
            ghost = await d.build("other_demo/agent_other", subject_ids=[str(uuid.uuid4())], profile="planning")
            strip = lambda r: {k: v for k, v in r.json().items() if k != "generated_at"}  # noqa: E731
            ok = (
                "Engineering Manager" in support["context_text"]
                and support["memories"] == []
                and support["instructions"] == []
                and foreign.json()["status"] == "denied"
                and strip(foreign) == strip(ghost)
                and foreign.json()["context_text"] == ""
                and "Sarah" not in foreign.text
            )
            return (
                ok,
                f"agent_support: {len(support['facts'])} facts and no memories of Sarah; another workspace: {foreign.json()['status']} with an empty body, identical to a random id",
            )

        await step(
            "7 who may ask (AT-10, AT-11)",
            "an agent with no grants gets facts and none of Sarah's memories; another workspace is denied with nothing, the same as for an id that does not exist",
            s7,
        )

        # ---- 8: Neo4j stops ---------------------------------------------------------------------------------------------------------------
        async def s8() -> tuple[bool, str]:
            compose("stop", "neo4j")
            try:
                partial = await d.ok(
                    "demo/agent_planning", subject_ids=[d.ids["sarah"]], profile="planning", max_tokens=600, project="newco-plan"
                )
                refused = await d.build("demo/agent_planning", subject_ids=[d.ids["sarah"]], profile="planning", allow_partial=False)
            finally:
                compose("start", "neo4j")
                for _ in range(90):
                    if compose("ps", "--format", "{{.Health}}", "neo4j").strip() == "healthy":
                        break
                    await asyncio.sleep(2)
            healed = None
            for _ in range(30):  # the driver reconnects on the next query
                healed = await d.ok(
                    "demo/agent_planning", subject_ids=[d.ids["sarah"]], profile="planning", max_tokens=600, project="newco-plan"
                )
                if healed["status"] == "complete":
                    break
                await asyncio.sleep(2)
            assert healed is not None
            sources = {u["source"]: u["reason"] for u in partial["unavailable_sources"]}
            ok = (
                partial["status"] == "partial"
                and "graph" in sources
                and "Engineering Manager" in partial["context_text"]
                and refused.status_code == 503
                and refused.json()["error"]["code"] == "context_incomplete"
                and "Engineering Manager" not in refused.text
                and healed["status"] == "complete"
            )
            return (
                ok,
                f"Neo4j stopped: partial, unavailable {sources}, current state still from Postgres; allow_partial=false -> {refused.status_code} {refused.json()['error']['code']}, no package; restarted: {healed['status']}",
            )

        await step("8 Neo4j stops (CTX-07, RET-06)", "partial and labelled; fail-closed on request; complete again after the restart", s8)

        # ---- 9: trace, act-as ---------------------------------------------------------------------------------------------------------------
        async def s9() -> tuple[bool, str]:
            out = d.ids["main"]
            tr = await d.send("GET", f"/v1/context/{out['context_id']}/trace", "demo/agent_planning")
            denied = await d.send("GET", f"/v1/context/{out['context_id']}/trace", "demo/agent_support")
            body = dict(subject_ids=[d.ids["sarah"]], profile="planning", max_tokens=600, project="newco-plan")
            lab = await d.build("demo/admin@demo", headers={"x-act-as-principal": d.ids["planning"]}, **body)
            direct = await d.ok("demo/agent_planning", **body)
            audit = (await d.send("GET", "/v1/audit", "demo/admin@demo", action="context.lab_build", limit=3)).json()["items"]
            t = tr.json()
            ok = (
                tr.status_code == 200
                and denied.status_code == 404
                and t["versions"]["ranker"] == "ctx-rank.v1"
                and any(i["included"] for i in t["items"])
                and all(i["exclusion_reason"] for i in t["items"] if not i["included"])  # (a fresh workspace may exclude nothing)
                and lab.status_code == 200
                and lab.json()["context_text"] == direct["context_text"]
                and bool(audit)
                and audit[0]["metadata"]["target_principal_id"] == d.ids["planning"]
            )
            return (
                ok,
                f"the owner reads the plan, {t['counts']['selected']} selected / {len(t['items'])} authorised candidates with scores and reasons; agent_support -> {denied.status_code}; the Lab acting as agent_planning builds the identical package and is audited (context.lab_build)",
            )

        await step(
            "9 the trace and the Context Lab (AT-12, ADM-04, ADM-05)",
            "the owner sees why; another agent gets 404; the Lab's package is the agent's package, audited",
            s9,
        )

        # ---- 10: re-materialisation -----------------------------------------------------------------------------------------------------------
        async def s10() -> tuple[bool, str]:
            note = f"Run {RUN}: the NewCo licence application is due at the end of the first quarter."
            r = await d.send(
                "POST",
                "/v1/memories",
                "demo/admin@demo",
                key=f"demo-p6-note-{RUN}",
                json_={
                    "type": "semantic",
                    "content": note,
                    "scopes": [{"kind": "project", "ref": "newco-plan"}],
                    "source_ref": "demo-p6",
                    "purpose": "re-materialisation drill",
                },
            )
            assert r.status_code == 201, r.text
            mid = r.json()["memory_id"]
            for _ in range(60):
                if (await d.sql("SELECT embedding_status FROM curated_memories WHERE id = CAST(:m AS uuid)", m=mid))[
                    0
                ].embedding_status != "pending":
                    break
                await asyncio.sleep(1)
            out = await d.ok(
                "demo/agent_planning", "NewCo licence application", subject_ids=[d.ids["sarah"]], profile="planning", project="newco-plan"
            )
            assert mid in ids_in(out), "the new project memory should be in the package"
            got = (await d.send("GET", f"/v1/memories/{mid}", "demo/admin@demo")).headers["etag"]
            gone = await d.send(
                "POST", f"/v1/memories/{mid}/expire", "demo/admin@demo", json_={"reason_code": "drill_over"}, headers={"if-match": got}
            )
            assert gone.status_code in (200, 201), gone.text
            again = (await d.send("GET", f"/v1/context/{out['context_id']}", "demo/agent_planning")).json()
            ok = (
                again["rematerialized"] is True
                and again["withdrawn"] == 1
                and mid not in ids_in(again)
                and "licence application is due" not in again["context_text"]
            )
            return (
                ok,
                f"the memory was in the package; after it expired the same context id re-materialises WITHOUT it (withdrawn {again['withdrawn']}): a stored copy would have resurfaced it",
            )

        await step(
            "10 re-materialisation under current authorisation", "a memory expired after the build is omitted and counted as withdrawn", s10
        )

        # ---- 11: telemetry --------------------------------------------------------------------------------------------------------------------
        async def s11() -> tuple[bool, str]:
            sentinel = f"zq{uuid.uuid4().hex}"
            out = await d.ok(
                "demo/agent_planning",
                f"{sentinel} Help Sarah assess the implications of starting a business",
                subject_ids=[d.ids["sarah"]],
                profile="planning",
                max_tokens=600,
                project="newco-plan",
            )
            logs = await asyncio.to_thread(compose, "logs", "--no-log-prefix", "--tail=3000", "api", "worker")
            async with httpx.AsyncClient() as c:
                metrics = (await c.get(METRICS)).text
            db = ""
            for table in ("context_traces", "context_items", "audit_log", "model_calls", "outbox_jobs"):
                for x in await d.sql(f"SELECT t::text AS j FROM {table} t WHERE t.workspace_id = :ws ORDER BY 1 DESC LIMIT 400"):  # noqa: S608 (constant table names)
                    db += " " + x.j
            body = out["context_text"]
            clean = all(sentinel not in blob and "Engineering Manager at Acme" not in blob for blob in (logs, metrics, db))
            return (
                clean and bool(body),
                f"a task containing {sentinel[:8]}… and the rendered context appear in no log line, metric, trace, audit entry, model-call row or job payload (the trace keeps a keyed hash and a token count)",
            )

        await step(
            "11 context text never reaches telemetry",
            "no task or context text in logs, metrics, traces, audit, the call log or job payloads",
            s11,
        )

    width = max(len(r[0]) for r in rows)
    print(f"{'step'.ljust(width)}  result  expected -> actual")
    for name, expect, actual, ok in rows:
        print(f"{name.ljust(width)}  {'PASS' if ok else 'FAIL'}    {expect} -> {actual}")
    passed = sum(1 for r in rows if r[3])
    print(f"\n{passed}/{len(rows)} passed")
    print(
        "\nstep 12 (make eval-context) scores the 14 frozen benchmark cases; the MVP sign-off needs the single LIVE session (P4, P5 and P6 together):\n"
        "  1. ./start-local.sh in ~/Documents/oso_model_router        2. export OSO_ROUTER_API_KEY=...  (non-admin kg-api / kg-worker keys)\n"
        "  3. make verify-router        4. make eval-extraction-live        5. make eval-memory-live        6. make eval-context-live\n"
        "  then confirm the proposed thresholds, set IDENTITY_COS_MIN and MEMORY_VEC_MIN_COSINE, close TK-02 (the token margin) and re-run `make eval-context`."
    )
    return 0 if passed == len(rows) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
