"""Builds a workspace for a visitor: the Sarah world through the 2027 job change, with a research agent, a planning agent and an agent with no grants.

It reuses the Sarah example's own steps (so what a visitor gets is what `make demo-sarah` proves) with one difference: the command step does not count model calls
on the mock router, because other visitors' pipelines are running at the same time and the count is global."""

from __future__ import annotations

import re
import secrets
import subprocess
import uuid
from dataclasses import dataclass, field
from typing import Any

from examples._kit import admin, mock
from examples._kit.env import Env
from examples._kit.report import Report
from examples._kit.workspace import Workspace
from examples.sarah.steps import Scenario
from kg_sandbox.config import REPO, Config


@dataclass
class Seeded:
    slug: str
    admin_key: str
    conv_key: str
    keys: dict[str, str]  # what the visitor is handed
    ids: dict[str, str]
    baseline: dict[str, int] = field(default_factory=dict)  # counts after seeding, to tell what the visitor did


def env_for(cfg: Config) -> Env:
    return Env(base_url=cfg.api_url, mcp_url="", mock_url=cfg.mock_url, live=False)


def load_fixture_pack(cfg: Config) -> None:
    """Strict mode on and the Sarah scenario's answers loaded, and left that way: the sandbox's mock router answers only what was prepared."""
    env = env_for(cfg)
    mock._call(env, "POST", "/__mock/strict", json={"enabled": True})
    mock._call(env, "POST", "/__mock/reload")
    mock._call(env, "POST", "/__mock/fixtures", json={"rules": Scenario.mock_rules(None)})  # type: ignore[arg-type]


def create_workspace(cfg: Config, slug: str) -> str:
    cmd = [*cfg.cli, "workspaces", "create", "--slug", slug, "--name", "Sandbox", "--ontology", str(cfg.ontology)]
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=REPO, check=False, timeout=180)  # noqa: S603
    if proc.returncode != 0:
        raise RuntimeError(f"kg workspaces create failed: {proc.stderr[-300:]}")
    m = re.search(r"admin@[^:\s]*:\s*(\S+)", proc.stdout) or re.search(r"^\S+: (kg\S+)$", proc.stdout, re.M)
    if not m:
        raise RuntimeError("no administrator key in the CLI output")
    return m.group(1)


def seed_sarah(cfg: Config) -> Seeded:
    slug = f"sbx-{secrets.token_hex(4)}"
    admin_key = create_workspace(cfg, slug)
    env = env_for(cfg)
    ws = Workspace(slug=slug, admin_key=admin_key, base_url=cfg.api_url, keys={})
    sc = Scenario(env, ws, Report("sandbox seed", "mock"), out=lambda _m: None)
    sc.s1_ontology()
    commands(sc)
    sc.s3_conversation()  # registers the agents, ingests evt_003, waits for it to be indexed
    sc.s4_facts_and_memory()
    sc.s5_finding()
    sc.s7_job_change()
    keys = {
        "admin": admin_key,
        "planning_agent": ws.keys["planning-agent"],
        "research_agent": ws.keys["research-agent"],
        "ungranted_agent": ws.keys["ungranted-agent"],
    }
    ids = {k: sc.ids[k] for k in ("sarah", "acme", "michael", "topic")}
    return Seeded(slug=slug, admin_key=admin_key, conv_key=ws.keys["conversation-app"], keys=keys, ids=ids, baseline=counts(ws))


def commands(sc: Scenario) -> None:
    """Scenario.s2_commands without its model-call assertion (see the module docstring)."""
    key = sc.ws.keys["conversation-app"]

    def hr(v: str) -> list[dict[str, str]]:
        return [{"system": "hr", "value": v}]

    def ent(t: str, n: str, **b: Any) -> dict[str, Any]:
        return admin.post(sc.ws, "/v1/entities", {"type": t, "name": n, "source_system": "hr", **b}, key=key)

    sarah = ent("Person", "Sarah Johnson", external_ids=hr("E-1001"), source_ref="E-1001")
    acme = ent("Organization", "Acme", external_ids=hr("ORG-1"), source_ref="ORG-1")
    michael = ent("Person", "Michael", source_ref="E-2")
    topic = ent("Topic", "Entrepreneurship", source_ref="T-1")
    sc.ids.update(sarah=sarah["entity_id"], acme=acme["entity_id"], michael=michael["entity_id"], topic=topic["entity_id"])

    def rel(**b: Any) -> dict[str, Any]:
        body = {
            "type": "WORKS_AT",
            "source": {"entity_id": sc.ids["sarah"]},
            "target": {"entity_id": sc.ids["acme"]},
            "source_system": "hr",
            **b,
        }
        return admin.post(sc.ws, "/v1/relationships", body, key=key)

    f1 = rel(valid_from="2022-01-10", attributes={"role": "Software Engineer"}, source_ref="evt_001")
    f2 = rel(valid_from="2024-06-01", attributes={"role": "Engineering Manager"}, source_ref="evt_002")
    sc.ids.update(swe=f1["fact_id"], em=f2["fact_id"])


def counts(ws: Workspace) -> dict[str, int]:
    """How much exists right after seeding: the tour compares against it."""

    def n(path: str) -> int:
        r = admin.get(ws, path, limit=100)
        r.raise_for_status()
        body = r.json()
        return len(body["items"] if isinstance(body, dict) and "items" in body else body)

    return {
        "entities": n("/v1/entities"),
        "memories": n("/v1/memories"),
        "contexts": n("/v1/context"),
        "corrections": n("/v1/corrections"),
        "deletions": n("/v1/deletion-requests"),
    }


SCRIPTS: dict[str, dict[str, Any]] = {
    "manager": {
        "title": "Michael says he is Sarah's manager",
        "why": "A wrong extraction to reject: it arrives as an unverified MANAGES fact.",
        "content": "Michael is Sarah's manager.",
        "subjects": ["sarah", "michael"],
    },
    "newco": {
        "title": "Sarah says she works at NewCo now",
        "why": "A job change by conversation: it arrives unverified and does not overwrite the HR fact.",
        "content": "Sarah Johnson: I work at NewCo now.",
        "subjects": ["sarah"],
    },
}


def send_script(cfg: Config, row: Any, name: str) -> dict[str, Any]:
    spec = SCRIPTS[name]
    import json

    ids = json.loads(row["ids_json"])
    ws = Workspace(slug=row["slug"], admin_key=row["admin_key"], base_url=cfg.api_url, keys={})
    return admin.post(
        ws,
        "/v1/interactions",
        {
            "source_system": "sandbox",
            "source_ref": f"{name}-{uuid.uuid4().hex[:8]}",
            "kind": "message",
            "content": spec["content"],
            "occurred_at": "2026-10-15T09:00:00Z",
            "subject_refs": [{"entity_id": ids[k]} for k in spec["subjects"]],
        },
        key=row["conv_key"],
    )


def delete_workspace(cfg: Config, row: Any) -> int:
    ws = Workspace(slug=row["slug"], admin_key=row["admin_key"], base_url=cfg.api_url, keys={})
    with ws.client() as c:
        r = c.post(
            "/v1/deletion-requests",
            headers={"idempotency-key": str(uuid.uuid4())},
            json={"kind": "workspace", "confirm_slug": row["slug"], "reason_code": "sandbox_expired"},
        )
    return r.status_code
