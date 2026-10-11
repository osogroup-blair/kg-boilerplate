"""T-MG-04: model_calls, audit_log and logs hold no content, even when messages are full of sentinel strings."""

from __future__ import annotations

import json
import uuid

import pytest
from kg_cli.seed import workspace_id_for
from kg_db.session import tenant_session
from kg_domain.principal import WorkspaceContext
from kg_modelgw.calllog import DbCallSink
from kg_modelgw.config import load_config
from kg_modelgw.factory import build_router_stack
from kg_modelgw.gateway import Correlation, ModelGateway
from kg_obs.logging import configure_logging
from sqlalchemy import text

from tests.support.router_env import KEY, Env

pytestmark = [pytest.mark.integration, pytest.mark.security]

SENTINELS = ["SENTINEL-MSG-ALPHA-7731", "SENTINEL-EMBED-BETA-9042", "SENTINEL-SYS-GAMMA-1188"]


async def test_mg04_no_content_reaches_the_database_or_the_logs(api, capsys):
    configure_logging("DEBUG", service="kg-test")
    env = Env()
    sink = DbCallSink(api.engine)
    stack = build_router_stack(
        env.settings,
        service="kg-worker",
        sink=sink,
        inner=env.spy,
        config=load_config(env.settings.model_purposes_path, env.settings.pricing_path),
    )
    gw = ModelGateway(stack.http, stack.config, sink)
    ws = workspace_id_for("demo")
    corr = Correlation(str(uuid.uuid4()), str(ws), str(uuid.uuid4()), "kg-worker")
    await gw.complete(
        "context_compression",
        [{"role": "system", "content": SENTINELS[2]}, {"role": "user", "content": SENTINELS[0]}],
        classification="internal",
        correlation=corr,
    )
    await gw.embed("embedding", [SENTINELS[1]], kind="passage", classification="internal", correlation=corr)
    with pytest.raises(Exception):
        await gw.complete("context_compression", [{"role": "user", "content": SENTINELS[0]}], classification="local_only", correlation=corr)
    await sink.flush()
    ctx = WorkspaceContext.for_system_task(ws, reason="test read")
    async with tenant_session(api.engine, ctx) as c:
        rows = (await c.execute(text("SELECT * FROM model_calls WHERE correlation_id = :c"), {"c": corr.correlation_id})).all()
        audit = (await c.execute(text("SELECT * FROM audit_log"))).all()
    assert len(rows) == 3 and {r.outcome for r in rows} == {"ok", "egress_denied"}
    dump = json.dumps([[str(v) for v in r] for r in rows + audit])
    out = capsys.readouterr()
    for s in SENTINELS:
        assert s not in dump and s not in out.out and s not in out.err, s
    assert all(r.tokenizer and r.service == "kg-worker" and r.workspace_id == ws for r in rows)
    assert {col for col in rows[0]._fields} == {
        "id",
        "workspace_id",
        "purpose",
        "alias",
        "operation",
        "classification",
        "correlation_id",
        "router_request_id",
        "prompt_tokens",
        "completion_tokens",
        "usage_estimated",
        "est_cost_usd",
        "latency_ms",
        "attempts",
        "outcome",
        "fallback_used",
        "tokenizer",
        "service",
        "created_at",
    }
    assert KEY and stack.http is not None
