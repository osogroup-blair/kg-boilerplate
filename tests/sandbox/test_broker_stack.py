"""The broker against the real dev stack (`make up seed`): a real workspace is created and seeded, the visitor's keys work, a scripted event is processed, the tour sees
what the visitor did, and the workspace deletion request is accepted. Skipped when the stack is not up."""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from kg_sandbox import seed as seeding
from kg_sandbox.app import Broker, create_app
from kg_sandbox.config import REPO, Config
from kg_sandbox.store import Store

from examples._kit import mock


def _stack_up() -> bool:
    try:
        return httpx.get("http://127.0.0.1:8000/health", timeout=2).status_code == 200
    except httpx.HTTPError:
        return False


pytestmark = pytest.mark.skipif(not _stack_up() and os.environ.get("KG_REQUIRE_STACK") != "1", reason="the dev stack is not up")


def test_a_visitor_session_end_to_end(tmp_path: Path) -> None:
    uv = os.environ.get("UV") or shutil.which("uv") or str(REPO / ".tools/uvenv/bin/uv")
    cfg = Config(db_path=tmp_path / "s.sqlite", cli=(uv, "run", "kg"), pool_size=0, manage_mock=False)
    env = seeding.env_for(cfg)
    broker = Broker(cfg, Store(cfg.db_path))
    app = TestClient(create_app(cfg, broker=broker, background=False))
    with mock.mock_pack(env, seeding.Scenario.mock_rules(None)):  # type: ignore[arg-type]
        r = app.post("/sandbox/sessions", json={"scenario": "sarah"}, headers={"x-forwarded-for": "203.0.113.7"})
        assert r.status_code == 201, r.text
        s: dict[str, Any] = r.json()
        admin = {"authorization": f"Bearer {s['keys']['admin']}"}
        api = httpx.Client(base_url=cfg.api_url, timeout=60)

        # the keys work, and each one is what it says
        me = api.get("/v1/me", headers=admin)
        assert me.status_code == 200
        plan = {"authorization": f"Bearer {s['keys']['planning_agent']}"}
        ctx = api.post(
            "/v1/context/build",
            headers=plan,
            json={
                "task": "Help Sarah assess the implications of starting a business",
                "subject_ids": [s["ids"]["sarah"]],
                "profile": "planning",
                "max_tokens": 600,
                "project": "newco-plan",
            },
        )
        assert ctx.status_code == 200 and ctx.json()["status"] == "complete", ctx.text
        out = api.get(f"/v1/entities/{s['ids']['sarah']}", headers={"authorization": f"Bearer {s['keys']['ungranted_agent']}"})
        assert out.status_code == 404, "the agent with no grants must not see Sarah"
        denied = api.post(
            "/v1/context/build",
            headers={"authorization": f"Bearer {s['keys']['ungranted_agent']}"},
            json={
                "task": "Help Sarah assess the implications of starting a business",
                "subject_ids": [s["ids"]["sarah"]],
                "profile": "planning",
                "max_tokens": 600,
            },
        )
        assert denied.status_code == 200 and denied.json()["status"] == "denied" and "Sarah" not in denied.json()["context_text"], (
            denied.text
        )

        # the tour: a context was built (the outsider's refused build leaves no record, so that task is ticked by the visitor)
        tour = app.get(f"/sandbox/sessions/{s['session_id']}", headers=admin).json()["tour"]
        assert tour["context"] is True and tour["command"] is False and tour["budget"] is False, tour
        api.post(
            "/v1/context/build",
            headers=plan,
            json={
                "task": "Help Sarah assess the implications of starting a business",
                "subject_ids": [s["ids"]["sarah"]],
                "profile": "planning",
                "max_tokens": 150,
                "project": "newco-plan",
            },
        )
        assert app.get(f"/sandbox/sessions/{s['session_id']}", headers=admin).json()["tour"]["budget"] is True

        # every scripted event goes through the real pipeline and is indexed
        for script in seeding.SCRIPTS:
            ev = app.post(f"/sandbox/sessions/{s['session_id']}/events/{script}", headers=admin)
            assert ev.status_code == 202, ev.text
            deadline = time.time() + 120
            status = ""
            while time.time() < deadline:
                status = api.get(f"/v1/interactions/{ev.json()['event_id']}", headers=admin).json()["processing_status"]
                if status in ("indexed", "partial", "failed", "needs_review"):
                    break
                time.sleep(1)
            assert status == "indexed", (script, status)

        # another visitor's key is the same 404
        assert app.get(f"/sandbox/sessions/{s['session_id']}", headers={"authorization": "Bearer kgk_wrong"}).status_code == 404

        # ending the session asks for the governed workspace deletion, which suspends it at once
        assert app.delete(f"/sandbox/sessions/{s['session_id']}", headers=admin).status_code == 204
        assert api.get("/v1/me", headers=admin).status_code in (401, 403, 404, 423), "the workspace is suspended"
