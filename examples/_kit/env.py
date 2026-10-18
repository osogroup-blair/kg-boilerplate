"""Where the examples find the stack. Mock mode is the default; `--live` (owner action) points at a real router and disables the mock-only helpers."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[2]
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


@dataclass(frozen=True)
class Env:
    base_url: str
    mcp_url: str
    mock_url: str
    live: bool

    @property
    def mock_available(self) -> bool:
        return not self.live


def load_env(*, live: bool = False) -> Env:
    env = Env(
        base_url=os.environ.get("KG_BASE_URL", "http://127.0.0.1:8000"),
        mcp_url=os.environ.get("KG_MCP_URL", "http://127.0.0.1:8790/mcp/"),
        mock_url=os.environ.get("KG_MOCK_URL", "http://127.0.0.1:8782"),
        live=live,
    )
    host = urlparse(env.base_url).hostname or ""
    if host not in LOCAL_HOSTS and not live:
        raise SystemExit(
            f"refusing to run the examples against {host}: they create workspaces and load mock fixtures. Pass --live to override (owner action)."
        )
    return env


def dev_credentials() -> dict[str, str]:
    path = ROOT / ".local" / "dev-credentials.json"
    if not path.exists():
        raise SystemExit("missing .local/dev-credentials.json: run `make seed` first")
    return dict(json.loads(path.read_text()))
