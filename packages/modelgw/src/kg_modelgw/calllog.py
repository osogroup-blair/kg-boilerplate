"""Metadata-only call log (ADR-012 §6): no message content, no vectors. Rows go to model_calls in batches."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from typing import Protocol

from kg_common.ids import uuid7
from kg_common.tokens import TOKENIZER_ID
from kg_db.session import system_session, tenant_session
from kg_domain.principal import WorkspaceContext
from kg_obs.logging import get_logger
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

log = get_logger("kg.modelgw.calllog")


@dataclass
class CallRecord:
    purpose: str
    alias: str
    operation: str  # chat | embeddings | models
    classification: str
    service: str
    outcome: str  # ok | <router error code> | egress_denied | transport_error | invalid_structured_output
    workspace_id: str | None = None
    correlation_id: str | None = None
    router_request_id: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    usage_estimated: bool = False
    est_cost_usd: float | None = None
    latency_ms: float = 0.0
    attempts: int = 1
    fallback_used: bool = False
    tokenizer: str = TOKENIZER_ID


class CallSink(Protocol):
    def write(self, record: CallRecord) -> None: ...
    async def flush(self) -> None: ...


class MemorySink:
    def __init__(self) -> None:
        self.records: list[CallRecord] = []

    def write(self, record: CallRecord) -> None:
        self.records.append(record)

    async def flush(self) -> None:
        return None


_INSERT = text("""
    INSERT INTO model_calls (id, workspace_id, purpose, alias, operation, classification, correlation_id, router_request_id, prompt_tokens,
                             completion_tokens, usage_estimated, est_cost_usd, latency_ms, attempts, outcome, fallback_used, tokenizer, service)
    VALUES (:id, :ws, :purpose, :alias, :op, :cls, :corr, :rid, :pt, :ct, :est, :cost, :lat, :att, :out, :fb, :tok, :svc)""")


class DbCallSink:
    """Buffers records and inserts them per workspace (RLS WITH CHECK needs the workspace set). `flush()` is awaited by callers that
    need the row immediately (router-check); otherwise a background task flushes every `interval` seconds."""

    def __init__(self, engine: AsyncEngine, *, interval: float = 2.0, max_buffer: int = 5000):
        self._engine, self._interval, self._max = engine, interval, max_buffer
        self._buffer: list[CallRecord] = []
        self._task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._loop())

    async def aclose(self) -> None:
        if self._task:
            self._task.cancel()
            self._task = None
        await self.flush()

    def write(self, record: CallRecord) -> None:
        if len(self._buffer) >= self._max:
            self._buffer.pop(0)  # never let telemetry grow without bound; the oldest row is dropped
        self._buffer.append(record)

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            try:
                await self.flush()
            except Exception:
                log.warning("call_log_flush_failed")

    async def flush(self) -> None:
        async with self._lock:
            batch, self._buffer = self._buffer, []
            if not batch:
                return
            by_ws: dict[str | None, list[CallRecord]] = {}
            for rec in batch:
                by_ws.setdefault(rec.workspace_id, []).append(rec)
            failed: list[CallRecord] = []
            for ws, records in by_ws.items():
                try:
                    session = (
                        tenant_session(self._engine, WorkspaceContext.for_system_task(uuid.UUID(ws), reason="model call log"))
                        if ws
                        else system_session(self._engine)
                    )
                    async with session as conn:
                        await conn.execute(_INSERT, [self._params(r) for r in records])
                except Exception:
                    log.warning("call_log_insert_failed", rows=len(records))
                    failed.extend(records)
            self._buffer = failed + self._buffer

    @staticmethod
    def _params(r: CallRecord) -> dict[str, object]:
        return {
            "id": uuid7(),
            "ws": uuid.UUID(r.workspace_id) if r.workspace_id else None,
            "purpose": r.purpose,
            "alias": r.alias,
            "op": r.operation,
            "cls": r.classification,
            "corr": r.correlation_id,
            "rid": r.router_request_id,
            "pt": r.prompt_tokens,
            "ct": r.completion_tokens,
            "est": r.usage_estimated,
            "cost": r.est_cost_usd,
            "lat": r.latency_ms,
            "att": r.attempts,
            "out": r.outcome,
            "fb": r.fallback_used,
            "tok": r.tokenizer,
            "svc": r.service,
        }
