"""Request-scoped identifiers. Workspace is only ever logged as an HMAC hash (never the raw id)."""

from __future__ import annotations

import hashlib
import hmac
from contextvars import ContextVar
from typing import Any

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
correlation_id_var: ContextVar[str | None] = ContextVar("correlation_id", default=None)
principal_id_var: ContextVar[str | None] = ContextVar("principal_id", default=None)
workspace_hash_var: ContextVar[str | None] = ContextVar("workspace_hash", default=None)
# The service name is process-wide (set once at startup), NOT a ContextVar: a value set in the lifespan task would not reach request tasks.
_service = "kg"


def set_service(name: str) -> None:
    global _service
    _service = name


def service_name() -> str:
    return _service


def hmac_hash(value: str, secret: str | bytes) -> str:
    key = secret.encode() if isinstance(secret, str) else secret
    return hmac.new(key, value.encode(), hashlib.sha256).hexdigest()[:16]


def current() -> dict[str, Any]:
    return {
        k: v
        for k, v in {
            "request_id": request_id_var.get(),
            "correlation_id": correlation_id_var.get(),
            "principal_id": principal_id_var.get(),
            "workspace_hash": workspace_hash_var.get(),
            "service": service_name(),
        }.items()
        if v is not None
    }


def bind(**values: str | None) -> None:
    var_map: dict[str, ContextVar[Any]] = {
        "request_id": request_id_var,
        "correlation_id": correlation_id_var,
        "principal_id": principal_id_var,
        "workspace_hash": workspace_hash_var,
    }
    for key, value in values.items():
        if key in var_map and value is not None:
            var_map[key].set(value)


def clear() -> None:
    for var in (request_id_var, correlation_id_var, principal_id_var, workspace_hash_var):
        var.set(None)
