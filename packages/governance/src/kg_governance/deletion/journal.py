"""The out-of-band deletion journal (phase-8 plan §5.4.3, ADR-045): an append-only JSON-lines file OUTSIDE the database and its backups.

It exists for one reason: a backup restored from before a deletion must forget again. P9's restore runbook replays this file (`kg deletions reapply --since <backup time>`),
so each line carries what a restore needs (the workspace, the kind, the ids of what was deleted, the HMACs of the suppressed identifiers) and nothing else: no name, no
content, no text. Lines are chained: each carries the HMAC of the previous line, so a removed, edited or reordered line is detectable (`verify`).
"""

from __future__ import annotations

import datetime as dt
import fcntl
import hashlib
import hmac
import json
import os
import uuid
from pathlib import Path
from typing import Any

FILE = "deletions.jsonl"
GENESIS = "0" * 64


class JournalCorrupt(RuntimeError):
    pass


def _canon(entry: dict[str, Any]) -> bytes:
    return json.dumps(entry, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str).encode()


class DeletionJournal:
    def __init__(self, directory: str | Path, secret: bytes):
        self._dir, self._secret = Path(directory), secret
        self._dir.mkdir(parents=True, exist_ok=True)
        self._path = self._dir / FILE

    @property
    def path(self) -> Path:
        return self._path

    def _mac(self, prev: str, entry: dict[str, Any]) -> str:
        return hmac.new(self._secret, prev.encode() + b"|" + _canon(entry), hashlib.sha256).hexdigest()

    def _lines(self) -> list[dict[str, Any]]:
        if not self._path.exists():
            return []
        out = []
        for raw in self._path.read_text().splitlines():
            if raw.strip():
                out.append(json.loads(raw))
        return out

    def append(
        self,
        *,
        request_id: uuid.UUID,
        workspace_id: uuid.UUID,
        kind: str,
        completed_at: dt.datetime,
        ids: dict[str, list[str]],
        suppression_hmacs: list[str],
    ) -> str:
        """Appends one line (flock-guarded, fsynced) and returns its reference `<seq>:<mac prefix>`. Idempotent per request: a repeat returns the existing reference."""
        with open(self._dir / ".lock", "a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            lines = self._lines()
            for line in lines:
                if line["request_id"] == str(request_id):
                    return f"{line['seq']}:{line['mac'][:12]}"
            prev = lines[-1]["mac"] if lines else GENESIS
            entry = {
                "seq": len(lines) + 1,
                "request_id": str(request_id),
                "workspace_id": str(workspace_id),
                "kind": kind,
                "completed_at": completed_at.astimezone(dt.UTC).isoformat(),
                "ids": {k: sorted(v) for k, v in sorted(ids.items())},
                "suppression_hmacs": sorted(suppression_hmacs),
                "prev": prev,
            }
            entry["mac"] = self._mac(prev, entry)
            fd = os.open(self._path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                os.write(fd, _canon(entry) + b"\n")
                os.fsync(fd)
            finally:
                os.close(fd)
            fcntl.flock(lock, fcntl.LOCK_UN)
            return f"{entry['seq']}:{str(entry['mac'])[:12]}"

    def verify(self) -> int:
        """Walks the chain. Returns the number of lines; raises `JournalCorrupt` at the first line that does not follow."""
        prev = GENESIS
        for n, entry in enumerate(self._lines(), start=1):
            body = {k: v for k, v in entry.items() if k != "mac"}
            if entry.get("seq") != n or entry.get("prev") != prev or entry.get("mac") != self._mac(prev, body):
                raise JournalCorrupt(f"the deletion journal breaks at line {n}")
            prev = entry["mac"]
        return n if prev != GENESIS else 0

    def since(self, when: dt.datetime) -> list[dict[str, Any]]:
        """What a restore from a backup taken at `when` must re-apply (verified first)."""
        self.verify()
        return [e for e in self._lines() if dt.datetime.fromisoformat(e["completed_at"]) >= when]
