"""Helpers shared by the P2 routers: the Idempotency-Key header, ETag/If-Match, replayable JSON responses."""

from __future__ import annotations

import re
import uuid
from collections.abc import Awaitable, Callable
from typing import Annotated, Any

from fastapi import Header
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from kg_common.errors import IdempotencyKeyRequired, PreconditionFailed
from kg_knowledge import idempotency
from sqlalchemy.ext.asyncio import AsyncConnection

IdempotencyKeyHeader = Annotated[str | None, Header(alias="Idempotency-Key", max_length=255)]
IfMatchHeader = Annotated[str | None, Header(alias="If-Match", max_length=64)]
_REV = re.compile(r'^(?:W/)?"rev-(\d{1,9})"$')


def require_key(value: str | None) -> str:
    if value is None or not value.strip():
        raise IdempotencyKeyRequired()
    return value.strip()


def etag(revision: int) -> str:
    return f'W/"rev-{revision}"'


def parse_if_match(value: str | None) -> int | None:
    """None when the header is absent (the service answers 428); a malformed value can never match, so 412."""
    if value is None:
        return None
    m = _REV.match(value.strip())
    if m is None:
        raise PreconditionFailed()
    return int(m.group(1))


async def run_idempotent(
    conn: AsyncConnection,
    workspace_id: uuid.UUID,
    key: str,
    route: str,
    parts: Any,
    producer: Callable[[], Awaitable[tuple[int, Any, dict[str, str]]]],
) -> JSONResponse:
    """Replays a stored response for a repeated key, 409s a reused key with a different request, otherwise runs the mutation and stores its response in the
    same transaction. Errors raise through (and roll back), so a failed attempt can be retried with the same key."""
    digest = idempotency.request_hash(route, parts)
    replay = await idempotency.begin(conn, workspace_id, key, route, digest)
    if replay is not None:
        return JSONResponse(replay.response, status_code=replay.status_code, headers={"Idempotent-Replay": "true"})
    status, body, headers = await producer()
    payload = jsonable_encoder(body)
    await idempotency.record(conn, workspace_id, key, route, digest, status, payload)
    return JSONResponse(payload, status_code=status, headers=headers)
