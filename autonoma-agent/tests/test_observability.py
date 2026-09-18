"""Logs JSON con trace_id, redacción de secretos, métricas y temporizadores."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from autonoma.errors import ConfigurationError, ErrorCode, FileSystemError
from autonoma.observability import (
    JsonLogFormatter,
    MetricsRegistry,
    OutcomeStatus,
    configure_logging,
    current_trace_id,
    log_event,
    measure,
    runtime_metadata,
    trace_scope,
)


@pytest.fixture
def captured(caplog: pytest.LogCaptureFixture):
    caplog.set_level(logging.DEBUG)
    return caplog


def test_trace_scope_generates_and_restores() -> None:
    assert current_trace_id() == ""
    with trace_scope() as first:
        assert first.startswith("trc-")
        assert current_trace_id() == first
        with trace_scope("trc-fijo") as second:
            assert second == "trc-fijo"
        assert current_trace_id() == first
    assert current_trace_id() == ""


def test_trace_scope_restores_on_exception() -> None:
    with pytest.raises(RuntimeError):
        with trace_scope():
            raise RuntimeError("boom")
    assert current_trace_id() == ""


def test_json_formatter_emits_flat_structured_record(captured: pytest.LogCaptureFixture) -> None:
    formatter = JsonLogFormatter(version="test")
    logger = logging.getLogger("autonoma.test.json")
    with trace_scope("trc-aaaaaaaa"):
        with captured.at_level(logging.INFO, logger="autonoma.test.json"):
            log_event(logger, logging.INFO, "tool.write_file", {"tool": "write_file", "path": "/tmp/a"})
    record = captured.records[-1]
    payload = json.loads(formatter.format(record))
    assert payload["trace_id"] == "trc-aaaaaaaa"
    assert payload["event"] == "tool.write_file"
    assert payload["tool"] == "write_file"
    assert payload["path"] == "/tmp/a"
    assert payload["version"] == "test"
    assert payload["level"] == "INFO"


def test_reserved_record_keys_are_prefixed_not_dropped(captured: pytest.LogCaptureFixture) -> None:
    # Un campo llamado `msg` o `args` chocaría con el LogRecord: se reubica, no se pierde.
    formatter = JsonLogFormatter()
    with captured.at_level(logging.INFO):
        log_event(logging.getLogger("autonoma.test"), logging.INFO, "weird", {"msg": "hola", "args": "x", "name": "n"})
    payload = json.loads(formatter.format(captured.records[-1]))
    assert payload["x_msg"] == "hola" and payload["x_args"] == "x" and payload["x_name"] == "n"
    assert payload["event"] == "weird"


def test_configure_logging_writes_json_and_redacts(tmp_path: Path) -> None:
    secret = "sk-muy-secreto-9999"
    runtime = configure_logging(log_dir=tmp_path, level=logging.DEBUG, secret_values=[secret], version="x")
    try:
        with trace_scope():
            log_event(logging.getLogger("autonoma.conf"), logging.WARNING, "config.read", {"path": f"/x/{secret}"})
    finally:
        runtime.close()
    lines = (tmp_path / "autonoma.log").read_text(encoding="utf-8").strip().splitlines()
    assert lines, "el log JSON debe existir"
    payload = json.loads(lines[-1])
    assert payload["event"] == "config.read"
    assert secret not in lines[-1]
    assert payload["trace_id"].startswith("trc-")
    ours = set(id(handler) for handler in runtime.handlers)
    assert ours.isdisjoint({id(handler) for handler in logging.getLogger().handlers})  # sin handlers huérfanos


def test_configure_logging_can_run_without_directory() -> None:
    runtime = configure_logging(log_dir=None, level=logging.WARNING, console=True)
    assert runtime.log_file is None
    assert runtime.handler_count == 1
    runtime.close()


def test_metrics_counters_and_duration_summaries() -> None:
    metrics = MetricsRegistry()
    metrics.increment("turn.started")
    metrics.increment("turn.started", by=2)
    for sample in (10.0, 20.0, 30.0, 40.0):
        metrics.observe_duration("tool.read_file", sample)
    counters = metrics.counters()
    assert counters["turn.started"] == 3
    with pytest.raises(TypeError):
        counters["otro"] = 1  # type: ignore[index]
    summary = metrics.summary("tool.read_file")
    assert summary is not None and summary.count == 4
    assert summary.mean_ms == 25.0 and summary.max_ms == 40.0
    assert 10.0 <= summary.p50_ms <= 40.0
    snapshot = metrics.snapshot()
    assert set(snapshot) == {"counters", "durations_ms"}
    metrics.reset()
    assert metrics.summary("tool.read_file") is None


def test_duration_samples_are_bounded() -> None:
    metrics = MetricsRegistry()
    for index in range(2500):
        metrics.observe_duration("noisy", float(index))
    summary = metrics.summary("noisy")
    assert summary is not None and summary.count == 2048  # cola acotada, sin crecimiento libre


def test_measure_records_success_and_reraises_typed_failure() -> None:
    metrics = MetricsRegistry()
    with measure(metrics, "tool.x", fields={"tool": "x"}) as outcome:
        value = 1
    assert outcome.ok and value == 1
    with pytest.raises(FileSystemError):
        with measure(metrics, "tool.x", fields={"tool": "x"}) as failed:
            raise FileSystemError("no se pudo")
    assert failed.status is OutcomeStatus.ERROR and failed.error_code is ErrorCode.FILESYSTEM_IO
    counters = metrics.counters()
    assert counters["tool.x.success"] == 1
    assert counters["tool.x.failure.filesystem_io"] == 1
    assert metrics.summary("tool.x") is not None


def test_measure_does_not_swallow_cancellation() -> None:
    metrics = MetricsRegistry()
    with pytest.raises(KeyboardInterrupt):
        with measure(metrics, "turn"):
            raise KeyboardInterrupt
    assert metrics.counters().get("turn.failure.internal") == 1


def test_runtime_metadata_reports_frozen_and_python() -> None:
    metadata = runtime_metadata()
    assert {"app", "version", "python", "platform", "frozen", "pid"} <= set(metadata)
    assert metadata["frozen"] is False


def test_log_event_skips_disabled_levels() -> None:
    """Si el nivel está deshabilitado no se construyen campos ni se gasta CPU."""
    logger = logging.getLogger("autonoma.test.quiet")
    seen: list[str] = []

    class _Spy(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            seen.append(record.getMessage())

    spy = _Spy(level=logging.NOTSET)
    previous_level = logger.level
    previous_propagate = logger.propagate
    logger.addHandler(spy)
    logger.setLevel(logging.ERROR)
    logger.propagate = False
    try:
        log_event(logger, logging.DEBUG, "no.debe.salir", {"costly": object()})
        log_event(logger, logging.ERROR, "si.debe.salir", {})
    finally:
        logger.removeHandler(spy)
        logger.setLevel(previous_level)
        logger.propagate = previous_propagate
    assert seen == ["si.debe.salir"]


def test_exception_details_are_redacted_in_formatter(captured: pytest.LogCaptureFixture) -> None:
    formatter = JsonLogFormatter()
    try:
        raise ConfigurationError("clave sk-larga-123456 filtrada")
    except ConfigurationError as exc:
        with captured.at_level(logging.ERROR):
            log_event(logging.getLogger("autonoma.test.exc"), logging.ERROR, "boom", {"error": str(exc)}, exc_info=exc)
    payload = json.loads(formatter.format(captured.records[-1]))
    assert payload["event"] == "boom"
    assert "ConfigurationError" in payload["exception"]
