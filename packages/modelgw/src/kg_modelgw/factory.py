"""Wiring from Settings: config + sink + RouterTransport + ModelGateway. Graphiti gets the SAME transport (see kg_graph.factory)."""

from __future__ import annotations

from dataclasses import dataclass

import httpx
from kg_common.settings import Settings

from kg_modelgw.calllog import CallSink
from kg_modelgw.config import ModelConfig, load_config
from kg_modelgw.gateway import ModelGateway
from kg_modelgw.narrowing import WorkspaceEgress
from kg_modelgw.transport import RouterTransport


@dataclass
class RouterStack:
    config: ModelConfig
    transport: RouterTransport
    http: httpx.AsyncClient
    gateway: ModelGateway
    base_url: str
    narrowing: WorkspaceEgress | None = None

    async def aclose(self) -> None:
        await self.http.aclose()


def build_router_stack(
    settings: Settings,
    *,
    service: str,
    sink: CallSink,
    inner: httpx.AsyncBaseTransport | None = None,
    config: ModelConfig | None = None,
    timeout: float = 120.0,
) -> RouterStack:
    cfg = config or load_config(settings.model_purposes_path, settings.pricing_path)
    base_url = settings.router_base_url
    engine = getattr(sink, "engine", None)
    narrowing = (
        WorkspaceEgress(engine, ttl_seconds=settings.workspace_egress_ttl_seconds) if engine is not None else None
    )  # P8: only a sink with a database knows workspaces
    transport = RouterTransport(
        base_url=base_url,
        api_key=settings.router_api_key(service).get_secret_value(),
        service=service,
        config=cfg,
        sink=sink,
        hmac_secret=settings.telemetry_hmac_secret.get_secret_value(),
        inner=inner,
        production=settings.is_production,
        narrowing=narrowing,
    )
    http = httpx.AsyncClient(base_url=base_url + "/", transport=transport, timeout=timeout)
    return RouterStack(cfg, transport, http, ModelGateway(http, cfg, sink), base_url, narrowing)
