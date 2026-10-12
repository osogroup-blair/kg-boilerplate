"""Extraction error classification (P4 §5.1). A model that answers with something Graphiti cannot parse is QUARANTINED, not retried forever (ING-03):
one retry (a flaky answer), then a dead letter with a stable code, and nothing in the ontology, the policy or the graph changes."""

from __future__ import annotations

from kg_jobs.errors import Category, JobError
from kg_knowledge.projection import classify_error

UNPARSEABLE = "extraction_unparseable"
_PARSE_ERROR_NAMES = frozenset(
    {"JSONDecodeError", "ValidationError", "KeyError", "UnicodeDecodeError", "InvalidStructuredOutput", "IndexError"}
)
MAX_UNPARSEABLE_ATTEMPTS = 2


def _chain(exc: BaseException) -> list[BaseException]:
    out: list[BaseException] = []
    cur: BaseException | None = exc
    while cur is not None and len(out) < 6:
        out.append(cur)
        cur = cur.__cause__ or cur.__context__
    return out


def is_unparseable(exc: BaseException) -> bool:
    return any({c.__name__ for c in type(e).__mro__} & _PARSE_ERROR_NAMES for e in _chain(exc))


def job_error_for(exc: BaseException, *, attempt: int) -> JobError:
    """Maps an exception from the extraction step to the outbox category. Names only: an exception text can echo content, so it is never kept."""
    from kg_knowledge.jobs import category_for

    if isinstance(exc, TimeoutError):
        return JobError(Category.RETRYABLE, "extraction_timeout")
    if is_unparseable(exc):
        category = Category.RETRYABLE if attempt < MAX_UNPARSEABLE_ATTEMPTS else Category.CONFIG_TERMINAL
        return JobError(category, UNPARSEABLE)
    code, _ = classify_error(exc)
    return JobError(category_for(code), code)
