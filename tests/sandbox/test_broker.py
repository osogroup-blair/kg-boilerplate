"""The sandbox broker's rules, without a stack: caps, expiry, ownership, the same 404 for everything that is not yours, and no keys kept after handover."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from kg_sandbox import seed as seeding
from kg_sandbox.app import Broker, create_app
from kg_sandbox.config import Config
from kg_sandbox.store import Store


class Clock:
    def __init__(self) -> None:
        self.t = 1_800_000_000.0

    def __call__(self) -> float:
        return self.t


def fake_seed(counter: list[int]) -> Any:
    def seeder(_cfg: Config) -> seeding.Seeded:
        counter.append(1)
        n = len(counter)
        return seeding.Seeded(
            slug=f"sbx-test{n}",
            admin_key=f"kgk_admin{n:04d}xxxxxxxx",
            conv_key=f"kgk_conv{n:04d}xxxxxxxx",
            keys={"admin": f"kgk_admin{n:04d}xxxxxxxx", "planning_agent": f"kgk_plan{n:04d}xxxxxxxx"},
            ids={"sarah": "s", "michael": "m", "acme": "a", "topic": "t"},
            baseline={"entities": 5, "memories": 2, "contexts": 0, "corrections": 0, "deletions": 0},
        )

    return seeder


def build(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **over: Any) -> tuple[TestClient, Broker, Clock, list[int]]:
    cfg = Config(
        db_path=tmp_path / "s.sqlite",
        **{"pool_size": 1, "max_active": 10, "ip_per_hour": 2, "ttl_hours": 2.0, "manage_mock": False, **over},
    )
    clock, seeded = Clock(), []
    broker = Broker(cfg, Store(cfg.db_path, clock), seeder=fake_seed(seeded), now=clock)
    deleted: list[str] = []
    monkeypatch.setattr(seeding, "delete_workspace", lambda _cfg, row: deleted.append(row["slug"]) or 202)
    broker.deleted = deleted  # type: ignore[attr-defined]
    return TestClient(create_app(cfg, broker=broker, background=False)), broker, clock, seeded


@pytest.fixture
def setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, Broker, Clock, list[int]]:
    return build(tmp_path, monkeypatch)


def start(c: TestClient, ip: str = "1.1.1.1") -> dict[str, Any]:
    r = c.post("/sandbox/sessions", json={"scenario": "sarah"}, headers={"x-forwarded-for": ip})
    assert r.status_code == 201, r.text
    return dict(r.json())


def test_a_session_hands_over_keys_once_and_forgets_them(setup: Any) -> None:
    c, b, _clock, _ = setup
    s = start(c)
    assert s["keys"]["admin"].startswith("kgk_") and s["api_url"] and s["ids"]["sarah"]
    row = b.store.get(s["session_id"])
    assert json.loads(row["keys_json"]) == {}, "the visitor keys must not be kept after handover"


def test_everything_that_is_not_yours_is_the_same_404(setup: Any) -> None:
    c, _b, _clock, _ = setup
    s = start(c)
    mine = {"authorization": f"Bearer {s['keys']['admin']}"}
    assert mine["authorization"]
    wrong_key = c.get(f"/sandbox/sessions/{s['session_id']}", headers={"authorization": "Bearer kgk_nope"})
    wrong_id = c.get("/sandbox/sessions/doesnotexist", headers=mine)
    no_auth = c.get(f"/sandbox/sessions/{s['session_id']}")
    assert wrong_key.status_code == wrong_id.status_code == no_auth.status_code == 404
    assert wrong_key.json() == wrong_id.json() == no_auth.json()


def test_the_per_address_limit(setup: Any) -> None:
    c, *_ = setup
    start(c, "9.9.9.9")
    start(c, "9.9.9.9")
    r = c.post("/sandbox/sessions", json={"scenario": "sarah"}, headers={"x-forwarded-for": "9.9.9.9"})
    assert r.status_code == 429 and r.json()["error"]["code"] == "rate_limited"
    assert start(c, "8.8.8.8")["session_id"], "another address is not affected"


def test_the_global_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    c, *_ = build(tmp_path, monkeypatch, max_active=2)
    start(c, "1.0.0.1")
    start(c, "1.0.0.2")
    r = c.post("/sandbox/sessions", json={"scenario": "sarah"}, headers={"x-forwarded-for": "1.0.0.3"})
    assert r.status_code == 503 and r.json()["error"]["code"] == "sandbox_full"


def test_expired_sessions_are_reaped_and_deleted_through_the_governed_path(setup: Any) -> None:
    c, b, clock, _ = setup
    s = start(c)
    assert b.reap() == 0
    clock.t += 2 * 3600 + 1
    assert b.reap() == 1
    assert b.deleted == [b.store.get(s["session_id"])["slug"]]  # type: ignore[attr-defined]
    assert b.store.get(s["session_id"])["state"] == "gone"
    assert b.store.get(s["session_id"])["admin_key"] == "", "keys are wiped when the session is gone"
    again = c.get(f"/sandbox/sessions/{s['session_id']}", headers={"authorization": f"Bearer {s['keys']['admin']}"})
    assert again.status_code == 404


def test_ending_early(setup: Any) -> None:
    c, b, _clock, _ = setup
    s = start(c)
    r = c.delete(f"/sandbox/sessions/{s['session_id']}", headers={"authorization": f"Bearer {s['keys']['admin']}"})
    assert r.status_code == 204 and len(b.deleted) == 1  # type: ignore[attr-defined]


def test_an_unknown_scenario_and_script(setup: Any) -> None:
    c, *_ = setup
    assert c.post("/sandbox/sessions", json={"scenario": "nope"}).status_code == 422
    s = start(c, "7.7.7.7")
    r = c.post(f"/sandbox/sessions/{s['session_id']}/events/not-a-script", headers={"authorization": f"Bearer {s['keys']['admin']}"})
    assert r.status_code == 404


def test_the_pool_is_refilled_to_its_target(setup: Any) -> None:
    import asyncio

    _c, b, _clock, seeded = setup
    asyncio.run(b.refill())
    asyncio.run(b.refill())
    assert b.store.count("pool", "sarah") == 1 and len(seeded) == 1


def test_health_reports_counts_and_no_secrets(setup: Any) -> None:
    c, *_ = setup
    start(c)
    body = c.get("/sandbox/health").json()
    assert body["ok"] and "kgk_" not in json.dumps(body)


def test_the_database_file_is_private(setup: Any) -> None:
    _c, b, _clock, _ = setup
    path = Path(b.cfg.db_path)
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert time.time() > 0
