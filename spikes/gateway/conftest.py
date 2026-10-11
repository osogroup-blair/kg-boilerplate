from __future__ import annotations

import json
import uuid

import httpx
import pytest

from gateway.gateway import Correlation, ModelGateway
from spy import SpyTransport
from tools.mock_router.app import MockConfig, create_app

KEY = "test-key"


class Env:
    def __init__(self, **cfg):
        self.app = create_app(MockConfig(api_key=KEY, retry_after_seconds=3, **cfg))
        self.spy = SpyTransport(httpx.ASGITransport(app=self.app))
        self.sleeps: list[float] = []

        async def fake_sleep(s: float) -> None:
            self.sleeps.append(s)

        self.gw = ModelGateway("http://mock/v1", KEY, transport=self.spy, sleep=fake_sleep, jitter=lambda: 0.0)

    def faults(self, *faults: dict) -> None:
        self.app.state.faults.clear()
        self.app.state.faults.extend(faults)


@pytest.fixture()
def env() -> Env:
    return Env()


@pytest.fixture()
def corr() -> Correlation:
    return Correlation(str(uuid.uuid4()), "ws_demo", "agent_research", "kg-worker")
