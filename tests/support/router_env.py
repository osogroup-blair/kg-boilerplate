"""Test rig: the production RouterTransport + ModelGateway wired to the in-process mock router through a request spy."""

from __future__ import annotations

import httpx
from kg_common.settings import Settings
from kg_modelgw.calllog import MemorySink
from kg_modelgw.config import load_config
from kg_modelgw.factory import build_router_stack
from kg_modelgw.gateway import ModelGateway

from tests.support.spy import SpyTransport
from tools.mock_router.app import MockConfig, create_app

KEY = "test-key"


class Env:
    def __init__(self, service: str = "kg-worker", **mock_cfg):
        self.settings = Settings(
            _env_file=None, env="test", oso_router_mock_url="http://mock/v1", oso_router_api_key_worker=KEY, oso_router_api_key_api=KEY
        )
        self.app = create_app(MockConfig(api_key=KEY, retry_after_seconds=3, **mock_cfg))
        self.spy = SpyTransport(httpx.ASGITransport(app=self.app))
        self.sink = MemorySink()
        self.stack = build_router_stack(
            self.settings,
            service=service,
            sink=self.sink,
            inner=self.spy,
            config=load_config(self.settings.model_purposes_path, self.settings.pricing_path),
        )
        self.sleeps: list[float] = []

        async def fake_sleep(s: float) -> None:
            self.sleeps.append(s)

        self.gw = ModelGateway(self.stack.http, self.stack.config, self.sink, sleep=fake_sleep, jitter=lambda: 0.0)

    @property
    def log(self):
        return self.sink.records

    def faults(self, *faults: dict) -> None:
        self.app.state.faults.clear()
        self.app.state.faults.extend(faults)
