"""Sessions in one SQLite file. States: pool (seeded, unclaimed) -> active -> deleting -> gone. Visitor keys live here only until they are handed over."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY, slug TEXT NOT NULL, scenario TEXT NOT NULL, state TEXT NOT NULL,
  created_at REAL NOT NULL, expires_at REAL, ip_hash TEXT, admin_key_hash TEXT NOT NULL,
  admin_key TEXT NOT NULL, conv_key TEXT NOT NULL, keys_json TEXT NOT NULL, ids_json TEXT NOT NULL, baseline_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS sessions_state ON sessions(state);
CREATE INDEX IF NOT EXISTS sessions_ip ON sessions(ip_hash, created_at);
"""


def sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


class Store:
    def __init__(self, path: Path, clock: Callable[[], float] = time.time) -> None:
        self.clock = clock
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._db.executescript(SCHEMA)
        try:
            path.chmod(0o600)
        except OSError:
            pass

    @contextmanager
    def tx(self) -> Any:
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield self._db
                self._db.execute("COMMIT")
            except Exception:
                self._db.execute("ROLLBACK")
                raise

    def add_pool(
        self,
        *,
        sid: str,
        slug: str,
        scenario: str,
        admin_key: str,
        conv_key: str,
        keys: dict[str, str],
        ids: dict[str, str],
        baseline: dict[str, Any],
    ) -> None:
        with self.tx() as db:
            db.execute(
                "INSERT INTO sessions (id, slug, scenario, state, created_at, admin_key_hash, admin_key, conv_key, keys_json, ids_json, baseline_json) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    sid,
                    slug,
                    scenario,
                    "pool",
                    self.clock(),
                    sha(admin_key),
                    admin_key,
                    conv_key,
                    json.dumps(keys),
                    json.dumps(ids),
                    json.dumps(baseline),
                ),
            )

    def claim(self, scenario: str, *, ip_hash: str, ttl_seconds: float) -> sqlite3.Row | None:
        """Takes one seeded workspace from the pool and starts its clock; returns the row (with keys) or None."""
        with self.tx() as db:
            row: sqlite3.Row | None = db.execute(
                "SELECT * FROM sessions WHERE state='pool' AND scenario=? ORDER BY created_at LIMIT 1", (scenario,)
            ).fetchone()
            if row is None:
                return None
            db.execute(
                "UPDATE sessions SET state='active', expires_at=?, ip_hash=? WHERE id=?", (self.clock() + ttl_seconds, ip_hash, row["id"])
            )
            db.execute("UPDATE sessions SET keys_json='{}' WHERE id=?", (row["id"],))  # handed over once, then forgotten
            return row

    def get(self, sid: str) -> sqlite3.Row | None:
        with self._lock:
            row: sqlite3.Row | None = self._db.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
            return row

    def count(self, state: str, scenario: str | None = None) -> int:
        with self._lock:
            q, a = "SELECT COUNT(*) FROM sessions WHERE state=?", [state]
            if scenario:
                q, a = q + " AND scenario=?", [*a, scenario]
            return int(self._db.execute(q, a).fetchone()[0])

    def ip_recent(self, ip_hash: str, window_seconds: float = 3600) -> int:
        with self._lock:
            return int(
                self._db.execute(
                    "SELECT COUNT(*) FROM sessions WHERE ip_hash=? AND created_at>?", (ip_hash, self.clock() - window_seconds)
                ).fetchone()[0]
            )

    def due(self, now: float | None = None) -> list[sqlite3.Row]:
        with self._lock:
            return list(
                self._db.execute("SELECT * FROM sessions WHERE state='active' AND expires_at<=?", (now or self.clock(),)).fetchall()
            )

    def stale_pool(self, max_age_seconds: float) -> list[sqlite3.Row]:
        with self._lock:
            return list(
                self._db.execute("SELECT * FROM sessions WHERE state='pool' AND created_at<?", (self.clock() - max_age_seconds,)).fetchall()
            )

    def set_state(self, sid: str, state: str) -> None:
        with self.tx() as db:
            db.execute("UPDATE sessions SET state=? WHERE id=?", (state, sid))
            if state == "gone":
                db.execute("UPDATE sessions SET admin_key='', conv_key='' WHERE id=?", (sid,))

    def extend_created(self, sid: str, ts: float) -> None:
        with self.tx() as db:
            db.execute("UPDATE sessions SET created_at=? WHERE id=?", (ts, sid))

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {r["state"]: r["n"] for r in self._db.execute("SELECT state, COUNT(*) AS n FROM sessions GROUP BY state")}
