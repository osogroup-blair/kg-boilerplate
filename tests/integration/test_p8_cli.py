"""P8: `kg deletions …` as real subprocesses against kg_test: what an operator can do when the API cannot serve (a suspended workspace has no credentials) and the platform-level
journal check."""

from __future__ import annotations

import json
import uuid

import pytest

from tests.integration.test_p3_cli import kg
from tests.support.p8 import deletion_world, request_deletion, run_deletion

pytestmark = pytest.mark.integration


async def test_deletions_list_show_retry_and_the_journal_check(api, tenant_governance):
    t = tenant_governance
    w = await deletion_world(api, t)
    rid = (await request_deletion(t, "interaction", w["i_sar"], "admin")).json()["request_id"]
    await run_deletion(api, t, rid)
    listed = await kg("deletions", "list", "--workspace", t.slug)
    assert listed.returncode == 0 and rid in listed.stdout and "completed" in listed.stdout, listed.stderr
    only = await kg("deletions", "list", "--workspace", t.slug, "--status", "failed")
    assert only.returncode == 0 and rid not in only.stdout
    shown = await kg("deletions", "show", "--workspace", t.slug, rid)
    assert shown.returncode == 0 and json.loads(shown.stdout)["request_id"] == rid
    missing = await kg("deletions", "show", "--workspace", t.slug, str(uuid.uuid4()))
    assert missing.returncode != 0 and "not_found" in missing.stderr
    # a completed request is not retryable, and not cancellable (only a cooling-off workspace deletion is)
    assert (await kg("deletions", "retry", "--workspace", t.slug, rid)).returncode != 0
    assert (await kg("deletions", "cancel", "--workspace", t.slug, rid)).returncode != 0
    journal = await kg("deletions", "journal")
    assert journal.returncode == 0 and "journal ok" in journal.stdout, journal.stderr
    since = await kg("deletions", "journal", "--since", "2000-01-01T00:00:00+00:00")
    assert since.returncode == 0 and rid in since.stdout
    sweep = await kg("deletions", "retention-sweep")
    assert sweep.returncode == 0 and json.loads(sweep.stdout.strip().splitlines()[-1]) is not None, sweep.stderr


async def test_a_workspace_deletion_in_its_cooling_off_is_cancelled_by_the_operator(api, tenant_governance):
    t = tenant_governance
    await deletion_world(api, t)
    r = await request_deletion(t, "workspace", None, "admin", reason="offboarding", confirm_slug=t.slug)
    assert r.status_code == 202, r.text
    rid = r.json()["request_id"]
    assert (await t.get("/v1/entities", "admin")).status_code in (401, 403)  # suspended
    done = await kg("deletions", "cancel", "--workspace", t.slug, rid)
    assert done.returncode == 0 and "cancelled" in done.stdout, done.stderr
    assert (await t.get("/v1/entities", "admin")).status_code == 200  # active again
