"""The Sarah scenario, step by step (blueprint 05-delivery/02, 06-reference/01). Black-box: HTTP, the SDK and the mock-router control API only."""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx
from kg_platform_sdk import KGClientSync

from examples._kit import admin, mock, waits
from examples._kit.env import Env
from examples._kit.report import Report, Step
from examples._kit.show import context_lines, show_context
from examples._kit.workspace import Workspace, fresh_workspace

PROJECT = "newco-plan"
EVT003 = "Sarah Johnson: I'm considering entrepreneurship, and Michael, who I've known for years, keeps encouraging me."
EVT005 = f"Research Agent market analysis {uuid.uuid4().hex[:6]}: NewCo's sector requires a state licence; the handbook lists the steps and the processing time."
FINDING = "NewCo needs a state licence to operate in this sector (state licensing board handbook, 2026)."
NOTE = "check NewCo licensing rules for the plan, private to the research agent"
TASK = "Help Sarah assess the implications of starting a business"


def hr(v: str) -> list[dict[str, str]]:
    return [{"system": "hr", "value": v}]


class Scenario:
    def __init__(self, env: Env, ws: Workspace, report: Report, out: Callable[[str], None] = print) -> None:
        self.env, self.ws, self.report, self.say = env, ws, report, out
        self.ids: dict[str, str] = {}
        self.data: dict[str, Any] = {}

    # ---- plumbing ------------------------------------------------------------------------------------------------------------------------
    def step(self, sid: str, title: str, at: list[str], fn: Callable[[], dict[str, Any]]) -> None:
        self.say(f"\n[{sid}] {title}")
        try:
            evidence = fn()
            self.report.add(Step(sid, title, at, True, evidence))
            self.say(f"     pass  {evidence}")
        except Exception as exc:  # a failing step must not hide the later ones
            self.report.add(Step(sid, title, at, False, error=f"{type(exc).__name__}: {str(exc)[:900]}"))
            self.say(f"     FAIL  {type(exc).__name__}: {str(exc)[:900]}")

    def sdk(self, handle: str) -> KGClientSync:
        return KGClientSync(self.env.base_url, self.ws.keys[handle])

    def mock_requests(self) -> int:
        return len(httpx.get(f"{self.env.mock_url}/__mock/requests", timeout=10).json())

    def build(self, handle: str, **kw: Any) -> dict[str, Any]:
        with self.sdk(handle) as kg:
            for _ in range(4):  # the first graph search after a restart can outlast its source timeout (`partial`, by design): ask again
                out = kg.build_context(**kw).model_dump(mode="json")
                if out["status"] != "partial":
                    break
                time.sleep(3)
        return out

    def settle(self) -> None:
        waits.wait_until(lambda: (self._backlog() == 0, self._backlog()), timeout=180, what="the outbox to drain")

    def _backlog(self) -> int:
        r = admin.get(self.ws, "/v1/ops/backlog")
        if r.status_code != 200:
            return 0
        body = r.json()
        return int(sum(v for v in (body.get("pending", 0), body.get("leased", 0)) if isinstance(v, int)))

    # ---- steps ---------------------------------------------------------------------------------------------------------------------------
    def s1_ontology(self) -> dict[str, Any]:
        active = admin.get(self.ws, "/v1/ontologies/active").json()
        v = active.get("version") or active
        assert v.get("state", "published") == "published", active
        me = admin.get(self.ws, "/v1/me").json()
        self.ids["admin"] = str(me["principal"]["id"])
        self.ws.keys["admin"] = self.ws.admin_key
        for scope in ("subject", "project"):
            admin.post(
                self.ws,
                f"/v1/principals/{self.ids['admin']}/grants",
                {"scope_kind": scope, "scope_ref": "*", "privileges": ["read", "propose", "publish"]},
            )
        app = admin.register_principal(
            self.ws,
            "conversation-app",
            "app",
            trusted_source=True,
            grants=[{"scope_kind": "subject", "scope_ref": "*", "privileges": ["read", "propose", "publish"]}],
        )
        _ = app
        return {"ontology_version": v.get("version") or v.get("semver"), "content_hash": str(v.get("content_hash", ""))[:12]}

    def s2_commands(self) -> dict[str, Any]:
        before = self.mock_requests()
        key = self.ws.keys["conversation-app"]

        def ent(t: str, n: str, **b: Any) -> dict[str, Any]:
            return admin.post(self.ws, "/v1/entities", {"type": t, "name": n, "source_system": "hr", **b}, key=key)

        sarah = ent("Person", "Sarah Johnson", external_ids=hr("E-1001"), source_ref="E-1001")
        acme = ent("Organization", "Acme", external_ids=hr("ORG-1"), source_ref="ORG-1")
        michael = ent("Person", "Michael", source_ref="E-2")
        topic = ent("Topic", "Entrepreneurship", source_ref="T-1")
        self.ids.update(sarah=sarah["entity_id"], acme=acme["entity_id"], michael=michael["entity_id"], topic=topic["entity_id"])

        def rel(**b: Any) -> dict[str, Any]:
            body = {
                "type": "WORKS_AT",
                "source": {"entity_id": self.ids["sarah"]},
                "target": {"entity_id": self.ids["acme"]},
                "source_system": "hr",
                **b,
            }
            return admin.post(self.ws, "/v1/relationships", body, key=key)

        f1 = rel(valid_from="2022-01-10", attributes={"role": "Software Engineer"}, source_ref="evt_001")
        f2 = rel(valid_from="2024-06-01", attributes={"role": "Engineering Manager"}, source_ref="evt_002")
        self.ids.update(swe=f1["fact_id"], em=f2["fact_id"])
        # the same key and body again is the same logical write
        idem = str(uuid.uuid4())
        with httpx.Client(base_url=self.env.base_url, headers={"authorization": f"Bearer {key}", "idempotency-key": idem}, timeout=30) as c:
            body = {"type": "Person", "name": "Idem Check", "source_system": "hr", "source_ref": "E-9"}
            a, b = c.post("/v1/entities", json=body), c.post("/v1/entities", json=body)
        assert a.json()["entity_id"] == b.json()["entity_id"], "a repeated key must be one logical write"
        calls = self.mock_requests() - before
        assert calls == 0, f"commands made {calls} model call(s)"
        statuses = {x["projection_status"] for x in (sarah, acme, michael, topic, f1, f2)}
        return {"projection": sorted(statuses), "model_calls": calls, "idempotent_replay": True}

    def register_agents(self) -> None:

        def scope(kind: str, ref: str, *priv: str) -> dict[str, Any]:
            return {"scope_kind": kind, "scope_ref": ref, "privileges": list(priv)}

        admin.register_principal(
            self.ws,
            "research-agent",
            "agent",
            description="finds and publishes sourced findings",
            model_disclosure="allowed",
            allowed_profiles=["research"],
            grants=[scope("subject", self.ids["sarah"], "read", "propose"), scope("project", PROJECT, "read", "propose", "publish")],
        )
        admin.register_principal(
            self.ws,
            "planning-agent",
            "agent",
            allowed_profiles=["planning"],
            grants=[scope("subject", self.ids["sarah"], "read"), scope("project", PROJECT, "read")],
        )
        admin.register_principal(self.ws, "ungranted-agent", "agent", allowed_profiles=["planning"])

    def s3_conversation(self) -> dict[str, Any]:
        self.register_agents()
        key = self.ws.keys["conversation-app"]
        self.data["before_evt003"] = datetime.now(UTC).isoformat()
        time.sleep(1.2)
        r = admin.post(
            self.ws,
            "/v1/interactions",
            {
                "source_system": "chat",
                "source_ref": "evt_003",
                "kind": "message",
                "content": EVT003,
                "occurred_at": "2026-10-10T09:00:00Z",
                "subject_refs": [{"entity_id": self.ids[k]} for k in ("sarah", "michael", "topic")],
            },
            key=key,
        )
        self.ids["evt003"] = r["event_id"]
        d = waits.wait_interaction(self.ws, r["event_id"], timeout=240)
        stages = {s["stage"]: s["status"] for s in d["stages"]}
        assert d["processing_status"] == "indexed", (d["processing_status"], stages)
        self.settle()
        return {"processing_status": d["processing_status"], "stages": stages}

    def s4_facts_and_memory(self) -> dict[str, Any]:
        facts = admin.get(self.ws, f"/v1/entities/{self.ids['sarah']}/relationships", as_of="2026-10-11T00:00:00Z").json()
        items = facts if isinstance(facts, list) else facts.get("items", [])
        current = [f for f in items if f["rel_type"] == "WORKS_AT"]
        assert any(f.get("attributes", {}).get("role") == "Engineering Manager" for f in current), current
        mems = admin.get(self.ws, "/v1/memories", type="episodic").json()
        rows = mems if isinstance(mems, list) else mems.get("items", [])
        assert rows, "the conversation should have produced an episodic memory"
        self.ids["mem_episodic"] = rows[0]["id"]
        considering = [f for f in items if f["rel_type"] == "CONSIDERING"]
        return {"works_at_current": len(current), "considering_facts": len(considering), "episodic_memories": len(rows)}

    def s5_finding(self) -> dict[str, Any]:
        with self.sdk("research-agent") as kg:
            kg.build_context(
                task="What do we know about Sarah's intended business?", subject_ids=[self.ids["sarah"]], profile="research", max_tokens=600
            )
            acc = kg.ingest_interaction(
                source_system="research-notes",
                source_ref="evt_005",
                kind="document",
                content_type="text/plain",
                content=EVT005,
                occurred_at="2026-10-13T09:00:00Z",
                subject_refs=[{"entity_id": self.ids["sarah"]}],
            )
            note = kg.save_working_memory("r7", NOTE)
            finding = kg.propose_memory(
                type="semantic",
                content=FINDING,
                scopes=[{"kind": "project", "ref": PROJECT}],
                source_event_ids=[str(acc.event_id)],
                subject_ids=[self.ids["sarah"]],
                purpose="research finding",
            )
        self.ids.update(finding=str(finding.memory_id), note=str(note.memory_id))
        self.settle()
        row = admin.get(self.ws, f"/v1/memories/{self.ids['finding']}").json()
        assert row["status"] == "active", row["status"]
        return {"finding_status": row["status"], "authority_class": row.get("authority_class"), "note_status": note.status}

    def s6_planning_context(self) -> dict[str, Any]:
        out = self.build("planning-agent", task=TASK, subject_ids=[self.ids["sarah"]], profile="planning", max_tokens=600, project=PROJECT)
        self.say(show_context(out, title="planning context (600 tokens)"))
        body = out["context_text"]
        used = out["budgets"]["used_tokens"]
        checks = {
            "status_ok": out["status"] in ("ok", "complete"),
            "acme_em_current": "Acme" in body and "Engineering Manager" in body,
            "finding_present": "state licence" in body,
            "no_working_note": NOTE not in body,
            "no_cycling": "cycling" not in body.lower(),
            "within_budget": used <= 600,
            "unknowns": bool(out["unknowns"]),
            "citations": bool(out.get("citations") or any(i.get("evidence") for i in out["memories"])),
        }
        bad = [k for k, v in checks.items() if not v]
        assert not bad, f"unmet: {bad}"
        self.data["planning_ctx"] = out
        return {"tokens": f"{used}/600", "lines": len(context_lines(out)), "unknowns": len(out["unknowns"])}

    def ingest(self, source_ref: str, content: str, subjects: list[str], occurred_at: str) -> str:
        r = admin.post(
            self.ws,
            "/v1/interactions",
            {
                "source_system": "chat",
                "source_ref": source_ref,
                "kind": "message",
                "content": content,
                "occurred_at": occurred_at,
                "subject_refs": [{"entity_id": s} for s in subjects],
            },
            key=self.ws.keys["conversation-app"],
        )
        return str(r["event_id"])

    def s7_job_change(self) -> dict[str, Any]:
        key = self.ws.keys["conversation-app"]
        end = admin.post(
            self.ws,
            f"/v1/relationships/{self.ids['em']}/end",
            {
                "valid_to": "2027-02-01T00:00:00Z",
                "source_recorded_at": "2027-02-05T09:00:00Z",
                "source_system": "hr",
                "source_ref": "evt_006",
            },
            key=key,
        )

        def facts(**q: Any) -> list[dict[str, Any]]:
            return list(admin.get(self.ws, f"/v1/entities/{self.ids['sarah']}/relationships", **q).json())

        def rel(**q: Any) -> list[dict[str, Any]]:
            return [f for f in facts(**q) if f["rel_type"] == "WORKS_AT"]

        now_ = rel()
        dec = rel(as_of="2026-12-01T00:00:00Z")
        mar = rel(as_of="2027-03-01T00:00:00Z")
        # known_at is record time: the CONSIDERING fact was recorded with evt_003, so it was not known just before that event
        known_now = [f for f in facts() if f["rel_type"] == "CONSIDERING"]
        known_before = [f for f in facts(known_at=self.data["before_evt003"]) if f["rel_type"] == "CONSIDERING"]
        assert dec, "on 2026-12-01 Sarah still works at Acme"
        assert not mar, f"on 2027-03-01 Acme is historical, not current: {mar}"
        assert known_now and not known_before, "known_at: CONSIDERING is known now and was not known before evt_003 was recorded"
        out = self.build(
            "planning-agent",
            task=TASK,
            subject_ids=[self.ids["sarah"]],
            profile="planning",
            max_tokens=600,
            project=PROJECT,
            as_of="2027-03-01T00:00:00Z",
        )
        self.say(show_context(out, title="planning context as_of 2027-03-01"))
        mem = admin.get(self.ws, f"/v1/memories/{self.ids['mem_episodic']}").json()
        assert mem["status"] == "active", "the entrepreneurship memory is not rewritten"
        return {
            "current_now": len(now_),
            "as_of_2026_12": len(dec),
            "as_of_2027_03": len(mar),
            "considering_known_now": len(known_now),
            "considering_known_before_evt003": len(known_before),
            "end_status": end.get("projection_status"),
        }

    def s8_unauthorized(self) -> dict[str, Any]:
        other = fresh_workspace(self.env, "other")
        okey = admin.register_principal(other, "other-agent", "agent", allowed_profiles=["planning"])
        self.data["other"] = other
        ghost = str(uuid.uuid4())

        def shape(r: httpx.Response) -> dict[str, Any]:
            body = r.json()
            err = body.get("error", body)
            return {"status": r.status_code, "code": err.get("code"), "message": err.get("message")}

        real = shape(admin.get(other, f"/v1/entities/{self.ids['sarah']}", key=okey))
        fake = shape(admin.get(other, f"/v1/entities/{ghost}", key=okey))
        assert real == fake and real["status"] == 404, (real, fake)
        with KGClientSync(self.env.base_url, okey) as kg:
            outs: list[tuple[str, Any]] = []
            for sid in (self.ids["sarah"], ghost):
                try:
                    outs.append(
                        ("ok", kg.build_context(task=TASK, subject_ids=[sid], profile="planning", max_tokens=600).model_dump(mode="json"))
                    )
                except Exception as exc:
                    outs.append((type(exc).__name__, str(getattr(exc, "code", ""))))
            assert outs[0][0] == outs[1][0], f"foreign and missing subject must answer alike: {outs[0][0]} vs {outs[1][0]}"
            if outs[0][0] == "ok":
                assert not outs[0][1]["facts"] and not outs[0][1]["memories"], "nothing may come back"
        leaked = [w for w in ("Sarah", "Acme", "Michael") if outs[0][0] == "ok" and w in outs[0][1]["context_text"]]
        assert not leaked, leaked
        un = self.build("ungranted-agent", task=TASK, subject_ids=[self.ids["sarah"]], profile="planning", max_tokens=600)
        body = un["context_text"]
        assert FINDING not in body and "entrepreneurship" not in body.lower() and not un["memories"], (
            "an agent with no grant sees no restricted memory"
        )
        return {"foreign_vs_missing": real, "other_workspace_context": outs[0][0], "ungranted_memories": len(un["memories"])}

    def s9_correction(self) -> dict[str, Any]:
        s, m = self.ids["sarah"], self.ids["michael"]
        eid = self.ingest("evt_manager", "Michael is Sarah's manager.", [s, m], "2026-10-14T09:00:00Z")
        d = waits.wait_interaction(self.ws, eid, timeout=240)
        self.settle()
        facts = admin.get(self.ws, f"/v1/entities/{m}/relationships", include="proposed").json()
        mf = [f for f in facts if f["rel_type"] == "MANAGES"]
        assert mf, f"no MANAGES fact extracted (processing {d['processing_status']})"
        fid = mf[0]["id"]
        task = "What do we know about Michael and Sarah?"
        before = self.build("admin", task=task, subject_ids=[s, m], profile="planning", max_tokens=600)
        shown = [ln for ln in before["context_text"].splitlines() if "manages" in ln.lower()]
        assert shown and any(w in shown[0].lower() for w in ("unverified", "reported")), f"MANAGES must be labelled unverified: {shown}"
        rev = f'W/"rev-{admin.get(self.ws, f"/v1/relationships/{fid}").json()["revision"]}"'
        with httpx.Client(
            base_url=self.env.base_url,
            headers={"authorization": f"Bearer {self.ws.admin_key}", "idempotency-key": str(uuid.uuid4()), "if-match": rev or ""},
            timeout=60,
        ) as c:
            r = c.post(f"/v1/relationships/{fid}/corrections", json={"kind": "reject", "reason_code": "incorrect_extraction"})
        assert r.status_code == 200, r.text
        after = self.build("admin", task=task, subject_ids=[s, m], profile="planning", max_tokens=600)
        left = [ln for ln in after["context_text"].splitlines() if "manages" in ln.lower()]
        assert not left, f"the rejected claim must not appear: {left}"
        ep = admin.get(self.ws, f"/v1/interactions/{eid}")
        assert ep.status_code == 200, "the original episode is retained"
        return {"labelled_unverified": True, "rejected": True, "gone_from_next_context": True, "episode_retained": True}

    def s10_deletion(self) -> dict[str, Any]:
        m = self.ids["michael"]
        req = admin.post(
            self.ws, "/v1/deletion-requests", {"kind": "subject", "target_id": m, "reason_code": "data_subject_request"}, expect=(202,)
        )
        rid = req["request_id"]
        gone = admin.get(self.ws, f"/v1/entities/{m}").status_code
        search = admin.get(self.ws, "/v1/memories")  # list still works; Michael's records are gone
        ctx = self.build(
            "planning-agent",
            task="What do we know about Michael?",
            subject_ids=[self.ids["sarah"]],
            profile="planning",
            max_tokens=600,
            project=PROJECT,
        )
        assert gone == 404, gone
        assert "Michael" not in ctx["context_text"], "reads are blocked immediately"
        _ = search
        final = waits.wait_until(
            lambda: (
                (s := admin.get(self.ws, f"/v1/deletion-requests/{rid}").json())["status"]
                in ("completed", "completed_with_exceptions", "failed"),
                s,
            ),
            timeout=300,
            what="the purge",
        )
        assert final["status"] in ("completed", "completed_with_exceptions"), final["status"]
        receipt = final.get("receipt") or {}
        assert receipt, "a receipt is issued"
        return {
            "purge": final["status"],
            "receipt_stores": sorted((receipt.get("stores") or {}).keys())[:6],
            "id_residuals": (receipt.get("verification") or {}).get("id_residuals"),
        }

    def s11_outage(self) -> dict[str, Any]:
        mock.inject_fault(self.env, "provider_unavailable", 200, "chat")
        try:
            eid = self.ingest("evt_outage", "Sarah Johnson: I work at NewCo now.", [self.ids["sarah"]], "2026-10-15T09:00:00Z")
            time.sleep(8)
            d = admin.get(self.ws, f"/v1/interactions/{eid}").json()
            assert d["processing_status"] != "indexed", "with the model down the event cannot be indexed yet"
            during = self.build(
                "planning-agent",
                task=TASK,
                subject_ids=[self.ids["sarah"]],
                profile="planning",
                max_tokens=600,
                project=PROJECT,
                allow_partial=True,
            )
            assert "Acme" in during["context_text"], "context still serves what Postgres holds"
        finally:
            mock.clear_faults(self.env)
        admin.post(self.ws, f"/v1/interactions/{eid}/replay", {"reason_code": "fault_cleared"})
        done = waits.wait_interaction(self.ws, eid, timeout=240)
        assert done["processing_status"] in ("indexed", "partial"), done["processing_status"]
        return {
            "during_outage": d["processing_status"],
            "context_status_during": during["status"],
            "after_replay": done["processing_status"],
        }

    def run_all(self) -> None:
        rules = [
            {"when": {"alias": "oso/general", "schema_name": "MemoryCandidates", "contains": [t]}, "response": {"candidates": []}}
            for t in ("work at NewCo now", "Michael is Sarah's manager")
        ]
        with mock.mock_pack(self.env, rules):
            self.step("1", "Admin publishes the ontology; principals registered", ["ONT-01", "ONT-02"], self.s1_ontology)
            self.step(
                "2", "Trusted app writes evt_001/evt_002 as commands (no model call, idempotent)", ["AT-01", "ONT-06"], self.s2_commands
            )
            self.step("3", "evt_003 conversation is ingested and processed", ["ING-01"], self.s3_conversation)
            self.step(
                "4",
                "Current employment is Acme; a CONSIDERING fact and an episodic memory exist",
                ["AT-02", "AT-03", "AT-04", "AT-05"],
                self.s4_facts_and_memory,
            )
            self.step("5", "Research Agent publishes a sourced finding", ["AT-10", "AGT"], self.s5_finding)
            self.step(
                "6",
                "Planning Agent gets a 600-token planning context",
                ["AT-06", "AT-07", "AT-08", "AT-09", "AT-12"],
                self.s6_planning_context,
            )
            self.step(
                "7", "2027 job change: Acme becomes historical; as_of and known_at differ", ["AT-02", "AT-03", "DEC-12"], self.s7_job_change
            )
            self.step("8", "An other_demo agent and an ungranted agent are denied", ["AT-11", "AT-14", "SEC-01"], self.s8_unauthorized)
            self.step(
                "9",
                "Reviewer rejects the wrong 'Michael manages Sarah'; the next context omits it",
                ["AT-13", "MEM-06"],
                self.s9_correction,
            )
            self.step("10", "Deletion of a subject: reads blocked at once, purge, receipt", ["AT-16", "AT-17"], self.s10_deletion)
            self.step("V", "Model-outage variant: queued, context still serves, replay completes", ["FI", "AT-08"], self.s11_outage)
