"""WP8: worker skeleton."""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys

import pytest
from kg_db.session import system_session
from sqlalchemy import text

from tests.conftest import APP_URL, NEO4J_PASSWORD, NEO4J_URI, NEO4J_USER, OWNER_URL

pytestmark = pytest.mark.integration


def worker_env(**over: str) -> dict[str, str]:
    env = {
        **os.environ,
        "ENV": "test",
        "DATABASE_URL_APP": APP_URL,
        "DATABASE_URL_OWNER": OWNER_URL,
        "NEO4J_URI": NEO4J_URI,
        "NEO4J_USER": NEO4J_USER,
        "NEO4J_PASSWORD": NEO4J_PASSWORD,
        "OSO_ROUTER_MODE": "mock",
        "OSO_ROUTER_MOCK_URL": "http://127.0.0.1:8782/v1",
        "LOG_LEVEL": "WARNING",
        "WORKER_HEARTBEAT_SECONDS": "0.5",
        "METRICS_PORT_WORKER": "19101",
    }
    env.update(over)
    return env


async def wait_for_row(engine, status: str, timeout: float = 30.0):  # noqa: ASYNC109
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        async with system_session(engine) as c:
            row = (
                await c.execute(text("SELECT * FROM worker_heartbeats WHERE status = :s ORDER BY last_beat_at DESC LIMIT 1"), {"s": status})
            ).first()
        if row:
            return row
        await asyncio.sleep(0.3)
    raise AssertionError(f"no worker heartbeat with status {status}")


async def test_heartbeat_advances_and_sigterm_marks_the_worker_stopped(api):
    proc = subprocess.Popen([sys.executable, "-m", "kg_worker"], env=worker_env(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT)  # noqa: S603, ASYNC220
    try:
        first = await wait_for_row(api.engine, "running")
        await asyncio.sleep(1.6)
        async with system_session(api.engine) as c:
            later = (
                await c.execute(text("SELECT last_beat_at FROM worker_heartbeats WHERE worker_id = :w"), {"w": first.worker_id})
            ).scalar_one()
        assert later > first.last_beat_at
        r = await api.client.get("/v1/operations/health", headers=api.h("demo/operator@demo"))
        assert r.json()["worker"]["status"] == "ok" and r.json()["worker"]["count"] >= 1
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=30) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
    async with system_session(api.engine) as c:
        status = (await c.execute(text("SELECT status FROM worker_heartbeats WHERE worker_id = :w"), {"w": first.worker_id})).scalar_one()
    assert status == "stopped"


def test_worker_refuses_to_start_with_an_rls_bypassing_role(migrated_db):
    proc = subprocess.run([sys.executable, "-m", "kg_worker"], env=worker_env(DATABASE_URL_APP=OWNER_URL), capture_output=True, timeout=60)  # noqa: S603
    assert proc.returncode == 3
    assert b"unsafe_database_role" in proc.stdout + proc.stderr
