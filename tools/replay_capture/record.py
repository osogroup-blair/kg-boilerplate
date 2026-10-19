"""Records the platform's real HTTP traffic while the Sarah and CRM examples run, and writes it as tapes for the public site's static playgrounds.

Black-box like the examples: it wraps httpx (which the SDK and the example kit use), so it sees exactly what a client sees and never reads a database.
Run it through `make tapes` against the dev stack (`make up seed`); each run creates its own workspaces (`ex-*`).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlparse

import httpx

from examples._kit import admin, mock
from examples._kit.env import ROOT, load_env
from examples._kit.report import Report
from examples._kit.workspace import fresh_workspace
from examples.sarah.steps import PROJECT, TASK, Scenario

OUT = ROOT / ".local" / "tapes"  # raw recordings hold plaintext keys: git-ignored; `normalize` writes the committed tapes
ONTOLOGY = ROOT / "examples" / "ontologies" / "personal-knowledge-governance.yaml"
TAPE_VERSION = 1


class Recorder:
    """Collects (step, call) pairs from every httpx request to the platform while `active`."""

    def __init__(self, base_url: str) -> None:
        self.base = urlparse(base_url)
        self.callers: dict[str, str] = {}  # plaintext key -> handle
        self.steps: dict[str, list[dict[str, Any]]] = {}  # tape -> ordered steps
        self.tape: str | None = None
        self.paused = False

    # ---- bookkeeping -------------------------------------------------------------------------------------------------------------
    def begin(self, tape: str, sid: str, title: str, **extra: Any) -> None:
        self.tape = tape
        self.steps.setdefault(tape, []).append({"id": sid, "title": title, "calls": [], **extra})

    def add(self, call: dict[str, Any]) -> None:
        if self.tape is None or self.paused or not self.steps.get(self.tape):
            return
        self.steps[self.tape][-1]["calls"].append(call)

    def note_key(self, key: str, handle: str) -> None:
        self.callers[key] = handle

    # ---- httpx hook --------------------------------------------------------------------------------------------------------------
    def wants(self, request: httpx.Request) -> bool:
        return request.url.host == self.base.hostname and request.url.port == self.base.port and request.url.path.startswith(("/v1/", "/health"))

    def record(self, request: httpx.Request, response: httpx.Response) -> None:
        if not self.wants(request):
            return
        token = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        try:
            req_body = json.loads(request.content) if request.content else None
        except ValueError:
            req_body = None
        try:
            resp_body: Any = response.json()
        except ValueError:
            resp_body = response.text[:2000]
        self.add(
            {
                "method": request.method,
                "path": request.url.path,
                "query": dict(parse_qsl(request.url.query.decode())),
                "caller": self.callers.get(token, "unknown"),
                "request": req_body,
                "status": response.status_code,
                "response": resp_body,
            }
        )

    def install(self) -> None:
        rec = self
        sync_send, async_send = httpx.Client.send, httpx.AsyncClient.send

        def send(self: httpx.Client, request: httpx.Request, **kw: Any) -> httpx.Response:
            response = sync_send(self, request, **kw)
            rec.record(request, response)
            return response

        async def asend(self: httpx.AsyncClient, request: httpx.Request, **kw: Any) -> httpx.Response:
            response = await async_send(self, request, **kw)
            rec.record(request, response)
            return response

        httpx.Client.send = send  # type: ignore[method-assign]
        httpx.AsyncClient.send = asend  # type: ignore[method-assign]


class RecordingScenario(Scenario):
    """The Sarah scenario with the recorder following it, plus the extra captures the playgrounds need after step 7."""

    def __init__(self, rec: Recorder, *a: Any, **kw: Any) -> None:
        super().__init__(*a, **kw)
        self.rec = rec

    def step(self, sid: str, title: str, at: list[str], fn: Any) -> None:
        self.rec.begin("sarah", sid, title, at_ids=at)
        for h, k in self.ws.keys.items():
            self.rec.note_key(k, h)
        super().step(sid, title, at, fn)
        for h, k in self.ws.keys.items():
            self.rec.note_key(k, h)
        self.rec.note_key(self.ws.admin_key, "admin")
        self.snapshot()

    def snapshot(self) -> None:
        """Graph and entity list after the step, tagged so the player can draw before/after."""
        ids = self.ids
        if "sarah" not in ids:
            return
        self.rec.tape = "sarah"
        step = self.rec.steps["sarah"][-1]
        mark = len(step["calls"])
        try:
            admin.get(self.ws, "/v1/entities", limit=50)
            admin.get(self.ws, f"/v1/entities/{ids['sarah']}/graph")
        except Exception:
            pass
        for c in step["calls"][mark:]:
            c["snapshot"] = True

    def s7_job_change(self) -> dict[str, Any]:
        out = super().s7_job_change()
        self.capture_time()
        self.capture_matrix()
        return out

    def s8_unauthorized(self) -> dict[str, Any]:
        out = super().s8_unauthorized()
        return out

    # ---- extra tapes -------------------------------------------------------------------------------------------------------------
    def capture_time(self) -> None:
        sarah = self.ids["sarah"]
        self.rec.begin("time", "t", "Sarah's relationships over time")
        for stamp in (
            "2022-03-15", "2022-12-15", "2023-09-15", "2024-03-15", "2024-09-15", "2025-06-15", "2026-03-15", "2026-10-15",
            "2026-12-15", "2027-01-15", "2027-02-15", "2027-03-15", "2027-06-15",
        ):  # fmt: skip
            admin.get(self.ws, f"/v1/entities/{sarah}/relationships", as_of=f"{stamp}T00:00:00Z")
        admin.get(self.ws, f"/v1/entities/{sarah}/relationships", known_at=self.data["before_evt003"])
        admin.get(self.ws, f"/v1/entities/{sarah}/relationships")

    def capture_matrix(self) -> None:
        self.rec.begin("context-matrix", "m", "Context builds across task, profile, budget and caller")
        profiles = admin.get(self.ws, "/v1/context/profiles").json()
        names = [p["name"] if isinstance(p, dict) else str(p) for p in (profiles if isinstance(profiles, list) else profiles.get("items", []))]
        tasks = [TASK, "What has Sarah done at work so far?", "What should be researched about Sarah's plans?"]
        combos = [("planning-agent", "planning"), ("research-agent", "research")] + [("admin", n) for n in names]
        for task in tasks:
            for handle, profile in combos:
                for budget in (64, 250, 600, 1200):
                    try:
                        self.build(
                            handle,
                            task=task,
                            subject_ids=[self.ids["sarah"]],
                            profile=profile,
                            max_tokens=budget,
                            project=PROJECT,
                        )
                    except Exception as exc:
                        self.say(f"     matrix skip {handle}/{profile}/{budget}: {type(exc).__name__}")

    def capture_permissions(self) -> None:
        time.sleep(20)  # the matrix and the scenario's own builds used up the admin's context-build rate limit: let it refill so no cell of the lens is a 429
        self.rec.begin("permissions", "p", "One question, four callers")
        other = self.data["other"]
        questions = [
            ("GET", f"/v1/entities/{self.ids['sarah']}", None),
            ("GET", f"/v1/entities/{uuid.UUID(int=0)}", None),
            ("GET", f"/v1/entities/{self.ids['sarah']}/relationships", None),
            ("POST", "/v1/memories/search", {"query": "state licence", "limit": 5}),
            ("POST", "/v1/context/build", {"task": TASK, "subject_ids": [self.ids["sarah"]], "profile": "planning", "max_tokens": 600}),
        ]
        callers = [(self.ws, "admin", self.ws.admin_key), (self.ws, "planning-agent", self.ws.keys["planning-agent"]), (self.ws, "ungranted-agent", self.ws.keys["ungranted-agent"]), (other, "other-workspace-agent", other.keys["other-agent"])]
        for method, path, body in questions:
            for ws, handle, key in callers:
                self.rec.note_key(key, handle)
                with ws.client(key) as c:
                    for _ in range(4):
                        if method == "GET":
                            r = c.get(path)
                        else:
                            r = c.post(path, json=body, headers={"idempotency-key": str(uuid.uuid4())} if "build" not in path else {})
                        if r.status_code != 429:
                            break
                        time.sleep(8)

    def run_all(self) -> None:
        # step 8 is where the other workspace appears, so the permission matrix is recorded right after it
        base_step = self.step

        def step(sid: str, title: str, at: list[str], fn: Any) -> None:
            base_step(sid, title, at, fn)
            if sid == "8":
                self.capture_permissions()
                self.rec.tape = None

        self.step = step  # type: ignore[method-assign]
        super().run_all()


def record_crm(rec: Recorder, env: Any) -> dict[str, str]:
    """The CRM example (a second domain, configuration only) with the recorder following it: setup, commands, agents, conversations, graphs, benchmark."""
    from examples.domains.crm.run import ONTOLOGY as CRM_ONTOLOGY
    from examples.domains.crm.run import Crm

    ws = fresh_workspace(env, "tape-crm", ontology=CRM_ONTOLOGY)
    rec.note_key(ws.admin_key, "admin")
    crm = Crm(env, ws, Report("tape crm", "mock"), out=lambda _m: None)

    def sync_keys() -> None:
        for h, k in ws.keys.items():
            rec.note_key(k, h)

    with mock.mock_pack(env, crm.mock_rules()):
        for sid, title, fn in (
            ("1", "Publish the CRM ontology; register the app", crm.setup),
            ("2", "The CRM system of record writes customers, contacts and a backdated job change by command", crm.commands),
            ("3", "Account managers and a support agent are registered with different grants", crm.agents),
            ("4", "Meeting notes and a support ticket are ingested; the account plan is published to one account", crm.conversations),
        ):
            rec.begin("crm", sid, title)
            fn()
            sync_keys()
        rec.begin("crm", "graph", "The graph after the CRM events")
        for key in ("globex", "initech"):
            admin.get(ws, f"/v1/entities/{crm.ids[key]}/graph")
        rec.begin("crm", "bench", "The nine benchmark questions: each context build")
        crm.evaluate()
    return {k: v for k, v in crm.ids.items() if not k.startswith(("fact:", "event:"))}


def fast_polling() -> None:
    """The examples poll an interaction every 0.4 s, which hides the stages a mock-speed pipeline passes through. Poll every 50 ms while recording."""
    from examples._kit import waits

    original = waits.wait_until

    def quick(probe: Any, *, timeout: float = 90, interval: float = 0.4, what: str = "condition") -> Any:  # noqa: ASYNC109
        return original(probe, timeout=timeout, interval=min(interval, 0.05), what=what)

    waits.wait_until = quick  # type: ignore[assignment]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args()
    fast_polling()
    env = load_env()
    rec = Recorder(env.base_url)
    rec.install()
    ws = fresh_workspace(env, "tape", ontology=ONTOLOGY)
    print(f"workspace {ws.slug}")
    rec.note_key(ws.admin_key, "admin")
    scenario = RecordingScenario(rec, env, ws, Report("tape recording", "mock"))
    scenario.run_all()
    ok = scenario.report.passed
    crm_ids = record_crm(rec, env)
    raw = {
        "recorded_with": {"workspace": ws.slug, "ontology": ONTOLOGY.name, "when": time.strftime("%Y-%m-%d")},
        "ids": {**crm_ids, **scenario.ids},
        "tapes": rec.steps,
        "passed": ok,
    }
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "_raw.json").write_text(json.dumps(raw, indent=1, default=str))
    print(f"\nraw tapes -> {args.out / '_raw.json'} ({'PASS' if ok else 'FAIL'}); steps per tape: {({k: len(v) for k, v in rec.steps.items()})}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
