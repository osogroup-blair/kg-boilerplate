"""The failure-injection suite (`make test-resilience`): real worker processes that are crashed, frozen and starved. It restarts compose services,
so it never runs as part of the ordinary suites: set KG_RESILIENCE=1 (the make target does)."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import threading
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from tests.conftest import APP_URL, NEO4J_PASSWORD, NEO4J_URI, NEO4J_USER, OWNER_URL

ROOT = Path(__file__).resolve().parents[2]
_ports = iter(range(19200, 19400))


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if os.environ.get("KG_RESILIENCE") == "1":
        return
    skip = pytest.mark.skip(reason="failure-injection suite: run `make test-resilience` (sets KG_RESILIENCE=1)")
    for item in items:
        if "/tests/resilience/" in str(item.fspath):
            item.add_marker(skip)


class WorkerProcess:
    """One `python -m kg_worker` subprocess against kg_test, with its own metrics port and log file."""

    def __init__(self, tmp: Path, name: str, **env: str):
        self.name, self.tmp, self.env = name, tmp, env
        self.proc: subprocess.Popen[bytes] | None = None
        self.starts = 0

    def _environ(self) -> dict[str, str]:
        return {
            **os.environ,
            "ENV": "test",
            "PYTHONUNBUFFERED": "1",  # the log file is read while the process runs
            "DATABASE_URL_APP": APP_URL,
            "DATABASE_URL_OWNER": OWNER_URL,
            "NEO4J_URI": NEO4J_URI,
            "NEO4J_USER": NEO4J_USER,
            "NEO4J_PASSWORD": NEO4J_PASSWORD,
            "OSO_ROUTER_MODE": "mock",
            "OSO_ROUTER_MOCK_URL": "http://127.0.0.1:8782/v1",
            "LOG_LEVEL": "INFO",
            "WORKER_HEARTBEAT_SECONDS": "1",
            "WORKER_POLL_BUSY_SECONDS": "0.05",
            "WORKER_POLL_IDLE_SECONDS": "0.3",
            "WORKER_SHUTDOWN_GRACE_SECONDS": "5",
            "METRICS_PORT_WORKER": str(next(_ports)),
            **self.env,
        }

    def start(self) -> None:
        self.starts += 1
        log = (self.tmp / f"{self.name}.log").open("ab")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "kg_worker"], env=self._environ(), stdout=log, stderr=subprocess.STDOUT, cwd=ROOT
        )  # noqa: S603

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def kill(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.kill()  # SIGKILL: no handlers, no cleanup
            self.proc.wait(timeout=30)

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=30)

    @property
    def log(self) -> str:
        path = self.tmp / f"{self.name}.log"
        return path.read_text(errors="replace") if path.exists() else ""


class Supervisor:
    """Keeps a worker running: whenever it dies (an injected crash, a SIGKILL from the test) it is started again, like a container restart policy."""

    def __init__(self, workers: list[WorkerProcess], restart_delay: float = 0.3):
        self.workers, self.restart_delay = workers, restart_delay
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self._stop.is_set():
            for w in self.workers:
                if not w.alive and not self._stop.is_set():
                    w.start()
            self._stop.wait(self.restart_delay)

    async def __aenter__(self) -> Supervisor:
        self._thread.start()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        self._stop.set()
        await asyncio.to_thread(self._thread.join, 10)
        for w in self.workers:
            await asyncio.to_thread(w.stop)


@pytest_asyncio.fixture()
async def resilient_api(api) -> AsyncIterator[Any]:  # type: ignore[no-untyped-def]
    """The in-process API with the request-time projection fast path switched off, so commands are projected by the worker processes under test."""
    settings = api.app.state.kg.settings
    original = settings.projection_request_budget_seconds
    settings.projection_request_budget_seconds = 0.0
    yield api
    settings.projection_request_budget_seconds = original
