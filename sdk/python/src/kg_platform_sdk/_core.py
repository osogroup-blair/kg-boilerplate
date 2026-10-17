"""The transport-independent half of the client: configuration, request building, the retry policy, response parsing. No I/O happens here, so the sync and the async client
share every rule and the tests can drive the policy without a network.

Rules (phase-7 plan §5.7, ADR-040):
* the key comes from an argument or `KG_API_KEY`, and is NEVER logged or shown in a `repr`;
* every mutating call carries an `Idempotency-Key`, generated once per logical call and REUSED by each of its retries, so the server sees one logical write;
* retries (default 3) are bounded to 429, 502, 503 and 504, honour `retry_after` and add jitter; a wait longer than `max_retry_wait` (a daily quota) is raised, not slept;
* every non-2xx answer becomes a typed exception built from the one error envelope.
"""

from __future__ import annotations

import logging
import os
import random
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from kg_platform_sdk.errors import KGError, error_from

log = logging.getLogger("kg_platform_sdk")
RETRY_STATUSES = frozenset({429, 502, 503, 504})
DEFAULT_BASE_URL = "http://127.0.0.1:8000"
USER_AGENT = "kg-platform-sdk/0.1.0"


class _Secret:
    """Holds the key so that no repr, str or traceback shows it."""

    __slots__ = ("_value",)

    def __init__(self, value: str):
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "<redacted>"

    __str__ = __repr__


@dataclass(frozen=True)
class Config:
    base_url: str
    key: _Secret
    timeout: float = 30.0
    max_retries: int = 3
    backoff: float = 0.25
    max_retry_wait: float = 30.0

    @classmethod
    def build(
        cls,
        base_url: str | None,
        api_key: str | None,
        *,
        timeout: float = 30.0,
        max_retries: int = 3,
        backoff: float = 0.25,
        max_retry_wait: float = 30.0,
    ) -> Config:
        key = api_key or os.environ.get("KG_API_KEY")
        if not key:
            raise ValueError("no API key: pass api_key= or set KG_API_KEY")
        if max_retries < 0:
            raise ValueError("max_retries must be >= 0")
        return cls(
            (base_url or os.environ.get("KG_BASE_URL") or DEFAULT_BASE_URL).rstrip("/"),
            _Secret(key),
            timeout,
            max_retries,
            backoff,
            max_retry_wait,
        )

    def __repr__(self) -> str:
        return f"Config(base_url={self.base_url!r}, api_key=<redacted>, timeout={self.timeout}, max_retries={self.max_retries})"


@dataclass
class Op[T]:
    """One API call, described. `parse` turns the JSON body into the result; `mutating` calls get an Idempotency-Key."""

    method: str
    path: str
    parse: Callable[[Any, str | None], T]
    json: Any = None
    params: dict[str, Any] | None = None
    headers: dict[str, str] = field(default_factory=dict)
    mutating: bool = False
    idempotency_key: str | None = None

    def prepared_headers(self, key: _Secret) -> dict[str, str]:
        headers = {"authorization": f"Bearer {key.reveal()}", "accept": "application/json", "user-agent": USER_AGENT, **self.headers}
        if self.mutating:
            if self.idempotency_key is None:
                self.idempotency_key = f"sdk-{uuid.uuid4().hex}"  # chosen ONCE per logical call: each retry re-sends the same one
            headers["idempotency-key"] = self.idempotency_key
        return headers


def retry_wait(cfg: Config, attempt: int, retry_after: int | None, rand: Callable[[], float] = random.random) -> float | None:
    """Seconds to wait before attempt `attempt + 1`, or None when the wait is too long to be worth sleeping (the error is raised instead)."""
    if retry_after is not None:
        if float(retry_after) > cfg.max_retry_wait:
            return None  # a daily quota: sleeping for hours is the caller's decision
        return min(float(retry_after) + rand() * min(1.0, 0.25 * max(1.0, float(retry_after))), cfg.max_retry_wait)
    wait = cfg.backoff * (2**attempt) * (0.5 + rand() / 2.0)
    return wait if wait <= cfg.max_retry_wait else None


def should_retry(cfg: Config, attempt: int, status: int, err: KGError | None) -> bool:
    return status in RETRY_STATUSES and attempt < cfg.max_retries


def parse_response[T](status: int, headers: Any, body: Any, op: Op[T]) -> tuple[T | None, KGError | None]:
    """(result, None) for a 2xx answer, (None, exception) otherwise."""
    request_id = headers.get("x-request-id")
    if 200 <= status < 300:
        return op.parse(body, request_id), None
    return None, error_from(status, body, request_id=request_id, retry_after_header=headers.get("retry-after"))
