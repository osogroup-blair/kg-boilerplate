"""classify_error: projection failures become stable codes, and the codes become outbox categories (ADR-012 mapping)."""

from __future__ import annotations

import pytest
from kg_jobs.errors import Category
from kg_knowledge.jobs import CATEGORY_BY_CODE, category_for
from kg_knowledge.projection import classify_error
from kg_modelgw.errors import RouterError

pytestmark = pytest.mark.unit


class APIStatusError(Exception):
    def __init__(self, status_code: int, code: str | None):
        super().__init__("provider said no")
        self.status_code, self.code = status_code, code


class ServiceUnavailable(Exception):
    pass


class ClientError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (APIStatusError(429, "rate_limited"), ("router_unavailable", True)),
        (APIStatusError(429, "budget_exceeded"), ("budget_exceeded", True)),  # same HTTP status, different meaning
        (APIStatusError(403, "classification_not_permitted"), ("router_policy", False)),
        (APIStatusError(404, "model_not_found"), ("router_rejected", False)),
        (APIStatusError(503, None), ("router_unavailable", True)),  # no router code: fall back to the status
        (APIStatusError(400, None), ("router_rejected", False)),
        (RouterError("budget_exceeded", "x"), ("budget_exceeded", True)),
        (RouterError("data_policy_violation", "x"), ("router_policy", False)),
        (RouterError("invalid_request", "x"), ("router_rejected", False)),
        (RouterError("provider_unavailable", "x"), ("router_unavailable", True)),
        (ServiceUnavailable("bolt://secret-host"), ("graph_unavailable", True)),
        (ClientError("Neo.ClientError.Schema.ConstraintValidationFailed"), ("graph_conflict", True)),  # a concurrent MERGE on the same uuid
        (ClientError("Neo.ClientError.Statement.SyntaxError"), ("graph_rejected", False)),
        (RuntimeError("anything else"), ("unexpected_error", True)),
    ],
)
def test_classification(exc: Exception, expected: tuple[str, bool]):
    assert classify_error(exc) == expected


def test_the_wrapped_cause_is_found():
    try:
        try:
            raise APIStatusError(429, "budget_exceeded")
        except APIStatusError as inner:
            raise RuntimeError("graphiti wrapper") from inner
    except RuntimeError as outer:
        assert classify_error(outer) == ("budget_exceeded", True)


def test_every_code_classify_error_can_produce_has_a_job_category():
    produced = {
        classify_error(e)[0]
        for e in (
            APIStatusError(429, "budget_exceeded"),
            APIStatusError(403, "forbidden"),
            APIStatusError(404, "model_not_found"),
            ServiceUnavailable(),
            ClientError("Neo.ClientError.Schema.ConstraintValidationFailed"),
            ClientError("x"),
            RuntimeError(),
        )
    }
    assert produced <= set(CATEGORY_BY_CODE), produced - set(CATEGORY_BY_CODE)
    assert {category_for(c) for c in ("budget_exceeded",)} == {Category.BUDGET}
    assert category_for("router_policy") is Category.POLICY_TERMINAL and category_for("router_rejected") is Category.CONFIG_TERMINAL
    assert (
        category_for("graph_conflict") is Category.RETRYABLE and category_for(None) is Category.RETRYABLE
    )  # unknown codes are retried, not buried
