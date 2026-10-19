"""`make demo-crm` / `make eval-crm`: the second domain (Project / Customer CRM) in a fresh workspace.

Only configuration is CRM-specific: ontology.yaml (types, relationships, rules, context profiles), events/timeline.json, mock_rules.py (offline model answers) and
benchmark/cases.yaml. The platform, SDK and kit are the same ones the Sarah example uses."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

import yaml

from examples._kit import admin, mock, waits
from examples._kit.env import ROOT, Env, load_env
from examples._kit.report import Report
from examples._kit.runner import Runner
from examples._kit.show import show_context
from examples._kit.workspace import fresh_workspace
from examples.domains.crm.mock_rules import rules_for

HERE = Path(__file__).parent
ONTOLOGY = HERE / "ontology.yaml"
TIMELINE = json.loads((HERE / "events" / "timeline.json").read_text())
CASES = yaml.safe_load((HERE / "benchmark" / "cases.yaml").read_text())["cases"]
STRICT = {"leakage": 0, "budget_adherence": 1.0, "attribution": 1.0}
PROPOSED = {"recall": 1.0}  # the mock is deterministic, so every expectation must hold


def scope(kind: str, ref: str, *priv: str) -> dict[str, Any]:
    return {"scope_kind": kind, "scope_ref": ref, "privileges": list(priv)}


class Crm(Runner):
    def entity_types(self) -> list[str]:
        spec = yaml.safe_load(ONTOLOGY.read_text())
        return list(spec["entity_types"])

    # ---- the world ---------------------------------------------------------------------------------------------------------------------
    def setup(self) -> dict[str, Any]:
        me = admin.get(self.ws, "/v1/me").json()["principal"]["id"]
        self.ws.keys["admin"] = self.ws.admin_key
        for kind in ("subject", "project"):
            admin.post(self.ws, f"/v1/principals/{me}/grants", scope(kind, "*", "read", "propose", "publish"))
        admin.register_principal(
            self.ws, "conversation-app", "app", trusted_source=True, grants=[scope("subject", "*", "read", "propose", "publish")]
        )
        active = admin.get(self.ws, "/v1/ontologies/active").json()["version"]
        return {
            "ontology": f"{active.get('version')} ({str(active.get('content_hash', ''))[:12]})",
            "profiles": ["account_review", "renewal_prep", "support_triage"],
        }

    def commands(self) -> dict[str, Any]:
        before = self.mock_requests()
        key = self.ws.keys["conversation-app"]
        for e in TIMELINE["entities"]:
            body = {k: v for k, v in e.items() if k not in ("key", "type", "name")}
            out = admin.post(self.ws, "/v1/entities", {"type": e["type"], "name": e["name"], "source_system": "crm", **body}, key=key)
            self.ids[e["key"]] = out["entity_id"]
        for r in TIMELINE["relationships"]:
            body = {k: v for k, v in r.items() if k not in ("id", "type", "source", "target")}
            out = admin.post(
                self.ws, "/v1/relationships",
                {"type": r["type"], "source": {"entity_id": self.ids[r["source"]]}, "target": {"entity_id": self.ids[r["target"]]}, "source_system": "crm", **body},
                key=key,
            )  # fmt: skip
            self.ids["fact:" + r["id"]] = out["fact_id"]
        m = TIMELINE["marcus_leaves"]  # a backdated authoritative update: the source learned it on 2026-09-10
        admin.post(
            self.ws, f"/v1/relationships/{self.ids['fact:marcus_globex']}/end",
            {"valid_to": m["valid_to"], "source_recorded_at": m["source_recorded_at"], "source_system": "crm", "source_ref": m["source_ref"]},
            key=key,
        )  # fmt: skip
        admin.post(
            self.ws, "/v1/relationships",
            {"type": "WORKS_FOR", "source": {"entity_id": self.ids["marcus"]}, "target": {"entity_id": self.ids[m["joins"]]}, "valid_from": "2026-09-01", "attributes": {"role": m["role"]}, "source_system": "crm", "source_ref": "crm_012"},
            key=key,
        )  # fmt: skip
        calls = self.mock_requests() - before
        assert calls == 0, f"commands made {calls} model call(s)"
        return {"entities": len(TIMELINE["entities"]), "relationships": len(TIMELINE["relationships"]) + 1, "model_calls": calls}

    def agents(self) -> dict[str, Any]:
        i = self.ids
        admin.register_principal(self.ws, "globex-am", "agent", allowed_profiles=["account_review", "renewal_prep"],
                                 grants=[scope("subject", i["dana"], "read"), scope("subject", i["marcus"], "read"), scope("project", "acct-globex", "read", "propose", "publish")])  # fmt: skip
        admin.register_principal(self.ws, "initech-am", "agent", allowed_profiles=["account_review", "renewal_prep"],
                                 grants=[scope("subject", i["priya"], "read"), scope("project", "acct-initech", "read", "propose", "publish")])  # fmt: skip
        admin.register_principal(self.ws, "support-agent", "agent", allowed_profiles=["support_triage"],
                                 grants=[scope("subject", i["dana"], "read"), scope("subject", i["marcus"], "read"), scope("subject", i["priya"], "read")])  # fmt: skip
        return {
            "agents": [
                "globex-am (Globex contacts + acct-globex)",
                "initech-am (Initech contacts + acct-initech)",
                "support-agent (all contacts, no account project)",
            ]
        }

    def conversations(self) -> dict[str, Any]:
        stages = {}
        for c in TIMELINE["conversations"]:
            eid = self.ingest(c["ref"], c["content"], [self.ids[k] for k in c["subjects"]], c["occurred_at"])
            d = waits.wait_interaction(self.ws, eid, timeout=240)
            assert d["processing_status"] == "indexed", (c["ref"], d["processing_status"], [(s["stage"], s["status"]) for s in d["stages"]])
            stages[c["ref"]] = d["processing_status"]
            self.ids["event:" + c["ref"]] = eid
        self.settle()
        ap = TIMELINE["account_plan"]
        with self.sdk("globex-am") as kg:
            note = kg.ingest_interaction(source_system="account-notes", source_ref=ap["ref"], kind="document", content_type="text/plain", content=ap["content"],
                                         occurred_at=ap["occurred_at"], subject_refs=[{"entity_id": self.ids[k]} for k in ap["subjects"]])  # fmt: skip
            plan = kg.propose_memory(type="semantic", content=ap["content"], scopes=[{"kind": "project", "ref": "acct-globex"}], subject_ids=[self.ids["dana"]],
                                     source_event_ids=[str(note.event_id)], purpose="account plan")  # fmt: skip
        waits.wait_interaction(self.ws, str(note.event_id), key=self.ws.keys["globex-am"], timeout=240)
        self.settle()
        assert plan.status == "active", plan.status
        return {"conversations": stages, "account_plan": plan.status}

    # ---- the demo output -----------------------------------------------------------------------------------------------------------------
    def show(self, who: str, title: str, **kw: Any) -> dict[str, Any]:
        out = self.build(who, **kw)
        self.say(show_context(out, title=title))
        return out

    # ---- the benchmark -------------------------------------------------------------------------------------------------------------------
    def evaluate(self) -> dict[str, Any]:
        rows, leaks, budget_ok, attr_ok, attr_n, hits, wants = [], 0, 0, 0, 0, 0, 0
        for c in CASES:
            kw: dict[str, Any] = {
                "task": c["task"],
                "subject_ids": [self.ids[s] for s in c["subjects"]],
                "profile": c["profile"],
                "max_tokens": c["max_tokens"],
            }
            for k in ("project", "as_of"):
                if c.get(k):
                    kw[k] = c[k]
            out = self.build(c["principal"], **kw)
            text = out["context_text"].lower()
            whole = json.dumps(out).lower()
            missing = [t for t in c.get("expect", []) if t.lower() not in text]
            present = [t for t in c.get("forbid", []) if t.lower() in text]
            leaked = [t for t in c.get("leak", []) if t.lower() in whole]
            missing += [
                f"unknown:{u}"
                for u in c.get("unknowns", [])
                if not any(u.lower() in str(x.get("text", "")).lower() for x in out["unknowns"])
            ]
            items = [*out["facts"], *out["memories"]]
            cited = sum(1 for i in items if i.get("citations"))
            used = out["budgets"]["used_tokens"]
            hits += len(c.get("expect", [])) + len(c.get("unknowns", [])) - len(missing)
            wants += len(c.get("expect", [])) + len(c.get("unknowns", []))
            leaks += len(leaked)
            budget_ok += used <= c["max_tokens"]
            attr_ok, attr_n = attr_ok + cited, attr_n + len(items)
            ok = not missing and not present and not leaked and used <= c["max_tokens"]
            rows.append(
                {
                    "id": c["id"],
                    "title": c["title"],
                    "ok": ok,
                    "tokens": f"{used}/{c['max_tokens']}",
                    "missing": missing,
                    "forbidden_present": present,
                    "leaked": leaked,
                }
            )
        metrics = {
            "recall": hits / max(wants, 1),
            "leakage": leaks,
            "budget_adherence": budget_ok / len(CASES),
            "attribution": attr_ok / max(attr_n, 1),
        }
        passed = (
            metrics["leakage"] == 0
            and metrics["budget_adherence"] >= 1.0
            and metrics["attribution"] >= 1.0
            and metrics["recall"] >= PROPOSED["recall"]
            and all(r["ok"] for r in rows)
        )
        return {"passed": passed, "metrics": metrics, "cases": rows}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--eval", action="store_true", help="print only the benchmark scorecard (make eval-crm)")
    ap.add_argument("--live", action="store_true", help="owner action: use the real router instead of the mock")
    ap.add_argument("--reports", type=Path, default=ROOT / "examples" / "domains" / "crm" / "reports")
    args = ap.parse_args()
    env: Env = load_env(live=args.live)
    ws = fresh_workspace(env, "crm", ontology=ONTOLOGY)
    report = Report("CRM second domain", "live" if env.live else "mock")
    crm = Crm(env, ws, report, out=(lambda _m: None) if args.eval else print)
    print(f"workspace {ws.slug}")
    types = crm.entity_types()
    by_name = {e["name"]: e["type"] for e in TIMELINE["entities"]}
    with mock.mock_pack(env, rules_for([*TIMELINE["conversations"], TIMELINE["account_plan"]], by_name, types)):
        crm.step("1", "Publish the CRM ontology; register the app", ["P-01", "ONT-01"], crm.setup)
        crm.step(
            "2",
            "CRM system of record writes customers, contacts, products, projects and a backdated job change (no model call)",
            ["AT-01"],
            crm.commands,
        )
        crm.step("3", "Register account managers and a support agent with different grants", ["AGT"], crm.agents)
        crm.step(
            "4",
            "Meeting notes and a support ticket are ingested; the account plan is published to the Globex account project",
            ["ING-01", "MEM"],
            crm.conversations,
        )
        if not args.eval:
            crm.step("5", "Renewal prep for Globex", ["CTX"], lambda: summary(crm.show("globex-am", "Globex renewal_prep", task="Prepare the Globex renewal", subject_ids=[crm.ids["globex"], crm.ids["dana"]], profile="renewal_prep", max_tokens=800, project="acct-globex")))  # fmt: skip
            crm.step("6", "Renewal prep for Initech: the missing renewal date and budget owner show as unknowns", ["CTX"], lambda: summary(crm.show("initech-am", "Initech renewal_prep", task="Prepare the Initech renewal", subject_ids=[crm.ids["initech"], crm.ids["priya"]], profile="renewal_prep", max_tokens=800, project="acct-initech")))  # fmt: skip
        result = crm.evaluate()
    for r in result["cases"]:
        print(
            f"  {'ok  ' if r['ok'] else 'FAIL'} {r['id']:<34} {r['tokens']:>9}  {' '.join(['missing=' + str(r['missing'])] if r['missing'] else [])} {'forbidden=' + str(r['forbidden_present']) if r['forbidden_present'] else ''} {'LEAK=' + str(r['leaked']) if r['leaked'] else ''}"
        )
    m = result["metrics"]
    print(
        f"benchmark: recall {m['recall']:.2f}, leakage {m['leakage']}, budget {m['budget_adherence']:.2f}, attribution {m['attribution']:.2f} -> {'PASS' if result['passed'] else 'FAIL'}"
    )
    args.reports.mkdir(parents=True, exist_ok=True)
    stem = f"crm-{time.strftime('%Y-%m-%d')}"
    (args.reports / f"{stem}-benchmark.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    report.write(args.reports, stem)
    ok = report.passed and result["passed"]
    print(
        f"\n{'PASS' if ok else 'FAIL'}: {sum(s.passed for s in report.steps)}/{len(report.steps)} steps, benchmark {'PASS' if result['passed'] else 'FAIL'}"
    )
    return 0 if ok else 1


def summary(out: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": out["status"],
        "tokens": out["budgets"]["used_tokens"],
        "facts": len(out["facts"]),
        "memories": len(out["memories"]),
        "unknowns": [u["text"] for u in out["unknowns"]],
    }


if __name__ == "__main__":
    sys.exit(main())
