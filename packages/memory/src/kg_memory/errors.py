"""Error classification for the memory jobs. Names only: an exception's text can echo content, so it is never stored, logged or returned.

A model that answers with something the schema rejects is QUARANTINED, not retried forever (ING-03): one retry (a flaky answer), then a dead letter with a stable
code, and nothing in the memory store changes."""

from __future__ import annotations

from kg_jobs.errors import Category, JobError
from kg_knowledge.jobs import category_for
from kg_knowledge.projection import classify_error

UNPARSEABLE = "memory_extraction_unparseable"
_PARSE_ERROR_NAMES = frozenset(
    {"JSONDecodeError", "ValidationError", "StructuredOutputError", "InvalidStructuredOutput", "KeyError", "UnicodeDecodeError"}
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
    if isinstance(exc, TimeoutError):
        return JobError(Category.RETRYABLE, "extraction_timeout")
    if is_unparseable(exc):
        return JobError(Category.RETRYABLE if attempt < MAX_UNPARSEABLE_ATTEMPTS else Category.CONFIG_TERMINAL, UNPARSEABLE)
    code, _ = classify_error(exc)
    return JobError(category_for(code), code)
