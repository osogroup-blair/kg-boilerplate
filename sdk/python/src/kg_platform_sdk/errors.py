"""Typed exceptions that mirror the platform's one error envelope: `{"error": {"code", "message", "request_id", "retry_after"?, "details"?}}`.

Every exception carries the stable machine-readable `code`, the HTTP status, the `request_id` (quote it when asking for support) and `details` (field names, sources, a
quota name: never request values or content). `retry_after` is set on 429 and some 503 answers. The class tells you what to do about it; the `code` tells you which case it
was.
"""

from __future__ import annotations

from typing import Any


class KGError(Exception):
    """Any non-2xx answer. Use the subclasses to branch; `code` is stable."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "error",
        status: int = 0,
        request_id: str | None = None,
        details: dict[str, Any] | None = None,
        retry_after: int | None = None,
    ):
        super().__init__(message)
        self.message, self.code, self.status, self.request_id, self.details, self.retry_after = (
            message,
            code,
            status,
            request_id,
            details or {},
            retry_after,
        )

    def __str__(self) -> str:
        rid = f" [request {self.request_id}]" if self.request_id else ""
        return f"{self.status} {self.code}: {self.message}{rid}"


class AuthError(KGError):
    """401: the key is missing, wrong, expired or revoked, or its principal was revoked. Do not retry with the same key."""


class Forbidden(KGError):
    """403: the principal's role does not allow this action."""


class NotFound(KGError):
    """404: there is no such record FOR YOU. A record in another workspace, one you hold no grant on, and one that never existed are indistinguishable by design."""


class Conflict(KGError):
    """409: the record is in a state that does not allow this (an idempotency key reused with a different request, a frozen memory, a stale revision)."""


class RuleViolation(KGError):
    """422 `rule_violation`: the ontology refused the command; `details["violations"]` names the rules (never values)."""


class InvalidEvidence(KGError):
    """422 `invalid_evidence`: a cited source does not exist or is not available to you (the two are the same answer)."""


class ValidationError(KGError):
    """422: the request is malformed; `details["fields"]` lists the offending field paths."""


class RateLimited(KGError):
    """429 `rate_limited`: too many context builds; wait `retry_after` seconds."""


class QuotaExceeded(KGError):
    """429 `quota_exceeded`: a per-principal quota is exhausted; `details["quota"]` names it and `retry_after` says when it refills."""


class ContextIncomplete(KGError):
    """503 `context_incomplete`: `allow_partial=False` and a source was unavailable; `details["unavailable_sources"]` names them."""


class ServerError(KGError):
    """5xx that is not one of the above."""


_BY_CODE: dict[str, type[KGError]] = {
    "rule_violation": RuleViolation,
    "invalid_evidence": InvalidEvidence,
    "quota_exceeded": QuotaExceeded,
    "rate_limited": RateLimited,
    "context_incomplete": ContextIncomplete,
}
_BY_STATUS: dict[int, type[KGError]] = {401: AuthError, 403: Forbidden, 404: NotFound, 409: Conflict, 422: ValidationError}


def error_from(status: int, body: Any, *, request_id: str | None, retry_after_header: str | None = None) -> KGError:
    """Builds the exception for a response. Tolerates a body that is not the envelope (a proxy's HTML error page)."""
    env: dict[str, Any] = {}
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        env = body["error"]
    code = str(env.get("code") or f"http_{status}")
    retry_after = env.get("retry_after")
    if retry_after is None and retry_after_header and retry_after_header.isdigit():
        retry_after = int(retry_after_header)
    cls = _BY_CODE.get(code) or _BY_STATUS.get(status) or (ServerError if status >= 500 else KGError)
    return cls(
        str(env.get("message") or f"HTTP {status}"),
        code=code,
        status=status,
        request_id=str(env.get("request_id") or request_id or "") or None,
        details=env.get("details") if isinstance(env.get("details"), dict) else None,
        retry_after=int(retry_after) if retry_after is not None else None,
    )
