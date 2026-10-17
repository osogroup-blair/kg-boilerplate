"""The MCP server's whole configuration: where the platform API is, where to listen, how long to wait. There is NO database setting, NO router setting and NO service
credential: the server holds nothing but the address of the API, and each tool call forwards the CALLER's own key (phase-7 plan §5.8, ADR-040)."""

from __future__ import annotations

import os
from dataclasses import dataclass

ENV_FIELDS = ("KG_API_URL", "MCP_HOST", "MCP_PORT", "MCP_TIMEOUT_SECONDS")


@dataclass(frozen=True)
class McpSettings:
    api_url: str = "http://127.0.0.1:8000"
    host: str = "127.0.0.1"
    port: int = 8010
    timeout_seconds: float = 30.0

    @classmethod
    def from_env(cls) -> McpSettings:
        env = os.environ
        return cls(
            api_url=env.get("KG_API_URL", cls.api_url),
            host=env.get("MCP_HOST", cls.host),
            port=int(env.get("MCP_PORT", cls.port)),
            timeout_seconds=float(env.get("MCP_TIMEOUT_SECONDS", cls.timeout_seconds)),
        )
