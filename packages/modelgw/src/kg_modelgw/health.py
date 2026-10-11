"""Router health: is it up, does our key work, and does it serve every alias the purpose map needs."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

import httpx

from kg_modelgw.config import ModelConfig
from kg_modelgw.egress import egress

Status = Literal["ok", "unreachable", "auth_error", "degraded"]


@dataclass
class RouterHealth:
    status: Status
    missing_aliases: list[str] = field(default_factory=list)


async def router_health(origin: str, http: httpx.AsyncClient, config: ModelConfig, *, timeout: float = 3.0) -> RouterHealth:  # noqa: ASYNC109
    try:
        async with httpx.AsyncClient(timeout=timeout) as plain:  # /health is outside /v1 and carries no platform data
            if (await plain.get(f"{origin}/health")).status_code >= 500:
                return RouterHealth("unreachable")
        with egress(classification="public", purpose="health", service="kg-health"):
            r = await http.get("models", timeout=timeout)
    except (httpx.HTTPError, OSError):
        return RouterHealth("unreachable")
    if r.status_code in (401, 403):
        return RouterHealth("auth_error")
    if r.status_code >= 400:
        return RouterHealth("degraded")
    try:
        served = {m["id"] for m in r.json().get("data", [])}
    except (ValueError, KeyError, TypeError, AttributeError):
        return RouterHealth("degraded")
    missing = sorted(config.aliases() - served)
    return RouterHealth("degraded" if missing else "ok", missing)
