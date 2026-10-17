"""Records what the tool did in `backup_runs` (status, size, hash, retention: never content). Needs the owner connection; a database that cannot be reached is logged, not fatal:
the backup itself must not fail because its bookkeeping could not be written (a missing record is itself visible as an old backup age)."""

from __future__ import annotations

import json
import uuid
from typing import Any

from tools.backup.config import Config
from tools.backup.tooling import log


def _connect(cfg: Config):  # type: ignore[no-untyped-def]
    import psycopg

    return psycopg.connect(cfg.owner_url, autocommit=True, connect_timeout=5)


def start(cfg: Config, kind: str, store: str) -> uuid.UUID:
    run_id = uuid.uuid4()
    try:
        with _connect(cfg) as conn:
            conn.execute("INSERT INTO backup_runs (id, kind, store, status) VALUES (%s, %s, %s, 'running')", (run_id, kind, store))
    except Exception as exc:
        log(f"could not record the start of {kind}/{store}: {type(exc).__name__}")
    return run_id


def finish(
    cfg: Config,
    run_id: uuid.UUID,
    status: str,
    *,
    size: int | None = None,
    sha: str | None = None,
    location: str | None = None,
    retention_days: int | None = None,
    error_code: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    try:
        with _connect(cfg) as conn:
            conn.execute(
                "UPDATE backup_runs SET status = %s, completed_at = now(), size_bytes = %s, sha256 = %s, location = %s, "
                "expires_at = CASE WHEN %s::int IS NULL THEN NULL ELSE now() + make_interval(days => %s::int) END, error_code = %s, details = %s::jsonb WHERE id = %s",
                (status, size, sha, location, retention_days, retention_days, error_code, json.dumps(details or {}), run_id),
            )
    except Exception as exc:
        log(f"could not record the end of run {run_id}: {type(exc).__name__}")


def rehearsal(cfg: Config, status: str, details: dict[str, Any]) -> None:
    """A restore rehearsal is a `backup_runs` row of its own (kind restore_rehearsal): when it ran, how long it took (RTO), how old the backup was (RPO), and whether it passed."""
    try:
        with _connect(cfg) as conn:
            conn.execute(
                "INSERT INTO backup_runs (id, kind, store, status, completed_at, details) VALUES (%s, 'restore_rehearsal', 'all', %s, now(), %s::jsonb)",
                (uuid.uuid4(), status, json.dumps(details)),
            )
    except Exception as exc:
        log(f"could not record the rehearsal: {type(exc).__name__}")
