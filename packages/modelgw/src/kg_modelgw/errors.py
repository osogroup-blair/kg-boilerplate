"""Router error codes -> platform categories (docs/router-contract.md §4). Promoted from the Phase 0 spike unchanged in behaviour."""

from __future__ import annotations

from enum import StrEnum


class Category(StrEnum):
    RETRYABLE = "retryable"
    POLICY_TERMINAL = "policy_terminal"  # stage -> skipped_by_policy, never retried
    CONFIG_TERMINAL = "config_terminal"  # operator alert, never retried
    BUDGET = "budget"  # pause queue with long backoff


CATEGORY_BY_CODE: dict[str, Category] = {
    "rate_limited": Category.RETRYABLE,
    "provider_unavailable": Category.RETRYABLE,
    "timeout": Category.RETRYABLE,
    "unknown_provider_error": Category.RETRYABLE,
    "routing_failed": Category.RETRYABLE,
    "budget_exceeded": Category.BUDGET,
    "cost_limit_exceeded": Category.BUDGET,
    "data_policy_violation": Category.POLICY_TERMINAL,
    "privacy_policy_violation": Category.POLICY_TERMINAL,
    "classification_not_permitted": Category.POLICY_TERMINAL,
    "forbidden": Category.POLICY_TERMINAL,
    "authentication_error": Category.CONFIG_TERMINAL,
    "model_not_found": Category.CONFIG_TERMINAL,
    "unsupported_capability": Category.CONFIG_TERMINAL,
    "context_too_large": Category.CONFIG_TERMINAL,
    "invalid_request": Category.CONFIG_TERMINAL,
}


def categorize(code: str) -> Category:
    # An unknown code is treated as config-terminal: retrying something we do not understand hides router changes.
    return CATEGORY_BY_CODE.get(code, Category.CONFIG_TERMINAL)


class GatewayException(Exception):
    pass


class EgressDenied(GatewayException):
    """The data class may not leave the platform. Raised BEFORE any transport call; the stage becomes skipped_by_policy."""

    def __init__(self, classification: str):
        super().__init__(f"egress denied for classification '{classification}'")
        self.classification = classification
        self.stage_state = "skipped_by_policy"


class RouterError(GatewayException):
    def __init__(self, code: str, message: str, status: int | None = None, request_id: str | None = None, retry_after: float | None = None):
        super().__init__(f"{code}: {message}")
        self.code, self.status, self.request_id, self.retry_after = code, status, request_id, retry_after
        self.category = categorize(code)
        self.attempts = 1

    @property
    def stage_state(self) -> str:
        return "skipped_by_policy" if self.category is Category.POLICY_TERMINAL else "failed"


class StructuredOutputError(GatewayException):
    """Router answered 200 but the content does not validate against the requested schema."""


class EmbeddingDimensionError(GatewayException):
    pass
