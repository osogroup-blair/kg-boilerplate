"""`make demo-support`: the support-desk domain from the tutorial, in a fresh workspace. Configuration only: ontology.yaml and events/timeline.json are the whole domain;
the offline model answers are generated from the timeline with the same helper the CRM example uses."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

import yaml

from examples._kit import admin, mock, waits
from examples._kit.env import ROOT, load_env
from examples._kit.report import Report
from examples._kit.runner import Runner
from examples._kit.show import show_context
from examples._kit.workspace import fresh_workspace, save_for_exploring
from examples.domains.crm.mock_rules import rules_for  # a generic helper: it builds model answers from a timeline, whatever the domain

HERE = Path(__file__).parent
ONTOLOGY = HERE / "ontology.yaml"
TIMELINE = json.loads((HERE / "events" / "timeline.json").read_text())


def scope(kind: str, ref: str, *priv: str) -> dict[str, Any]:
    return {"scope_kind": kind, "scope_ref": ref, "privileges": list(priv)}


class Support(Runner):
    def mock_rules(self) -> list[dict[str, Any]]:
        by_name = {e["name"]: e["type"] for e in TIMELINE["entities"]}
        types = list(yaml.safe_load(ONTOLOGY.read_text())["entity_types"])
        return rules_for(TIMELINE["conversations"], by_name, types)

    def setup(self) -> dict[str, Any]:
        me = admin.get(self.ws, "/v1/me").json()["principal"]["id"]
        self.ws.keys["admin"] = self.ws.admin_key
        for kind in ("subject", "project"):
            admin.post(self.ws, f"/v1/principals/{me}/grants", scope(kind, "*", "read", "propose", "publish"))
        admin.register_principal(
            self.ws, "desk-app", "app", trusted_source=True, grants=[scope("subject", "*", "read", "propose", "publish")]
        )
        active = admin.get(self.ws, "/v1/ontologies/active").json()["version"]
        return {"ontology": f"{active.get('version')} ({str(active.get('content_hash', ''))[:12]})"}

    def commands(self) -> dict[str, Any]:
        before = self.mock_requests()
        key = self.ws.keys["desk-app"]
        for e in TIMELINE["entities"]:
            body = {k: v for k, v in e.items() if k not in ("key", "type", "name")}
            out = admin.post(self.ws, "/v1/entities", {"type": e["type"], "name": e["name"], "source_system": "desk", **body}, key=key)
            self.ids[e["key"]] = out["entity_id"]
        for r in TIMELINE["relationships"]:
            body = {k: v for k, v in r.items() if k not in ("id", "type", "source", "target")}
            admin.post(
                self.ws, "/v1/relationships",
                {"type": r["type"], "source": {"entity_id": self.ids[r["source"]]}, "target": {"entity_id": self.ids[r["target"]]}, "source_system": "desk", **body},
                key=key,
            )  # fmt: skip
        ra = TIMELINE["reassign"]  # a new ASSIGNED_TO ends the previous one: the `succession` rule, no model involved
        admin.post(
            self.ws, "/v1/relationships",
            {"type": "ASSIGNED_TO", "source": {"entity_id": self.ids[ra["ticket"]]}, "target": {"entity_id": self.ids[ra["to"]]}, "valid_from": ra["valid_from"], "source_system": "desk", "source_ref": ra["source_ref"]},
            key=key,
        )  # fmt: skip
        calls = self.mock_requests() - before
        assert calls == 0, f"commands made {calls} model call(s)"
        return {"entities": len(TIMELINE["entities"]), "relationships": len(TIMELINE["relationships"]) + 1, "model_calls": calls}

    def agents(self) -> dict[str, Any]:
        i = self.ids
        admin.register_principal(
            self.ws, "triage-agent", "agent", allowed_profiles=["ticket_triage"], grants=[scope("subject", i["alex"], "read")]
        )
        admin.register_principal(self.ws, "ungranted-agent", "agent", allowed_profiles=["ticket_triage"])
        return {"agents": ["triage-agent (read on Alex Kim only)", "ungranted-agent (no grants)"]}

    def conversations(self) -> dict[str, Any]:
        stages = {}
        for c in TIMELINE["conversations"]:
            eid = self.ingest(c["ref"], c["content"], [self.ids[k] for k in c["subjects"]], c["occurred_at"], key=self.ws.keys["desk-app"])
            d = waits.wait_interaction(self.ws, eid, timeout=240)
            assert d["processing_status"] == "indexed", (c["ref"], d["processing_status"], [(s["stage"], s["status"]) for s in d["stages"]])
            stages[c["ref"]] = d["processing_status"]
        self.settle()
        return {"conversations": stages}

    def triage(self) -> dict[str, Any]:
        out = self.build(
            "triage-agent",
            task="Triage TCK-101: Alex Kim says the dashboard export fails for large files",
            subject_ids=[self.ids["t101"], self.ids["alex"]],
            profile="ticket_triage",
            max_tokens=600,
        )
        self.say(show_context(out, title="ticket_triage for TCK-101"))
        text = out["context_text"]
        checks = {
            "assigned_to_riya": "assigned to Riya Patel" in text,
            "sam_is_not_current": "assigned to Sam Ortiz" not in text,
            "the_2gb_note": "2 GB" in text,
            "within_budget": out["budgets"]["used_tokens"] <= 600,
        }
        bad = [k for k, v in checks.items() if not v]
        assert not bad, f"unmet: {bad}\n{text}"
        return {"tokens": f"{out['budgets']['used_tokens']}/600", **checks}

    def time_travel(self) -> dict[str, Any]:
        out = self.build(
            "triage-agent",
            task="Who was working TCK-101?",
            subject_ids=[self.ids["t101"]],
            profile="ticket_triage",
            max_tokens=600,
            as_of="2026-10-05T00:00:00Z",
        )
        text = out["context_text"]
        assert "assigned to Sam Ortiz" in text and "assigned to Riya Patel" not in text, text
        return {"as_of_2026_10_05": "assigned to Sam Ortiz", "now": "assigned to Riya Patel"}

    def unknown_and_hidden(self) -> dict[str, Any]:
        out = self.build(
            "triage-agent",
            task="Triage ticket TCK-102: slow dashboard load",
            subject_ids=[self.ids["t102"]],
            profile="ticket_triage",
            max_tokens=600,
        )
        self.say(show_context(out, title="ticket_triage for TCK-102 (Jo Park is hidden from this agent)"))
        text = out["context_text"]
        unknowns = [str(u.get("text", "")) for u in out["unknowns"]]
        assert any("severity" in u.lower() for u in unknowns), unknowns
        assert "Jo Park" not in json.dumps(out), "an agent with no grant on Jo Park must not see Jo Park anywhere in the package"
        assert "a minute to load" not in text
        return {"unknowns": unknowns, "jo_park_visible": False}

    def outsider(self) -> dict[str, Any]:
        out = self.build(
            "ungranted-agent",
            task="Triage ticket TCK-101",
            subject_ids=[self.ids["t101"], self.ids["alex"]],
            profile="ticket_triage",
            max_tokens=600,
        )
        whole = json.dumps(out)
        assert "Alex Kim" not in whole and "2 GB" not in whole, "an agent with no grants sees nothing about Alex Kim"
        return {"status": out["status"], "alex_visible": False}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--live", action="store_true", help="owner action: use the real router instead of the mock")
    ap.add_argument("--reports", type=Path, default=ROOT / "examples" / "domains" / "support" / "reports")
    args = ap.parse_args()
    env = load_env(live=args.live)
    ws = fresh_workspace(env, "support", ontology=ONTOLOGY)
    print(f"workspace {ws.slug}")
    report = Report("Support desk (tutorial domain)", "live" if env.live else "mock")
    s = Support(env, ws, report)
    with mock.mock_pack(env, s.mock_rules()):
        s.step("1", "Publish the support-desk ontology; register the desk app", ["ONT-01"], s.setup)
        s.step(
            "2",
            "The desk system writes customers, requesters, engineers, tickets and a reassignment by command (no model call)",
            ["AT-01"],
            s.commands,
        )
        s.step("3", "Register a triage agent (a grant on one requester) and an agent with no grants", ["AGT"], s.agents)
        s.step("4", "Two support-call notes are ingested and processed", ["ING-01", "MEM"], s.conversations)
        s.step("5", "Triage context for TCK-101: current assignee, the call note, no stale assignment", ["CTX"], s.triage)
        s.step("6", "as_of 2026-10-05 the ticket was Sam's", ["AT-02", "AT-03"], s.time_travel)
        s.step(
            "7",
            "TCK-102: severity is reported unknown, and a requester the agent has no grant on is nowhere in the package",
            ["AT-06", "AT-11"],
            s.unknown_and_hidden,
        )
        s.step("8", "An agent with no grants learns nothing about Alex Kim", ["SEC-01"], s.outsider)
    print(f"keys and ids to explore this workspace: {save_for_exploring(ws, 'support', s.ids)}")
    args.reports.mkdir(parents=True, exist_ok=True)
    report.write(args.reports, f"support-{time.strftime('%Y-%m-%d')}")
    print(f"\n{'PASS' if report.passed else 'FAIL'}: {sum(x.passed for x in report.steps)}/{len(report.steps)} steps")
    return 0 if report.passed else 1


if __name__ == "__main__":
    sys.exit(main())
