"""OpenTelemetry setup. The OTLP exporter is on only when OTEL_EXPORTER_OTLP_ENDPOINT is set; span attributes pass the same deny-list."""

from __future__ import annotations

from collections.abc import Sequence

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter, SpanExportResult

from kg_obs.redaction import redact_attributes


class RedactingExporter(SpanExporter):
    def __init__(self, inner: SpanExporter):
        self.inner = inner

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        clean = [
            ReadableSpan(
                name=s.name,
                context=s.context,
                parent=s.parent,
                resource=s.resource,
                attributes=redact_attributes(dict(s.attributes or {})),
                events=(),
                links=s.links,
                kind=s.kind,
                status=s.status,
                start_time=s.start_time,
                end_time=s.end_time,
                instrumentation_scope=s.instrumentation_scope,
            )
            for s in spans
        ]
        return self.inner.export(clean)

    def shutdown(self) -> None:
        self.inner.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self.inner.force_flush(timeout_millis)


def setup_tracing(service_name: str, otlp_endpoint: str = "", *, exporter: SpanExporter | None = None) -> TracerProvider:
    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    if exporter is not None:
        provider.add_span_processor(BatchSpanProcessor(RedactingExporter(exporter)))
    elif otlp_endpoint:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        provider.add_span_processor(BatchSpanProcessor(RedactingExporter(OTLPSpanExporter(endpoint=otlp_endpoint))))
    trace.set_tracer_provider(provider)
    return provider


def tracer(name: str = "kg") -> trace.Tracer:
    return trace.get_tracer(name)
