"""`KGClient` (async) and `KGClientSync`: a thin typed client over the platform's HTTP API. Both are the same eight operations over one shared rule set (`_core`)."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import httpx

from kg_platform_sdk import models as m
from kg_platform_sdk._core import Config, Op, parse_response, retry_wait, should_retry
from kg_platform_sdk.errors import KGError
from kg_platform_sdk.results import (
    Accepted,
    ContextPackage,
    InteractionStatus,
    MemoryRecord,
    SearchResult,
    Usage,
    WriteResult,
)

JsonBody = Mapping[str, Any]


def _model(cls: type[Any]) -> Callable[[Any, str | None], Any]:
    def parse(body: Any, request_id: str | None) -> Any:
        out = cls.model_validate(body)
        out.request_id = request_id
        return out

    return parse


def _evidence(body: Any, request_id: str | None) -> list[m.EvidenceOut]:
    return [m.EvidenceOut.model_validate(x) for x in body]


def _raw(body: Any, request_id: str | None) -> Any:
    return body


def _dump(model: Any) -> dict[str, Any]:
    return dict(model.model_dump(mode="json", exclude_unset=True, exclude_none=True))


# ---- the operations: pure descriptions, shared by both clients --------------------------------------------------------------------------------


class Operations:
    @staticmethod
    def build_context(
        task: str,
        *,
        subject_ids: Sequence[uuid.UUID | str] | None = None,
        profile: str | None = None,
        max_tokens: int | None = None,
        act_as: uuid.UUID | str | None = None,
        **options: Any,
    ) -> Op[ContextPackage]:
        body = m.ContextBuildIn(
            task=task,
            subject_ids=[uuid.UUID(str(s)) for s in subject_ids or []],
            **({"profile": profile} if profile else {}),
            **({"max_tokens": max_tokens} if max_tokens else {}),
            **options,
        )
        headers = {"x-act-as-principal": str(act_as)} if act_as else {}
        return Op("POST", "/v1/context/build", _model(ContextPackage), json=_dump(body), headers=headers)

    @staticmethod
    def ingest_interaction(idempotency_key: str | None = None, **fields: Any) -> Op[Accepted]:
        body = m.InteractionIn(**fields)
        return Op("POST", "/v1/interactions", _model(Accepted), json=_dump(body), mutating=True, idempotency_key=idempotency_key)

    @staticmethod
    def get_interaction_status(event_id: uuid.UUID | str) -> Op[InteractionStatus]:
        return Op("GET", f"/v1/interactions/{uuid.UUID(str(event_id))}", _model(InteractionStatus))

    @staticmethod
    def propose_memory(idempotency_key: str | None = None, **fields: Any) -> Op[WriteResult]:
        body = m.ProposalIn(**fields)
        return Op("POST", "/v1/memories/proposals", _model(WriteResult), json=_dump(body), mutating=True, idempotency_key=idempotency_key)

    @staticmethod
    def save_working_memory(
        run_id: str, content: str, *, ttl_hours: float | None = None, idempotency_key: str | None = None, **fields: Any
    ) -> Op[WriteResult]:
        body = m.MemorySaveIn(type="working", content=content, run_id=run_id, **({"ttl_hours": ttl_hours} if ttl_hours else {}), **fields)
        return Op("POST", "/v1/memories", _model(WriteResult), json=_dump(body), mutating=True, idempotency_key=idempotency_key)

    @staticmethod
    def get_memory(memory_id: uuid.UUID | str) -> Op[MemoryRecord]:
        return Op("GET", f"/v1/memories/{uuid.UUID(str(memory_id))}", _model(MemoryRecord))

    @staticmethod
    def publish_memory(
        memory_id: uuid.UUID | str,
        scopes: Sequence[Mapping[str, Any]],
        reason_code: str,
        *,
        revision: int,
        as_type: str | None,
        idempotency_key: str | None,
    ) -> Op[WriteResult]:
        body = m.MemoryPublishIn(scopes=[dict(s) for s in scopes], reason_code=reason_code)
        return Op(
            "POST",
            f"/v1/memories/{uuid.UUID(str(memory_id))}/publish",
            _model(WriteResult),
            json=_dump(body),
            params={"as_type": as_type} if as_type else None,
            headers={"if-match": f'W/"rev-{revision}"'},
            mutating=True,
            idempotency_key=idempotency_key,
        )

    @staticmethod
    def search_memory(query: str, **fields: Any) -> Op[SearchResult]:
        return Op("POST", "/v1/memories/search", _model(SearchResult), json=_dump(m.SearchIn(query=query, **fields)))

    @staticmethod
    def get_evidence(fact_id: uuid.UUID | str) -> Op[list[m.EvidenceOut]]:
        return Op("GET", f"/v1/relationships/{uuid.UUID(str(fact_id))}/evidence", _evidence)

    @staticmethod
    def list_profiles() -> Op[list[dict[str, Any]]]:
        return Op("GET", "/v1/context/profiles", _raw)

    @staticmethod
    def usage() -> Op[Usage]:
        return Op("GET", "/v1/me/usage", _model(Usage))


def _json(r: httpx.Response) -> Any:
    """The JSON body, or None when there is none or it is not JSON (a proxy's HTML error page): the status still decides the exception."""
    if not r.content:
        return None
    try:
        return r.json()
    except ValueError:
        return None


def _revision_of(etag: str | None) -> int:
    if not etag or '"rev-' not in etag:
        raise KGError("the API did not return the memory's revision (ETag)", code="missing_etag")
    return int(etag.split('"rev-', 1)[1].rstrip('"'))


# ---- async ----------------------------------------------------------------------------------------------------------------------------------------------


class KGClient:
    """Async client. Use as `async with KGClient(base_url=..., api_key=...) as kg:`. The key is never logged and never appears in a repr."""

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        *,
        timeout: float = 30.0,
        max_retries: int = 3,
        backoff: float = 0.25,
        max_retry_wait: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Any] = asyncio.sleep,
    ):
        self._cfg = Config.build(
            base_url, api_key, timeout=timeout, max_retries=max_retries, backoff=backoff, max_retry_wait=max_retry_wait
        )
        self._http = httpx.AsyncClient(base_url=self._cfg.base_url, timeout=timeout, transport=transport)
        self._sleep = sleep

    def __repr__(self) -> str:
        return f"KGClient({self._cfg!r})"

    async def __aenter__(self) -> KGClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _send_raw[T](self, op: Op[T]) -> tuple[T, httpx.Headers]:
        headers = op.prepared_headers(self._cfg.key)
        attempt = 0
        while True:
            try:
                r = await self._http.request(op.method, op.path, params=op.params, json=op.json, headers=headers)
            except httpx.TransportError as exc:
                raise KGError(f"could not reach the platform ({type(exc).__name__})", code="transport_error") from None
            body = _json(r)
            result, err = parse_response(r.status_code, r.headers, body, op)
            if err is None:
                assert result is not None or body is None
                return result, r.headers  # type: ignore[return-value]
            wait = retry_wait(self._cfg, attempt, err.retry_after)
            if not should_retry(self._cfg, attempt, r.status_code, err) or wait is None:
                raise err
            await self._sleep(wait)
            attempt += 1

    async def _send[T](self, op: Op[T]) -> T:
        return (await self._send_raw(op))[0]

    # ---- the eight operations ----------------------------------------------------------------------------------------------------------------------

    async def build_context(
        self,
        task: str,
        subject_ids: Sequence[uuid.UUID | str] | None = None,
        profile: str | None = None,
        max_tokens: int | None = None,
        **options: Any,
    ) -> ContextPackage:
        """The smallest sufficiently informative, authorised, budgeted, cited context for `task`. Extra keyword options are the API's own (`as_of`, `known_at`, `project`,
        `evidence_required`, `include`, `compression`, `response_mode`, `allow_partial`, `run_id`, ...)."""
        return await self._send(Operations.build_context(task, subject_ids=subject_ids, profile=profile, max_tokens=max_tokens, **options))

    async def ingest_interaction(self, *, idempotency_key: str | None = None, **fields: Any) -> Accepted:
        """Submits an event (`source_system`, `source_ref`, `kind`, `content`, `occurred_at`...). Retries reuse one Idempotency-Key, so the platform sees one write."""
        return await self._send(Operations.ingest_interaction(idempotency_key, **fields))

    async def get_interaction_status(self, event_id: uuid.UUID | str) -> InteractionStatus:
        return await self._send(Operations.get_interaction_status(event_id))

    async def propose_memory(self, *, idempotency_key: str | None = None, **fields: Any) -> WriteResult:
        """Proposes a source-backed memory (`type`, `content`, `source_event_ids` / `fact_ids` / `memory_ids`, `scopes`, `subject_ids`...). The platform decides its status."""
        return await self._send(Operations.propose_memory(idempotency_key, **fields))

    async def save_working_memory(
        self, run_id: str, content: str, *, ttl_hours: float | None = None, idempotency_key: str | None = None, **fields: Any
    ) -> WriteResult:
        """A private working note tied to a run: visible to this principal and run only, expires on its own."""
        return await self._send(
            Operations.save_working_memory(run_id, content, ttl_hours=ttl_hours, idempotency_key=idempotency_key, **fields)
        )

    async def publish_memory(
        self,
        memory_id: uuid.UUID | str,
        scopes: Sequence[Mapping[str, Any]],
        *,
        reason_code: str = "share_finding",
        revision: int | None = None,
        as_type: str | None = None,
        idempotency_key: str | None = None,
    ) -> WriteResult:
        """Shares one of your private memories into `scopes` as a NEW proposal (the original stays private). `revision` defaults to the memory's current one."""
        if revision is None:
            _, headers = await self._send_raw(Operations.get_memory(memory_id))
            revision = _revision_of(headers.get("etag"))
        return await self._send(
            Operations.publish_memory(memory_id, scopes, reason_code, revision=revision, as_type=as_type, idempotency_key=idempotency_key)
        )

    async def search_memory(self, query: str, **fields: Any) -> SearchResult:
        return await self._send(Operations.search_memory(query, **fields))

    async def get_evidence(self, fact_id: uuid.UUID | str) -> list[m.EvidenceOut]:
        return await self._send(Operations.get_evidence(fact_id))

    async def list_profiles(self) -> list[dict[str, Any]]:
        return await self._send(Operations.list_profiles())

    async def usage(self) -> Usage:
        return await self._send(Operations.usage())


# ---- sync ---------------------------------------------------------------------------------------------------------------------------------------------------


class KGClientSync:
    """The same operations over a blocking `httpx.Client`."""

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        *,
        timeout: float = 30.0,
        max_retries: int = 3,
        backoff: float = 0.25,
        max_retry_wait: float = 30.0,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], Any] = time.sleep,
    ):
        self._cfg = Config.build(
            base_url, api_key, timeout=timeout, max_retries=max_retries, backoff=backoff, max_retry_wait=max_retry_wait
        )
        self._http = httpx.Client(base_url=self._cfg.base_url, timeout=timeout, transport=transport)
        self._sleep = sleep

    def __repr__(self) -> str:
        return f"KGClientSync({self._cfg!r})"

    def __enter__(self) -> KGClientSync:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    def _send_raw[T](self, op: Op[T]) -> tuple[T, httpx.Headers]:
        headers = op.prepared_headers(self._cfg.key)
        attempt = 0
        while True:
            try:
                r = self._http.request(op.method, op.path, params=op.params, json=op.json, headers=headers)
            except httpx.TransportError as exc:
                raise KGError(f"could not reach the platform ({type(exc).__name__})", code="transport_error") from None
            body = _json(r)
            result, err = parse_response(r.status_code, r.headers, body, op)
            if err is None:
                return result, r.headers  # type: ignore[return-value]
            wait = retry_wait(self._cfg, attempt, err.retry_after)
            if not should_retry(self._cfg, attempt, r.status_code, err) or wait is None:
                raise err
            self._sleep(wait)
            attempt += 1

    def _send[T](self, op: Op[T]) -> T:
        return self._send_raw(op)[0]

    def build_context(
        self,
        task: str,
        subject_ids: Sequence[uuid.UUID | str] | None = None,
        profile: str | None = None,
        max_tokens: int | None = None,
        **options: Any,
    ) -> ContextPackage:
        return self._send(Operations.build_context(task, subject_ids=subject_ids, profile=profile, max_tokens=max_tokens, **options))

    def ingest_interaction(self, *, idempotency_key: str | None = None, **fields: Any) -> Accepted:
        return self._send(Operations.ingest_interaction(idempotency_key, **fields))

    def get_interaction_status(self, event_id: uuid.UUID | str) -> InteractionStatus:
        return self._send(Operations.get_interaction_status(event_id))

    def propose_memory(self, *, idempotency_key: str | None = None, **fields: Any) -> WriteResult:
        return self._send(Operations.propose_memory(idempotency_key, **fields))

    def save_working_memory(
        self, run_id: str, content: str, *, ttl_hours: float | None = None, idempotency_key: str | None = None, **fields: Any
    ) -> WriteResult:
        return self._send(Operations.save_working_memory(run_id, content, ttl_hours=ttl_hours, idempotency_key=idempotency_key, **fields))

    def publish_memory(
        self,
        memory_id: uuid.UUID | str,
        scopes: Sequence[Mapping[str, Any]],
        *,
        reason_code: str = "share_finding",
        revision: int | None = None,
        as_type: str | None = None,
        idempotency_key: str | None = None,
    ) -> WriteResult:
        if revision is None:
            _, headers = self._send_raw(Operations.get_memory(memory_id))
            revision = _revision_of(headers.get("etag"))
        return self._send(
            Operations.publish_memory(memory_id, scopes, reason_code, revision=revision, as_type=as_type, idempotency_key=idempotency_key)
        )

    def search_memory(self, query: str, **fields: Any) -> SearchResult:
        return self._send(Operations.search_memory(query, **fields))

    def get_evidence(self, fact_id: uuid.UUID | str) -> list[m.EvidenceOut]:
        return self._send(Operations.get_evidence(fact_id))

    def list_profiles(self) -> list[dict[str, Any]]:
        return self._send(Operations.list_profiles())

    def usage(self) -> Usage:
        return self._send(Operations.usage())
