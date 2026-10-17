"""P5 gate: FI-11 (a worker dies after the model answered the memory question, before anything was committed) and FI-12 (it dies after the commit, before the ack)."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from tests.resilience.conftest import Supervisor, WorkerProcess
from tests.resilience.test_failure_injection import LEASE, assert_clean, counters, wait_settled
from tests.support.p4 import EVT003, MOCK, ingest, sarah_story, stage, wait_terminal
from tests.support.p5 import BUSINESS, memory_rows
from tests.support.projection import project_due

pytestmark = [pytest.mark.integration, pytest.mark.resilience]


async def project_story(api, t) -> None:  # type: ignore[no-untyped-def]
    while await project_due(api, t.ws):
        pass


async def asked_memory_question() -> int:
    async with httpx.AsyncClient() as c:
        requests = (await c.get(f"{MOCK}/__mock/requests")).json()["requests"]
    return sum(1 for r in requests if r.get("endpoint") == "chat" and r.get("schema_name") == "MemoryCandidates")


async def run_with_fault(resilient_api, t, tmp_path: Path, fault: str):  # type: ignore[no-untyped-def]
    ids = await sarah_story(t, projected=False)
    await project_story(resilient_api, t)
    async with httpx.AsyncClient() as c:
        await c.delete(f"{MOCK}/__mock/requests")
    event = await ingest(t, EVT003, subjects=[ids["sarah"], ids["michael"], ids["topic"]])
    faults_dir = tmp_path / "faults"
    worker = WorkerProcess(tmp_path, "w", KG_FAULTS=fault, KG_FAULTS_DIR=str(faults_dir), WORKER_LEASE_SECONDS=str(LEASE))
    async with Supervisor([worker]):
        detail = await wait_terminal(t, event, timeout=240.0)
        await wait_settled(t, 1)
    return detail, faults_dir, worker


async def test_fi11_a_crash_after_the_model_answered_re_asks_and_the_content_hash_keeps_one_memory(
    resilient_api, tenant_mem, strict_mock, tmp_path: Path
):
    t = tenant_mem
    detail, faults_dir, worker = await run_with_fault(resilient_api, t, tmp_path, "memory_propose:after_side_effect:1")
    assert counters(faults_dir) == {"memory_propose:after_side_effect": 1}, "the planned crash happened, exactly once"
    assert worker.starts >= 2
    st = stage(detail, "memory_propose")
    assert st["status"] == "succeeded" and st["attempts"] == 2 and st["result"]["kept"] == 1 and st["result"]["statuses"] == {"active": 1}
    assert await asked_memory_question() == 2  # nothing was committed before the crash, so the second attempt asked again...
    rows = await memory_rows(resilient_api, t)
    assert [r.content for r in rows] == [BUSINESS]  # ...and still there is ONE memory (the keyed content hash; no duplicate, no orphan)
    rep = await assert_clean(t, 1, sentinel=False)
    assert rep.duplicate_memories == [] and rep.memories_without_scopes == 0


async def test_fi12_a_crash_after_the_commit_never_asks_the_model_again(resilient_api, tenant_mem, strict_mock, tmp_path: Path):
    t = tenant_mem
    detail, faults_dir, worker = await run_with_fault(resilient_api, t, tmp_path, "memory_propose:before_complete:1")
    assert counters(faults_dir) == {"memory_propose:before_complete": 1}
    assert worker.starts >= 2
    st = stage(detail, "memory_propose")
    assert st["status"] == "succeeded" and st["attempts"] == 1 and st["result"]["kept"] == 1
    assert await asked_memory_question() == 1  # the stage was terminal at this generation: the re-run only acked
    assert [r.content for r in await memory_rows(resilient_api, t)] == [BUSINESS]
    rep = await assert_clean(t, 1, sentinel=False)
    assert rep.duplicate_memories == []
