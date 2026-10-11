from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from kg_common.settings import Settings
from kg_governance.authn.resolver import Authenticator
from kg_graph.service import GraphService
from kg_ingestion.services import IngestionServices
from kg_knowledge.services import KnowledgeServices
from kg_modelgw.calllog import DbCallSink
from kg_modelgw.factory import RouterStack
from kg_modelgw.health import RouterHealth
from sqlalchemy.ext.asyncio import AsyncEngine


@dataclass
class AppState:
    settings: Settings
    engine: AsyncEngine
    authenticator: Authenticator
    router: RouterStack
    call_sink: DbCallSink
    graph: GraphService
    knowledge: KnowledgeServices
    ingestion: IngestionServices
    _router_health: tuple[float, RouterHealth] | None = field(default=None, repr=False)
    extras: dict[str, Any] = field(default_factory=dict)

    def cached_router_health(self, ttl: float = 5.0) -> RouterHealth | None:
        if self._router_health and time.monotonic() - self._router_health[0] < ttl:
            return self._router_health[1]
        return None

    def store_router_health(self, value: RouterHealth) -> None:
        self._router_health = (time.monotonic(), value)
