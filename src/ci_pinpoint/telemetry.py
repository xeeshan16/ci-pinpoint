"""Optional OpenTelemetry traces and metrics.

Off unless asked for, and a no-op when OpenTelemetry is not installed, so the
core has no hard dependency on it. Turn it on with ``CI_PINPOINT_OTEL=1`` after
installing the extra::

    pipx install "ci-pinpoint[otel]"
    CI_PINPOINT_OTEL=1 ci-pinpoint explain run.log --provider ollama

With ``OTEL_EXPORTER_OTLP_ENDPOINT`` set, spans and metrics go to that
collector; otherwise they are printed to stderr (handy for a quick look). What
is recorded:

* a span per stage — ``normalize``, ``select``, ``diagnose`` — under one
  ``explain`` span, each tagged with the source and carrying the stage latency;
* ``diagnose`` spans tagged with provider, model, attempts and whether a
  citation verified;
* counters for model calls, input/output tokens and estimated cost, and a
  histogram of stage latency.

Log text is never put on a span: attributes are metadata (source, model,
counts), never log lines.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ci_pinpoint.models import Brief

log = logging.getLogger(__name__)

_SERVICE = "ci-pinpoint"


class Telemetry:
    """Base no-op. Every method is safe to call whether or not OTel is present."""

    enabled = False

    @contextmanager
    def stage(self, name: str, **attributes: Any) -> Iterator[None]:
        yield

    def record_brief(self, brief: Brief) -> None:
        pass

    def shutdown(self) -> None:
        pass


class _OtelTelemetry(Telemetry):
    enabled = True

    def __init__(self, tracer: Any, meter: Any, providers: tuple[Any, Any]) -> None:
        self._tracer = tracer
        self._providers = providers
        self._calls = meter.create_counter(
            "ci_pinpoint.model.calls", description="chat model completions"
        )
        self._tokens = meter.create_counter(
            "ci_pinpoint.model.tokens", unit="token", description="chat model tokens"
        )
        self._cost = meter.create_counter(
            "ci_pinpoint.model.cost_usd", unit="USD", description="estimated model cost"
        )
        self._diagnoses = meter.create_counter(
            "ci_pinpoint.diagnoses", description="diagnoses, by whether a citation verified"
        )
        self._stage_ms = meter.create_histogram(
            "ci_pinpoint.stage.duration", unit="ms", description="stage latency"
        )

    @contextmanager
    def stage(self, name: str, **attributes: Any) -> Iterator[None]:
        clean = {k: v for k, v in attributes.items() if v is not None}
        started = time.perf_counter()
        with self._tracer.start_as_current_span(name) as span:
            for key, value in clean.items():
                span.set_attribute(key, value)
            try:
                yield
            finally:
                elapsed_ms = (time.perf_counter() - started) * 1000
                span.set_attribute("duration_ms", round(elapsed_ms, 2))
                self._stage_ms.record(elapsed_ms, {"stage": name})

    def record_brief(self, brief: Brief) -> None:
        acc = brief.accounting
        if acc is None:
            return
        labels = {"provider": acc.provider, "model": acc.model}
        if brief.diagnosis is not None:
            self._calls.add(brief.diagnosis.attempts, labels)
            self._diagnoses.add(1, {**labels, "verified": brief.diagnosis.verified})
        self._tokens.add(acc.input_tokens, {**labels, "direction": "input"})
        self._tokens.add(acc.output_tokens, {**labels, "direction": "output"})
        if acc.cost_usd:
            self._cost.add(acc.cost_usd, labels)

    def shutdown(self) -> None:
        for provider in self._providers:
            try:
                provider.shutdown()
            except Exception as exc:  # pragma: no cover - best-effort flush
                log.debug("otel shutdown failed", extra={"error": str(exc)})


_NULL = Telemetry()
_active: Telemetry = _NULL


def get_telemetry() -> Telemetry:
    return _active


def _wants_otel(env: Mapping[str, str]) -> bool:
    return env.get("CI_PINPOINT_OTEL", "").strip().lower() in ("1", "true", "yes", "on")


def configure(env: Mapping[str, str]) -> Telemetry:
    """Set up telemetry from the environment, once. Returns the active instance.

    No-ops (and warns once) if the extra is not installed. Safe to call again.
    """
    global _active
    if _active.enabled or not _wants_otel(env):
        return _active
    try:
        built = _build(env)
    except ImportError:
        log.warning(
            "CI_PINPOINT_OTEL is set but OpenTelemetry is not installed; "
            'install the extra: pipx install "ci-pinpoint[otel]"'
        )
        return _active
    _active = built
    return _active


def _build(env: Mapping[str, str]) -> Telemetry:
    from opentelemetry import metrics, trace
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import (
        ConsoleMetricExporter,
        PeriodicExportingMetricReader,
    )
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

    resource = Resource.create({"service.name": _SERVICE})
    endpoint = env.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if endpoint:
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
            OTLPMetricExporter,
        )
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )

        span_exporter: Any = OTLPSpanExporter()
        metric_exporter: Any = OTLPMetricExporter()
    else:
        span_exporter = ConsoleSpanExporter()
        metric_exporter = ConsoleMetricExporter()

    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(BatchSpanProcessor(span_exporter))
    reader = PeriodicExportingMetricReader(metric_exporter)
    meter_provider = MeterProvider(resource=resource, metric_readers=[reader])
    trace.set_tracer_provider(tracer_provider)
    metrics.set_meter_provider(meter_provider)
    log.info(
        "OpenTelemetry enabled",
        extra={"exporter": "otlp" if endpoint else "console"},
    )
    return _OtelTelemetry(
        trace.get_tracer(_SERVICE),
        metrics.get_meter(_SERVICE),
        (tracer_provider, meter_provider),
    )


def reset_for_tests() -> None:
    """Drop the active telemetry so a test can configure its own."""
    global _active
    _active = _NULL
