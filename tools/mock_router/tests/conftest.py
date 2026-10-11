from __future__ import annotations

import socket
import threading
import time

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient

from tools.mock_router.app import MockConfig, create_app

KEY = "test-key"
AUTH = {"Authorization": f"Bearer {KEY}"}


def make_client(**cfg) -> TestClient:
    return TestClient(create_app(MockConfig(api_key=KEY, **cfg)))


@pytest.fixture()
def client() -> TestClient:
    return make_client()


class LiveServer:
    def __init__(self, app):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        self.port = s.getsockname()[1]
        s.close()
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="error"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    def __enter__(self):
        self.thread.start()
        for _ in range(100):
            try:
                if httpx.get(f"http://127.0.0.1:{self.port}/health").status_code == 200:
                    return self
            except httpx.HTTPError:
                time.sleep(0.05)
        raise RuntimeError("mock router did not start")

    def __exit__(self, *a):
        self.server.should_exit = True
        self.thread.join(timeout=5)


@pytest.fixture(scope="module")
def live():
    with LiveServer(create_app(MockConfig(api_key=KEY, retry_after_seconds=0))) as srv:
        yield srv
