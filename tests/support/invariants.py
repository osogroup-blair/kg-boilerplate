"""The invariant checker for the failure-injection suite (phase-3 plan WP7): SQL over a workspace, run as the owner role so nothing is hidden by RLS."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from tests.conftest import OWNER_URL

TERMINAL = ("indexed", "partial", "failed")


@dataclass
class Report:
    interactions: int = 0
    non_terminal: list[str] = field(default_factory=list)
    open_jobs: int = 0
    without_stages: int = 0
    running_without_lease: int = 0
    duplicate_success: list[str] = field(default_factory=list)
    bad_normalized_rows: list[str] = field(default_factory=list)
    plaintext_tables: list[str] = field(default_factory=list)
    reclaims: int = 0
    attempts: dict[uuid.UUID, int] = field(default_factory=dict)
    statuses: dict[str, int] = field(default_factory=dict)

    def violations(self) -> list[str]:
        out: list[str] = []
        if self.non_terminal:
            out.append(f"{len(self.non_terminal)} interactions not terminal: {self.non_terminal[:3]}")
        if self.open_jobs:
            out.append(f"{self.open_jobs} jobs still pending or leased")
        if self.without_stages:
            out.append(f"{self.without_stages} interactions without stages")
        if self.running_without_lease:
            out.append(f"{self.running_without_lease} stages running without a live lease")
        if self.duplicate_success:
            out.append(f"duplicate succeeded jobs: {self.duplicate_success[:3]}")
        if self.bad_normalized_rows:
            out.append(f"interactions with other than one normalized row: {self.bad_normalized_rows[:3]}")
        if self.plaintext_tables:
            out.append(f"plaintext sentinel found in {self.plaintext_tables}")
        return out


async def check(ws: uuid.UUID, *, sentinel: str | None = None, owner_url: str | None = None) -> Report:
    engine = create_async_engine(owner_url or OWNER_URL)
    r = Report()
    try:
        async with engine.connect() as c:
            p = {"ws": ws}
            rows = (await c.execute(text("SELECT id, processing_status FROM interactions WHERE workspace_id = :ws"), p)).all()
            r.interactions = len(rows)
            for row in rows:
                r.statuses[row.processing_status] = r.statuses.get(row.processing_status, 0) + 1
            r.non_terminal = [str(x.id) for x in rows if x.processing_status not in TERMINAL]
            r.open_jobs = (
                await c.execute(text("SELECT count(*) FROM outbox_jobs WHERE workspace_id = :ws AND status IN ('pending','leased')"), p)
            ).scalar_one()
            r.without_stages = (
                await c.execute(
                    text(
                        "SELECT count(*) FROM interactions i WHERE i.workspace_id = :ws AND NOT EXISTS "
                        "(SELECT 1 FROM interaction_stages s WHERE s.workspace_id = i.workspace_id AND s.interaction_id = i.id)"
                    ),
                    p,
                )
            ).scalar_one()
            r.running_without_lease = (
                await c.execute(
                    text(
                        "SELECT count(*) FROM interaction_stages s WHERE s.workspace_id = :ws AND s.status = 'running' AND NOT EXISTS "
                        "(SELECT 1 FROM outbox_jobs j WHERE j.workspace_id = s.workspace_id AND j.subject_id = s.interaction_id AND j.stage = s.stage "
                        "AND j.status = 'leased' AND j.lease_expires_at > now())"
                    ),
                    p,
                )
            ).scalar_one()
            r.duplicate_success = [
                f"{d.subject_id}/{d.stage}/g{d.g}"
                for d in (
                    await c.execute(
                        text(
                            "SELECT subject_id, stage, payload->>'generation' AS g FROM outbox_jobs WHERE workspace_id = :ws AND status = 'succeeded' "
                            "GROUP BY 1, 2, 3 HAVING count(*) > 1"
                        ),
                        p,
                    )
                ).all()
            ]
            r.bad_normalized_rows = [
                str(x.id)
                for x in (
                    await c.execute(
                        text(
                            "SELECT i.id, count(c.form) AS n FROM interactions i LEFT JOIN interaction_contents c ON c.interaction_id = i.id AND c.form = 'normalized' "
                            "WHERE i.workspace_id = :ws AND i.processing_status = 'indexed' GROUP BY i.id HAVING count(c.form) <> 1"
                        ),
                        p,
                    )
                ).all()
            ]
            r.reclaims = (
                await c.execute(text("SELECT count(*) FROM job_attempts WHERE workspace_id = :ws AND outcome = 'lease_expired'"), p)
            ).scalar_one()
            for a in (
                await c.execute(
                    text("SELECT subject_id, max(attempts) AS n FROM outbox_jobs WHERE workspace_id = :ws GROUP BY subject_id"), p
                )
            ).all():
                r.attempts[a.subject_id] = a.n
            if sentinel:
                tables = [t[0] for t in (await c.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'"))).all()]
                for table in tables:
                    dump = (await c.execute(text(f"SELECT coalesce(string_agg(t::text, E'\\n'), '') FROM \"{table}\" t"))).scalar_one()  # noqa: S608
                    if sentinel in dump:
                        r.plaintext_tables.append(table)
    finally:
        await engine.dispose()
    return r
