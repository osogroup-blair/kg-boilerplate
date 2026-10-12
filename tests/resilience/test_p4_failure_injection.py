"""P4 gate: FI-09 (a worker dies after Graphiti saved the episode) and FI-10 (graph writers of one workspace contend across worker processes)."""

from __future__ import annotations

import uuid
from pathlib import Path

import httpx
import pytest

from tests.resilience.conftest import Supervisor, WorkerProcess
from tests.resilience.test_failure_injection import LEASE, assert_clean, counters, wait_settled
from tests.support.p4 import MOCK, NEWCO, ctx_of, ingest, sarah_story, sql, stage, wait_terminal
from tests.support.projection import project_due

pytestmark = [pytest.mark.integration, pytest.mark.resilience]


async def project_story(api, t) -> None:  # type: ignore[no-untyped-def]
    """The API's fast path is off in this suite; project the story here so the extraction finds its graph."""
    while await project_due(api, t.ws):
        pass


async def mock_requests() -> list[dict[str, object]]:
    async with httpx.AsyncClient() as c:
        return list((await c.get(f"{MOCK}/__mock/requests")).json()["requests"])


async def test_fi09_a_crash_after_graphiti_saved_pays_for_the_episode_once(resilient_api, tenant, strict_mock, tmp_path: Path):
    t = tenant
    ids = await sarah_story(t, projected=False)
    await project_story(resilient_api, t)
    async with httpx.AsyncClient() as c:
        await c.delete(f"{MOCK}/__mock/requests")
    event = await ingest(t, NEWCO, subjects=[ids["sarah"]])
    faults_dir = tmp_path / "faults"
    worker = WorkerProcess(
        tmp_path, "w", KG_FAULTS="graph_ingest:after_side_effect:1", KG_FAULTS_DIR=str(faults_dir), WORKER_LEASE_SECONDS=str(LEASE)
    )
    async with Supervisor([worker]):
        detail = await wait_terminal(t, event, timeout=240.0)
        await wait_settled(t, 1)
    assert counters(faults_dir) == {"graph_ingest:after_side_effect": 1}, "the planned crash happened, exactly once"
    assert worker.starts >= 2
    st = stage(detail, "graph_ingest")
    assert st["status"] == "succeeded" and st["attempts"] == 2 and st["result"]["reused"] is True
    asked = [r.get("schema_name") for r in await mock_requests() if r.get("endpoint") == "chat"]
    assert asked.count("ExtractedEntities") == 1 and asked.count("ExtractedEdges") == 1  # the stage ran twice, the model was asked once
    facts = await sql(resilient_api, t, "SELECT status FROM facts WHERE workspace_id = :ws AND source_type = 'extracted'")
    assert [f.status for f in facts] == ["proposed"]  # the invalidation made before the crash was still caught and undone
    assert (await resilient_api.app.state.kg.graph.scoped(ctx_of(t)).read_edge(ids["em"])).expired_at is None  # type: ignore[union-attr]
    report = await resilient_api.app.state.kg.knowledge.projector.reconcile(ctx_of(t))
    assert report.drift == []
    await assert_clean(t, 1, sentinel=False)


async def test_fi10_graph_writers_of_one_workspace_contend_across_worker_processes_without_drift(resilient_api, tenant, tmp_path: Path):
    t = tenant
    await sarah_story(t, projected=False)
    await project_story(resilient_api, t)
    events = []
    for i in range(8):
        events.append(await ingest(t, {"kind": "message", "content": f"Sarah Johnson: note {i}, nothing new today"}, subjects=[]))
    # commands race the extractions for the same workspace's graph; the API fast path is off, so the worker processes project them
    cmds = []
    for i in range(6):
        r = await t.entity(
            "Person",
            f"Contender {i}",
            external_ids=[{"system": "hr", "value": f"C-{i}-{uuid.uuid4().hex[:6]}"}],
            source_system="hr",
            source_ref=str(i),
        )
        assert r.status_code == 202, r.text
        cmds.append(r.json()["command_id"])
    workers = [WorkerProcess(tmp_path, f"w{i}", WORKER_LEASE_SECONDS=str(LEASE)) for i in range(2)]
    async with Supervisor(workers):
        rep = await wait_settled(t, 8, cmds)
    assert rep.violations() == [] and rep.statuses == {"indexed": 8}
    report = await resilient_api.app.state.kg.knowledge.projector.reconcile(ctx_of(t))
    assert report.drift == [] and report.pending == 0
    nodes, _ = await resilient_api.app.state.kg.graph.scoped(ctx_of(t)).list_uuids()
    assert len(nodes) == 4 + 6  # the story and the contenders; no extraction added or duplicated a node
    assert len(events) == 8
