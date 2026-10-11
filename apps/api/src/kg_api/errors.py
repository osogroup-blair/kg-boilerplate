"""Every error leaves the API as the same envelope (404/405 for unknown routes and 422 included)."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from kg_common.errors import NotFound, PlatformError, Unauthenticated, ValidationFailed
from kg_domain.envelope import ErrorBody, ErrorEnvelope
from kg_obs import context
from kg_obs.logging import get_logger
from starlette.exceptions import HTTPException as StarletteHTTPException

log = get_logger("kg.http.errors")
_STATUS_CODES = {
    404: ("not_found", "Resource not found"),
    405: ("method_not_allowed", "Method not allowed"),
    401: ("unauthenticated", "Authentication required"),
    403: ("forbidden", "Action not permitted"),
    413: ("payload_too_large", "Request body too large"),
}


def envelope(
    status: int,
    code: str,
    message: str,
    *,
    retry_after: int | None = None,
    details: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    body = ErrorEnvelope(
        error=ErrorBody(code=code, message=message, request_id=context.request_id_var.get() or "", retry_after=retry_after, details=details)
    )
    hdrs = dict(headers or {})
    if retry_after is not None:
        hdrs["retry-after"] = str(retry_after)
    return JSONResponse(body.model_dump(mode="json"), status_code=status, headers=hdrs)


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(PlatformError)
    async def _platform(_: Request, exc: PlatformError) -> JSONResponse:
        headers = {"www-authenticate": "Bearer"} if isinstance(exc, Unauthenticated) else None
        if isinstance(exc, NotFound):
            exc.details = None  # never distinguish "exists elsewhere" from "does not exist"
        return envelope(exc.http_status, exc.code, exc.message, retry_after=exc.retry_after, details=exc.details, headers=headers)

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code, message = _STATUS_CODES.get(exc.status_code, ("http_error", "Request failed"))
        return envelope(exc.status_code, code, message, headers=dict(exc.headers or {}))

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        # field PATHS only: values are never echoed (they may be content or secrets)
        fields = sorted({".".join(str(p) for p in err["loc"] if p not in ("body",)) for err in exc.errors()})
        return envelope(422, ValidationFailed.code, ValidationFailed.default_message, details={"fields": fields})

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
        log.error("unhandled_exception", exc_info=exc)
        return envelope(500, "internal_error", "Internal error")
