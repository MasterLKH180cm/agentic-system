"""Optional OpenTelemetry with bounded, content-free attributes."""
from contextlib import contextmanager
from functools import lru_cache
import os
import time


SAFE_ATTRIBUTES = {"run.id", "step.id", "tool.name", "model.route", "model.provider",
                   "http.method", "http.route", "http.status_code", "outcome", "token.count"}


class Telemetry:
    def __init__(self):
        self.enabled = os.getenv("AGENT_TELEMETRY_ENABLED", "0") == "1"
        self.provider = None
        if not self.enabled:
            return
        endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "")
        if not endpoint:
            raise ValueError("Enabled telemetry requires OTEL_EXPORTER_OTLP_ENDPOINT")
        try:
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
            from opentelemetry.sdk.metrics import MeterProvider
            from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
            from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
        except ImportError as exc:
            raise ValueError("Install the telemetry extra to enable OpenTelemetry") from exc
        resource = Resource.create({"service.name": os.getenv("OTEL_SERVICE_NAME", "agentic-system")})
        self.provider = TracerProvider(resource=resource)
        self.provider.add_span_processor(BatchSpanProcessor(
            OTLPSpanExporter(endpoint=endpoint.rstrip("/") + "/v1/traces", timeout=3),
            schedule_delay_millis=1000,
        ))
        self.tracer = self.provider.get_tracer("agentic.runtime", "0.2.0")
        reader = PeriodicExportingMetricReader(
            OTLPMetricExporter(endpoint=endpoint.rstrip("/") + "/v1/metrics", timeout=3),
            export_interval_millis=1000,
        )
        self.meter_provider = MeterProvider(resource=resource, metric_readers=[reader])
        meter = self.meter_provider.get_meter("agentic.runtime", "0.2.0")
        self.calls = meter.create_counter("agentic.operations", unit="1")
        self.latency = meter.create_histogram("agentic.operation.duration", unit="s")

    @contextmanager
    def span(self, name: str, attributes: dict | None = None, context=None):
        if not self.enabled:
            yield None
            return
        from opentelemetry.trace import Status, StatusCode
        attrs = {k: v for k, v in (attributes or {}).items()
                 if k in SAFE_ATTRIBUTES and isinstance(v, (str, int, float, bool))}
        started = time.monotonic()
        with self.tracer.start_as_current_span(name, attributes=attrs, context=context,
                                              record_exception=False, set_status_on_exception=False) as active:
            outcome = "success"
            try:
                yield active
            except BaseException as exc:
                outcome = "error"
                active.set_status(Status(StatusCode.ERROR))
                active.add_event("error", {"error.type": type(exc).__name__})
                raise
            finally:
                # Run/step identifiers stay in traces, never high-cardinality metric labels.
                labels = {"operation": name, "outcome": outcome}
                self.calls.add(1, labels)
                self.latency.record(time.monotonic() - started, labels)

    def flush(self):
        if self.enabled:
            self.provider.force_flush(timeout_millis=5000)
            self.meter_provider.force_flush(timeout_millis=5000)

    def shutdown(self):
        if self.enabled:
            self.provider.shutdown()
            self.meter_provider.shutdown()


@lru_cache(maxsize=1)
def get_telemetry() -> Telemetry:
    return Telemetry()


def span(name: str, attributes: dict | None = None):
    return get_telemetry().span(name, attributes)


def install_telemetry(app):
    telemetry = get_telemetry()
    app.state.telemetry = telemetry

    @app.middleware("http")
    async def instrument(request, call_next):
        context = None
        if telemetry.enabled:
            from opentelemetry.propagate import extract
            context = extract(dict(request.headers))
        with telemetry.span("http.request", {"http.method": request.method}, context=context) as active:
            response = await call_next(request)
            if active is not None:
                route = request.scope.get("route")
                active.set_attribute("http.route", getattr(route, "path", "unmatched"))
                active.set_attribute("http.status_code", response.status_code)
            return response
