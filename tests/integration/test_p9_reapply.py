"""P9 WP5 (V5, ADR-045): `kg deletions reapply`. A REAL backup (pg_dump of kg_test) is taken before a deletion, the deletion runs and is journalled, the backup is restored into a
separate database, and the reapply must forget again: idempotent, journal-verified, removal only, no jobs left behind."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from kg_cli.deletion_cmds import deletion_runtime
from kg_db.engine import create_app_engine
from kg_governance.deletion.journal import DeletionJournal, JournalCorrupt
from kg_governance.deletion.reapply import Reapplier
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from tests.conftest import SCRATCH_URL, make_test_settings, require
from tests.support.p8 import deletion_world, request_deletion, run_deletion
from tests.support.p9 import drop_dump, dump_database, reset_scratch, restore_into_scratch

pytestmark = pytest.mark.integration
SCRATCH_APP = SCRATCH_URL.replace("kg_owner:kg_owner_dev", "kg_app:kg_app_dev")


async def count(engine, ws: uuid.UUID, query: str, **params) -> int:
    from kg_db.session import tenant_session
    from kg_domain.principal import WorkspaceContext

    async with tenant_session(engine, WorkspaceContext.for_system_task(ws, reason="test probe")) as c:
        return int((await c.execute(text(query), {"ws": ws, **params})).scalar_one())


async def test_a_restore_from_before_a_deletion_forgets_again(api, tenant_governance):
    require(SCRATCH_URL, "postgres")
    t = tenant_governance
    w = await deletion_world(api, t)
    backup_time = dt.datetime.now(dt.UTC)
    dump = await dump_database()
    try:
        r = await request_deletion(t, "subject", w["zeb"], "app")
        assert r.status_code == 202, r.text
        done = await run_deletion(api, t, r.json()["request_id"])
        assert done["status"] in ("completed", "completed_with_exceptions"), done  # m_names is a deliberate exception for a person
        await restore_into_scratch(dump)
    finally:
        await drop_dump(dump)

    settings = make_test_settings(database_url_app=SCRATCH_APP, database_url_owner=SCRATCH_URL)
    engine = create_app_engine(settings, pool_size=3, max_overflow=2)
    try:
        # the restored database has the subject back, as a backup from before the deletion would
        assert await count(engine, t.ws, "SELECT count(*) FROM entities WHERE workspace_id = :ws AND id = :e", e=uuid.UUID(w["zeb"])) == 1
        assert (
            await count(engine, t.ws, "SELECT count(*) FROM curated_memories WHERE workspace_id = :ws AND id = :m", m=uuid.UUID(w["m_zeb"]))
            == 1
        )
        keep_before = await count(
            engine, t.ws, "SELECT count(*) FROM curated_memories WHERE workspace_id = :ws AND id = :m", m=uuid.UUID(w["m_keep"])
        )
        async with deletion_runtime(engine, settings) as (d, _):
            reapplier = Reapplier(d.service, d.journal, engine)
            preview = await reapplier.reapply(backup_time, dry_run=True, workspace_id=t.ws)
            assert [x.outcome for x in preview] == ["would_apply"] and preview[0].present["entities"] >= 1
            assert (
                await count(engine, t.ws, "SELECT count(*) FROM entities WHERE workspace_id = :ws AND id = :e", e=uuid.UUID(w["zeb"])) == 1
            )  # a dry run changes nothing

            applied = await reapplier.reapply(backup_time, workspace_id=t.ws)
            assert [x.outcome for x in applied] == ["applied"], [x.as_dict() for x in applied]
            assert applied[0].residuals == 0 and applied[0].status in ("completed", "completed_with_exceptions")

            for query, key in (
                ("SELECT count(*) FROM entities WHERE workspace_id = :ws AND id = :x", w["zeb"]),
                ("SELECT count(*) FROM facts WHERE workspace_id = :ws AND id = :x", w["knows"]),
                ("SELECT count(*) FROM curated_memories WHERE workspace_id = :ws AND id = :x", w["m_zeb"]),
                ("SELECT count(*) FROM curated_memories WHERE workspace_id = :ws AND id = :x", w["m_from_zeb"]),
                ("SELECT count(*) FROM interactions WHERE workspace_id = :ws AND id = :x", w["i_zeb"]),
            ):
                assert await count(engine, t.ws, query, x=uuid.UUID(key)) == 0, query
            # what did not depend on the subject is untouched, and nothing was created
            assert (
                await count(
                    engine, t.ws, "SELECT count(*) FROM curated_memories WHERE workspace_id = :ws AND id = :m", m=uuid.UUID(w["m_keep"])
                )
                == keep_before
            )
            assert (
                await count(engine, t.ws, "SELECT count(*) FROM entities WHERE workspace_id = :ws AND id = :x", x=uuid.UUID(w["sarah"]))
                == 1
            )
            # the line's tombstones and the suppression of his strong identifier are back in place
            assert (
                await count(
                    engine,
                    t.ws,
                    "SELECT count(*) FROM deletion_tombstones WHERE workspace_id = :ws AND kind = 'entity' AND id = :x",
                    x=uuid.UUID(w["zeb"]),
                )
                == 1
            )
            assert await count(engine, t.ws, "SELECT count(*) FROM deletion_suppressions WHERE workspace_id = :ws") >= 1
            # no job was left for a worker to find
            assert (
                await count(
                    engine,
                    t.ws,
                    "SELECT count(*) FROM outbox_jobs WHERE workspace_id = :ws AND stage LIKE 'deletion_%' AND status IN ('pending','leased')",
                )
                == 0
            )

            # idempotent: a second run finds nothing to do
            again = await reapplier.reapply(backup_time, workspace_id=t.ws)
            assert [x.outcome for x in again] == ["already_applied"]
            # the journal still has exactly one line for the request (the reapply used the same request id)
            assert sum(1 for line in d.journal._lines() if line["request_id"] == r.json()["request_id"]) == 1
    finally:
        await engine.dispose()
        await reset_scratch()  # the migration round-trip tests expect an empty scratch database


async def test_a_broken_journal_stops_the_reapply_before_it_touches_anything(api, tmp_path):
    secret = api.settings.content_hmac_secret.get_secret_value().encode()
    journal = DeletionJournal(tmp_path, secret)
    ws = uuid.uuid4()
    for _ in range(3):
        journal.append(
            request_id=uuid.uuid4(),
            workspace_id=ws,
            kind="subject",
            completed_at=dt.datetime.now(dt.UTC),
            ids={"entities": [str(uuid.uuid4())]},
            suppression_hmacs=[],
        )
    assert journal.verify() == 3
    lines = (tmp_path / "deletions.jsonl").read_text().splitlines()
    (tmp_path / "deletions.jsonl").write_text("\n".join([lines[0], lines[2], lines[1]]) + "\n")  # reordered
    engine = create_async_engine(SCRATCH_APP)
    try:
        svc = api.app.state.kg.deletion.service
        with pytest.raises(JournalCorrupt):
            await Reapplier(svc, journal, engine).reapply(dt.datetime.now(dt.UTC) - dt.timedelta(days=1))
    finally:
        await engine.dispose()
