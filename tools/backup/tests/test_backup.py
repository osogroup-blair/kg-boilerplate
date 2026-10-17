"""The parts of the backup driver that need no Docker: the rules about recipients and escrow, the hash check, retention."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from tools.backup import stores
from tools.backup.config import Config
from tools.backup.tooling import sha256_file

pytestmark = pytest.mark.unit


def cfg(tmp_path: Path, **over) -> Config:
    base = dict(
        backup_dir=tmp_path / "data",
        escrow_dir=tmp_path / "escrow",
        recipients=["age1aaa"],
        escrow_recipients=["age1bbb"],
        retention_days=30,
        keep_at_least=2,
    )
    base.update(over)
    return Config(**base)  # type: ignore[arg-type]


def make_set(c: Config, stamp: str, created: dt.datetime, files: dict[str, bytes]) -> Path:
    d = c.backup_dir / stamp
    d.mkdir(parents=True)
    arts = []
    for name, data in files.items():
        (d / name).write_bytes(data)
        arts.append({"store": "postgres", "name": name, "size_bytes": len(data), "sha256": sha256_file(d / name)})
    (d / stores.MANIFEST).write_text(json.dumps({"id": stamp, "created_at": created.isoformat(), "artifacts": arts}))
    return d


def test_backups_are_always_encrypted_and_the_escrow_is_separate(tmp_path):
    with pytest.raises(SystemExit, match="BACKUP_RECIPIENTS"):
        cfg(tmp_path, recipients=[]).validate_for_backup()
    with pytest.raises(SystemExit, match="different recipients"):
        cfg(tmp_path, escrow_recipients=["age1aaa"]).validate_for_backup()
    with pytest.raises(SystemExit, match="different locations"):
        cfg(tmp_path, escrow_dir=tmp_path / "data").validate_for_backup()
    cfg(tmp_path).validate_for_backup()


def test_a_tampered_or_missing_artifact_is_reported(tmp_path):
    c = cfg(tmp_path)
    d = make_set(c, "20261001T000000Z", dt.datetime.now(dt.UTC), {"a.age": b"one", "b.age": b"two"})
    assert stores.verify_set(d) == []
    (d / "a.age").write_bytes(b"changed")
    (d / "b.age").unlink()
    problems = stores.verify_set(d)
    assert any("a.age" in p and "sha256" in p for p in problems) and any("b.age: missing" in p for p in problems)


def test_the_escrow_artifact_is_not_checked_against_the_data_directory(tmp_path):
    c = cfg(tmp_path)
    d = make_set(c, "20261001T000000Z", dt.datetime.now(dt.UTC), {"a.age": b"one"})
    manifest = json.loads((d / stores.MANIFEST).read_text())
    manifest["artifacts"].append({"store": "keys", "name": "keys.json.age", "location": "escrow", "sha256": "0" * 64, "size_bytes": 1})
    (d / stores.MANIFEST).write_text(json.dumps(manifest))
    assert stores.verify_set(d) == []  # the keys never live beside the data


def test_prune_removes_only_expired_sets_and_always_keeps_the_newest_few(tmp_path, monkeypatch):
    c = cfg(tmp_path, retention_days=30, keep_at_least=2)
    monkeypatch.setattr("tools.backup.record.start", lambda *a, **k: "run")
    monkeypatch.setattr("tools.backup.record.finish", lambda *a, **k: None)
    now = dt.datetime(2026, 10, 17, tzinfo=dt.UTC)
    for i, age in enumerate((90, 60, 40, 10, 1)):
        make_set(c, f"2026{i:04d}T000000Z", now - dt.timedelta(days=age), {"x.age": b"x"})
    removed = stores.prune(c, now=now)
    assert removed == ["20260000T000000Z", "20260001T000000Z", "20260002T000000Z"]
    assert [p.name for p in stores.sets(c)] == ["20260003T000000Z", "20260004T000000Z"]
    # even when everything is old, the newest `keep_at_least` stay (a retention setting must never delete the only copy)
    c2 = cfg(tmp_path / "second", retention_days=1, keep_at_least=2)
    for i, age in enumerate((9, 8, 7)):
        make_set(c2, f"2025{i:04d}T000000Z", now - dt.timedelta(days=age), {"x.age": b"x"})
    assert len(stores.prune(c2, now=now)) == 1 and len(stores.sets(c2)) == 2
