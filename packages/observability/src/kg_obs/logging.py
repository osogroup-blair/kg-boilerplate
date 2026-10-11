from __future__ import annotations

import logging
import sys
import traceback
from collections.abc import MutableMapping
from typing import Any

import structlog

from kg_obs import context
from kg_obs.redaction import redact


def _add_context(_: Any, __: str, event: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
    for k, v in context.current().items():
        event.setdefault(k, v)
    return event


def _safe_exception(_: Any, __: str, event: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
    """Log the exception type and frames only: exception messages can echo request content (e.g. validation errors)."""
    exc = event.pop("exc_info", None)
    if exc:
        if exc is True:
            exc = sys.exc_info()[1]
        elif isinstance(exc, tuple):
            exc = exc[1]
        if isinstance(exc, BaseException):
            event["error_type"] = type(exc).__name__
            frames = traceback.extract_tb(exc.__traceback__)[-8:]
            event["frames"] = [f"{f.filename.rsplit('/', 1)[-1]}:{f.lineno} in {f.name}" for f in frames]
    return event


def _redact(_: Any, __: str, event: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
    return dict(redact(event))


# Libraries whose DEBUG records carry content: the Neo4j driver logs every Cypher statement WITH its parameters (names, fact text, attribute values),
# httpx/openai log request details. They stay at WARNING even when LOG_LEVEL=DEBUG, so turning on debug logs can never put data in the logs.
CONTENT_LOGGERS = ("neo4j", "neo4j.io", "neo4j.pool", "graphiti_core", "httpx", "httpcore", "openai", "sqlalchemy.engine", "psycopg")


def configure_logging(level: str = "INFO", *, service: str = "kg", json_output: bool = True) -> None:
    context.set_service(service)
    renderer: Any = structlog.processors.JSONRenderer() if json_output else structlog.dev.ConsoleRenderer()
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _add_context,
            _safe_exception,
            _redact,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level.upper())),
        logger_factory=structlog.PrintLoggerFactory(),  # resolves sys.stdout per logger, so a swapped stdout (tests, redirects) is honoured
        cache_logger_on_first_use=False,
    )
    # Route stdlib loggers (uvicorn, sqlalchemy) through the same filter level; their records carry no request content at INFO.
    logging.basicConfig(level=level.upper(), stream=sys.stdout, format="%(message)s", force=True)
    logging.getLogger("uvicorn.access").disabled = True  # the API writes its own metadata-only access log
    for name in CONTENT_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


def get_logger(name: str = "kg") -> Any:
    return structlog.get_logger(name)
