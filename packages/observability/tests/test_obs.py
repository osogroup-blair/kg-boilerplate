import json
import re

import pytest
from kg_obs import context, metrics
from kg_obs.logging import configure_logging, get_logger
from kg_obs.redaction import redact, redact_attributes

pytestmark = pytest.mark.unit


def _log_lines(capsys) -> list[dict]:
    return [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")]


def test_redaction_drops_denied_keys_at_any_depth_and_truncates():
    out = redact(
        {
            "ok": 1,
            "content": "secret",
            "nested": {"Messages": [{"role": "user", "content": "x"}], "deep": [{"api_key": "k", "n": 2}]},
            "long": "a" * 1000,
        }
    )
    assert out["content"] == "[redacted]" and out["nested"]["Messages"] == "[redacted]"
    assert out["nested"]["deep"][0] == {"api_key": "[redacted]", "n": 2}
    assert out["long"].endswith("[truncated]") and len(out["long"]) < 300 and out["ok"] == 1


def test_logger_never_emits_sentinel_content(capsys):
    configure_logging("INFO", service="kg-test")
    context.bind(request_id="rid-1")
    get_logger().info("evt", details={"content": "SENTINEL-CONTENT-123", "n": 1}, prompt="SENTINEL-PROMPT", count=3)
    out = capsys.readouterr().out
    assert "SENTINEL" not in out
    rec = json.loads(out.splitlines()[0])
    assert (
        rec["count"] == 3
        and rec["request_id"] == "rid-1"
        and rec["service"] == "kg-test"
        and rec["details"] == {"content": "[redacted]", "n": 1}
    )
    context.clear()


def test_exception_messages_are_not_logged(capsys):
    configure_logging("INFO")
    try:
        raise ValueError("input was SENTINEL-IN-EXC")
    except ValueError:
        get_logger().error("boom", exc_info=True)
    out = capsys.readouterr().out
    rec = json.loads(out.splitlines()[0])
    assert "SENTINEL" not in out and rec["error_type"] == "ValueError" and rec["frames"]


def test_workspace_hash_is_not_the_id():
    h = context.hmac_hash("11111111-1111-1111-1111-111111111111", "secret")
    assert re.fullmatch(r"[0-9a-f]{16}", h)
    assert (
        h
        == context.hmac_hash("11111111-1111-1111-1111-111111111111", "secret")
        != context.hmac_hash("11111111-1111-1111-1111-111111111111", "other")
    )


def test_metric_labels_carry_no_identifiers():
    forbidden = re.compile(r"workspace|principal|_id$|^id$|user|session|memory|entity")
    for family in metrics.REGISTRY.collect():
        for sample in family.samples:
            assert not any(forbidden.search(label) for label in sample.labels), (sample.name, sample.labels)
    # also inspect declared label names for metrics with no samples yet
    for name in ("http_requests_total", "auth_failures_total", "authz_denials_total", "router_calls_total", "dependency_up"):
        collector = metrics.REGISTRY._names_to_collectors.get(name)  # noqa: SLF001
        assert collector is not None and not any(forbidden.search(label) for label in collector._labelnames)  # noqa: SLF001


def test_span_attributes_are_redacted():
    from kg_obs.tracing import setup_tracing
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    mem = InMemorySpanExporter()
    provider = setup_tracing("t", exporter=mem)
    with provider.get_tracer("t").start_as_current_span("s") as sp:
        sp.set_attribute("content", "SENTINEL-SPAN")
        sp.set_attribute("count", 2)
    provider.force_flush()
    spans = mem.get_finished_spans()
    assert spans and dict(spans[0].attributes) == {"count": 2}
    assert redact_attributes({"content": "x", "count": 2}) == {"count": 2}


async def test_service_name_reaches_log_lines_emitted_from_other_tasks(capsys):
    """Regression: the access log said service=kg because the name lived in a ContextVar set inside the lifespan task."""
    import asyncio

    configure_logging("INFO", service="kg-api")
    await asyncio.get_running_loop().create_task(asyncio.sleep(0, result=get_logger().info("from_another_task")))
    assert json.loads(capsys.readouterr().out.splitlines()[0])["service"] == "kg-api"


def test_debug_logging_never_enables_content_bearing_library_loggers():
    import logging

    from kg_obs.logging import CONTENT_LOGGERS, configure_logging

    configure_logging("DEBUG", service="test", json_output=True)
    try:
        assert logging.getLogger().level == logging.DEBUG
        for name in CONTENT_LOGGERS:
            assert logging.getLogger(name).getEffectiveLevel() >= logging.WARNING, (
                name
            )  # the Neo4j driver would log Cypher parameters at DEBUG
    finally:
        configure_logging("WARNING", service="test", json_output=True)
