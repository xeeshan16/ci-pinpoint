from __future__ import annotations

import gzip
from collections.abc import Iterator
from pathlib import Path

import pytest
from opentelemetry import trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from ci_pinpoint import telemetry
from ci_pinpoint.config import Settings
from ci_pinpoint.models import RawLog
from ci_pinpoint.pipeline import explain
from ci_pinpoint.providers.fake import FakeChat

FIXTURES = Path(__file__).parent / "fixtures" / "logs"


def _raw(name: str) -> RawLog:
    text = gzip.decompress((FIXTURES / name).read_bytes()).decode("utf-8")
    return RawLog(text=text, source=name, job_name="job", failed_steps=("Run tests",))


@pytest.fixture
def otel() -> Iterator[tuple[InMemorySpanExporter, InMemoryMetricReader]]:
    spans = InMemorySpanExporter()
    tracer_provider = TracerProvider()
    tracer_provider.add_span_processor(SimpleSpanProcessor(spans))
    reader = InMemoryMetricReader()
    meter_provider = MeterProvider(metric_readers=[reader])
    active = telemetry._OtelTelemetry(
        trace.get_tracer("test", tracer_provider=tracer_provider),
        meter_provider.get_meter("test"),
        (tracer_provider, meter_provider),
    )
    telemetry._active = active
    try:
        yield spans, reader
    finally:
        telemetry.reset_for_tests()


def test_disabled_by_default() -> None:
    telemetry.reset_for_tests()
    assert telemetry.get_telemetry().enabled is False
    # The no-op stage and record are safe to call.
    with telemetry.get_telemetry().stage("x", a=1):
        pass


def test_configure_noop_without_request() -> None:
    telemetry.reset_for_tests()
    assert telemetry.configure({}).enabled is False


def test_explain_emits_stage_spans_and_token_metrics(
    otel: tuple[InMemorySpanExporter, InMemoryMetricReader],
) -> None:
    spans, reader = otel
    brief = explain(_raw("postgres-connection-refused.log.gz"), Settings(), FakeChat(), env={})
    assert brief.accounting is not None

    names = {s.name for s in spans.get_finished_spans()}
    assert {"normalize", "select", "diagnose"} <= names
    diagnose = next(s for s in spans.get_finished_spans() if s.name == "diagnose")
    assert diagnose.attributes is not None
    assert diagnose.attributes["provider"] == "fake"
    assert "duration_ms" in diagnose.attributes

    metrics = reader.get_metrics_data()
    assert metrics is not None
    points = {
        metric.name
        for rm in metrics.resource_metrics
        for sm in rm.scope_metrics
        for metric in sm.metrics
    }
    assert "ci_pinpoint.model.tokens" in points
    assert "ci_pinpoint.diagnoses" in points


def test_no_model_records_no_crash(
    otel: tuple[InMemorySpanExporter, InMemoryMetricReader],
) -> None:
    spans, _ = otel
    explain(_raw("format-check-diff.log.gz"), Settings(), None, env={})
    assert {"normalize", "select"} <= {s.name for s in spans.get_finished_spans()}
