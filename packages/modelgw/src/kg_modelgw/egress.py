"""Egress context: who is asking, for what purpose, with which data classification. Set by the gateway (or by code that drives Graphiti)
before any router request. RouterTransport refuses a request that has none (fail closed, L8)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field


@dataclass
class EgressContext:
    classification: str
    purpose: str
    workspace_id: str | None = None
    principal_id: str | None = None
    correlation_id: str | None = None
    service: str = "kg-worker"
    attempt: int = 0  # incremented by the transport per HTTP try within one logical call
    fallback_used: bool = False
    extra: dict[str, str] = field(default_factory=dict)


_current: ContextVar[EgressContext | None] = ContextVar("egress_context", default=None)


def current_egress() -> EgressContext | None:
    return _current.get()


@contextmanager
def egress(
    *,
    classification: str,
    purpose: str,
    workspace_id: str | None = None,
    principal_id: str | None = None,
    correlation_id: str | None = None,
    service: str = "kg-worker",
) -> Iterator[EgressContext]:
    ctx = EgressContext(classification, purpose, workspace_id, principal_id, correlation_id, service)
    token = _current.set(ctx)
    try:
        yield ctx
    finally:
        _current.reset(token)
