"""P3 WP6: the operator CLI (replay, jobs, reconcile, payload re-encryption), run as real subprocesses against kg_test."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from kg_db.session import tenant_session
from kg_domain.principal import WorkspaceContext
from kg_jobs import faults
from sqlalchemy import text

from tests.conftest import APP_URL, OWNER_URL
from tests.support.worker import running_worker, wait_until

pytestmark = pytest.mark.integration
KG = str(Path(sys.executable).parent / "kg")
DEV_KEY = base64.b64encode(b"dev-only-payload-key-32-bytes-!!").decode()
K2 = base64.b64encode(b"k2-key-for-the-rotation-test-32b").decode()


async def kg(*args: str, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "ENV": "test",
        "DATABASE_URL_APP": APP_URL,
        "DATABASE_URL_OWNER": OWNER_URL,
        "LOG_LEVEL": "WARNING",
        **(extra_env or {}),
    }
    return await asyncio.to_thread(subprocess.run, [KG, *args], env=env, capture_output=True, text=True, timeout=120, check=False)  # noqa: S603


async def rows(api, t, sql, **params):  # type: ignore[no-untyped-def]
    async with tenant_session(api.engine, WorkspaceContext.for_system_task(t.ws, reason="test")) as c:
        return (await c.execute(text(sql), params)).all()


async def status(t, eid):  # type: ignore[no-untyped-def]
    d = (await t.get(f"/v1/interactions/{eid}", "admin")).json()
    return d if d["processing_status"] in ("indexed", "failed", "partial") else None


def event() -> dict[str, object]:
    return {"source_system": "chat", "source_ref": "r", "kind": "message", "content": "hello CLI", "occurred_at": "2025-01-01T00:00:00Z"}


async def test_cli_jobs_and_replay_round_trip_with_audit_attribution(tenant, api):
    eid = (await tenant.req("POST", "/v1/interactions", "app", json=event())).json()["event_id"]
    faults.configure("normalize:raise_config:1")
    try:
        async with running_worker(api):
            await wait_until(lambda: status(tenant, eid), what="failed")
    finally:
        faults.configure("")
    listed = await kg("jobs", "list", "--workspace", tenant.slug, "--status", "dead")
    assert listed.returncode == 0, listed.stderr
    job = next(json.loads(line) for line in listed.stdout.splitlines() if json.loads(line)["subject_id"] == eid)
    assert job["stage"] == "normalize" and job["last_error_code"] == "fault_injected"
    shown = await kg("jobs", "show", job["id"], "--workspace", tenant.slug)
    assert shown.returncode == 0 and json.loads(shown.stdout)["attempts_log"][0]["outcome"] == "dead"

    replay = await kg(
        "interactions", "replay", eid, "--workspace", tenant.slug, "--actor", f"operator@{tenant.slug}", "--reason", "fault_cleared"
    )
    assert replay.returncode == 0, replay.stderr
    assert json.loads(replay.stdout)["replayed"][0]["generation"] == 1
    async with running_worker(api):
        d = await wait_until(lambda: status(tenant, eid), what="indexed")
    assert d["processing_status"] == "indexed"
    (audit,) = await rows(
        api,
        tenant,
        "SELECT p.handle, a.metadata FROM audit_log a JOIN principals p ON p.id = a.actor_principal_id WHERE a.action = 'interaction.replayed'",
    )
    assert audit.handle == f"operator@{tenant.slug}" and audit.metadata["reason_code"] == "fault_cleared"

    bad = await kg("interactions", "replay", eid, "--workspace", tenant.slug, "--actor", "nobody@nowhere", "--reason", "x")
    assert bad.returncode != 0  # an unknown actor is refused: repairs are always attributed


async def test_cli_cancel_and_retry_are_audited(tenant, api):
    eid = (await tenant.req("POST", "/v1/interactions", "app", json=event())).json()["event_id"]
    (job,) = await rows(api, tenant, "SELECT id FROM outbox_jobs WHERE subject_id = :i", i=uuid.UUID(eid))
    out = await kg(
        "jobs", "cancel", str(job.id), "--workspace", tenant.slug, "--actor", f"operator@{tenant.slug}", "--reason", "not_needed"
    )
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout)["status"] == "cancelled"
    again = await kg("jobs", "retry", str(job.id), "--workspace", tenant.slug, "--actor", f"operator@{tenant.slug}", "--reason", "x")
    assert again.returncode != 0  # only a dead job can be retried
    assert (await rows(api, tenant, "SELECT count(*) AS n FROM audit_log WHERE action = 'job.cancelled'"))[0].n == 1


async def test_cli_reconcile_reports_and_repairs(tenant, api):
    from sqlalchemy.ext.asyncio import create_async_engine

    eid = (await tenant.req("POST", "/v1/interactions", "app", json=event())).json()["event_id"]
    owner = create_async_engine(OWNER_URL)
    try:
        async with owner.begin() as c:
            await c.execute(text("DELETE FROM outbox_jobs WHERE subject_id = :i"), {"i": uuid.UUID(eid)})
    finally:
        await owner.dispose()
    found = await kg("interactions", "reconcile", "--workspace", tenant.slug)
    assert found.returncode == 1 and json.loads(found.stdout)["findings"][0]["kind"] == "missing_job"
    fixed = await kg("interactions", "reconcile", "--workspace", tenant.slug, "--repair")
    assert fixed.returncode == 0 and json.loads(fixed.stdout)["repaired"] == 1
    assert (await kg("interactions", "reconcile", "--workspace", tenant.slug)).returncode == 0
    async with running_worker(api):
        d = await wait_until(lambda: status(tenant, eid), what="indexed")
    assert d["processing_status"] == "indexed"


async def test_cli_payload_reencrypt_moves_contents_to_the_target_key(tenant, api):
    await tenant.req("POST", "/v1/interactions", "app", json=event())
    ring = {"PAYLOAD_KEYS": f"dev1:{DEV_KEY},k2:{K2}", "PAYLOAD_ACTIVE_KID": "k2"}
    missing = await kg(
        "payloads", "reencrypt", "--workspace", tenant.slug, "--to", "nope", "--actor", f"admin@{tenant.slug}", extra_env=ring
    )
    assert missing.returncode != 0
    out = await kg("payloads", "reencrypt", "--workspace", tenant.slug, "--to", "k2", "--actor", f"admin@{tenant.slug}", extra_env=ring)
    assert out.returncode == 0, out.stderr
    assert "re-encrypted 1 contents under 'k2'" in out.stdout
    assert [r.kid for r in await rows(api, tenant, "SELECT DISTINCT kid FROM interaction_contents")] == ["k2"]
    assert (await rows(api, tenant, "SELECT count(*) AS n FROM audit_log WHERE action = 'payload.reencrypted'"))[0].n == 1
