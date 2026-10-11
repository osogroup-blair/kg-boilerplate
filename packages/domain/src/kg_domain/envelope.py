from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


class ErrorBody(BaseModel):
    code: str
    message: str
    request_id: str
    retry_after: int | None = None
    details: dict[str, Any] | None = None


class ErrorEnvelope(BaseModel):
    """Every non-2xx response. `details` never echoes request values (field paths only)."""

    error: ErrorBody


class Page[T](BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    items: list[T]
    next_cursor: str | None = None
