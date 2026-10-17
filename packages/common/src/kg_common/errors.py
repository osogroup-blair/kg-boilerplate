"""Platform errors. Each carries the stable machine-readable code and HTTP status used in the error envelope (never content)."""

from __future__ import annotations

from typing import Any


class PlatformError(Exception):
    code = "internal_error"
    http_status = 500
    default_message = "Internal error"

    def __init__(
        self,
        message: str | None = None,
        *,
        code: str | None = None,
        http_status: int | None = None,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
        retry_after: int | None = None,
    ):
        super().__init__(message or self.default_message)
        self.message = message or self.default_message
        if code is not None:
            self.code = code
        if http_status is not None:
            self.http_status = http_status
        self.retryable = retryable
        self.details = details
        self.retry_after = retry_after


class NotFound(PlatformError):
    code, http_status, default_message = "not_found", 404, "Resource not found"


class Unauthenticated(PlatformError):
    code, http_status, default_message = "unauthenticated", 401, "Authentication required"


class Forbidden(PlatformError):
    code, http_status, default_message = "forbidden", 403, "Action not permitted"


class Conflict(PlatformError):
    code, http_status, default_message = "conflict", 409, "Conflict"


class ValidationFailed(PlatformError):
    code, http_status, default_message = "validation_failed", 422, "Request validation failed"


class PayloadTooLarge(PlatformError):
    code, http_status, default_message = "payload_too_large", 413, "Request body too large"


class DependencyUnavailable(PlatformError):
    code, http_status, default_message = "dependency_unavailable", 503, "A required dependency is unavailable"

    def __init__(self, message: str | None = None, **kw: Any):
        kw.setdefault("retryable", True)
        super().__init__(message, **kw)


class PreconditionFailed(PlatformError):
    code, http_status, default_message = "precondition_failed", 412, "The resource changed since you read it"


class PreconditionRequired(PlatformError):
    code, http_status, default_message = "precondition_required", 428, "If-Match is required"


class ImmutableVersion(Conflict):
    code, default_message = "immutable_version", "A published ontology version is immutable"


class NoActiveOntology(Conflict):
    code, default_message = "no_active_ontology", "This workspace has no active ontology"


class IdempotencyMismatch(Conflict):
    code, default_message = "idempotency_mismatch", "Idempotency-Key was already used with a different request"


class SubjectSuppressed(Conflict):
    """The identifier of a subject who asked to be forgotten (P8, ADR-045): it is not recorded again until an admin lifts the suppression."""

    code, default_message = "subject_suppressed", "This identifier was deleted at a subject's request and cannot be recorded again"


class IdempotencyKeyRequired(PlatformError):
    code, http_status, default_message = "idempotency_key_required", 400, "Idempotency-Key header is required"


class UnknownEntity(PlatformError):
    """An endpoint of a command does not resolve in this workspace. The same answer whether it never existed or lives in another workspace."""

    code, http_status, default_message = "unknown_entity", 422, "A referenced entity was not found"


class RuleViolation(PlatformError):
    """details.violations is a list of {rule_kind, rule_id, code, path}: never attribute values."""

    code, http_status, default_message = "rule_violation", 422, "The command violates an ontology rule"


class OntologyInvalid(PlatformError):
    code, http_status, default_message = "ontology_invalid", 422, "The ontology has validation errors"
