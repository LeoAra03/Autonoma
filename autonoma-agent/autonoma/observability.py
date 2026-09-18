"""Observabilidad: `trace_id` por turno, logs JSON rotativos y métricas del proceso.

Lo que se emite aquí es seguro por construcción: cada valor pasa por
`errors.redact` (longitud acotada y secretos sustituidos) antes de llegar al
handler. No se añaden dependencias externas para que el ejecutable siga siendo
portable y auditable con una sola herramienta.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import platform
import secrets
import sys
import threading
import time
from collections import Counter, deque
from collections.abc import Iterator, Mapping, MutableMapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from logging.handlers import RotatingFileHandler
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final

from autonoma.errors import AutonomaError, ErrorCode, redact

__all__ = [
    "DurationSummary",
    "JsonLogFormatter",
    "LoggingRuntime",
    "MetricOutcome",
    "MetricsRegistry",
    "OutcomeStatus",
    "PlainLogFormatter",
    "configure_logging",
    "current_trace_id",
    "log_event",
    "measure",
    "new_trace_id",
    "runtime_metadata",
    "trace_scope",
]

_RESERVED_RECORD_KEYS: Final[frozenset[str]] = frozenset(
    {
        "args", "asctime", "created", "exc_info", "exc_text", "filename", "funcName",
        "levelname", "levelno", "lineno", "message", "module", "msecs", "msg", "name",
        "pathname", "process", "processName", "relativeCreated", "stack_info", "taskName",
        "thread", "threadName",
    }
)
_MAX_FIELD_CHARS: Final[int] = 400
_SAMPLES_MAX: Final[int] = 2048
_DEFAULT_LOG_NAME: Final[str] = "autonoma.log"
_MIN_SECRET_SCAN_LENGTH: Final[int] = 6

_trace_id_var: ContextVar[str] = ContextVar("autonoma_trace_id", default="")


# --------------------------------------------------------------------- trazas
def new_trace_id() -> str:
    """Identificador corto y correlacionable para un turno o una operación."""
    return f"trc-{secrets.token_hex(6)}"


def current_trace_id() -> str:
    return _trace_id_var.get()


@contextlib.contextmanager
def trace_scope(trace_id: str | None = None) -> Iterator[str]:
    """Contexto dinámico; los hilos creados dentro heredan el `trace_id`."""
    token = _trace_id_var.set(trace_id or new_trace_id())
    try:
        yield _trace_id_var.get()
    finally:
        _trace_id_var.reset(token)


# --------------------------------------------------------------------- desenlace
class OutcomeStatus(str, Enum):
    """Estado final medido por `measure()`; evita inferirlo de cadenas."""

    PENDING = "pending"
    OK = "ok"
    ERROR = "error"


class MetricOutcome:
    """Superficie mutable mínima de `measure()`: un solo registro de resultado."""

    __slots__ = ("_code", "_status")

    def __init__(self) -> None:
        self._status: OutcomeStatus = OutcomeStatus.PENDING
        self._code: ErrorCode | None = None

    @property
    def status(self) -> OutcomeStatus:
        return self._status

    @property
    def error_code(self) -> ErrorCode | None:
        return self._code

    @property
    def ok(self) -> bool:
        return self._status is OutcomeStatus.OK

    def succeed(self) -> None:
        self._status = OutcomeStatus.OK

    def fail(self, code: ErrorCode) -> None:
        self._status = OutcomeStatus.ERROR
        self._code = code


# --------------------------------------------------------------------- métricas
@dataclass(frozen=True, slots=True)
class DurationSummary:
    """Resumen acotado de latencias locales; medición local, no benchmark."""

    count: int
    mean_ms: float
    p50_ms: float
    p95_ms: float
    max_ms: float
    total_ms: float

    def as_dict(self) -> dict[str, float | int]:
        return {
            "count": self.count,
            "mean_ms": round(self.mean_ms, 2),
            "p50_ms": round(self.p50_ms, 2),
            "p95_ms": round(self.p95_ms, 2),
            "max_ms": round(self.max_ms, 2),
            "total_ms": round(self.total_ms, 2),
        }


def _percentile(ordered: Sequence[float], fraction: float) -> float:
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, max(0, round(fraction * (len(ordered) - 1))))
    return ordered[index]


class MetricsRegistry:
    """Contadores y latencias en memoria, acotados y protegidos por un lock."""

    __slots__ = ("_counters", "_lock", "_samples")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: MutableMapping[str, int] = Counter()
        self._samples: dict[str, deque[float]] = {}

    def increment(self, name: str, *, by: int = 1) -> None:
        with self._lock:
            self._counters[name] += by

    def observe_duration(self, name: str, elapsed_ms: float) -> None:
        sample = max(0.0, float(elapsed_ms))
        with self._lock:
            bucket = self._samples.get(name)
            if bucket is None:
                bucket = deque(maxlen=_SAMPLES_MAX)
                self._samples[name] = bucket
            bucket.append(sample)

    def summary(self, name: str) -> DurationSummary | None:
        with self._lock:
            samples = tuple(self._samples.get(name, ()))
        if not samples:
            return None
        ordered = sorted(samples)
        total = sum(ordered)
        return DurationSummary(
            count=len(ordered),
            mean_ms=total / len(ordered),
            p50_ms=_percentile(ordered, 0.50),
            p95_ms=_percentile(ordered, 0.95),
            max_ms=ordered[-1],
            total_ms=total,
        )

    def counters(self) -> Mapping[str, int]:
        with self._lock:
            return MappingProxyType(dict(self._counters))

    def snapshot(self) -> dict[str, Any]:
        """Vista serializable para `/status`, `--doctor --json` y tests."""
        durations: dict[str, dict[str, float | int]] = {}
        with self._lock:
            counters = dict(self._counters)
            names = list(self._samples)
        for name in names:
            summary = self.summary(name)
            if summary is not None:
                durations[name] = summary.as_dict()
        return {"counters": counters, "durations_ms": durations}

    def reset(self) -> None:
        with self._lock:
            self._counters.clear()
            self._samples.clear()


# --------------------------------------------------------------------- formato
def _clean_fields(fields: Mapping[str, Any] | None) -> dict[str, Any]:
    if not fields:
        return {}
    cleaned: dict[str, Any] = {}
    for raw_key, value in fields.items():
        key = str(raw_key)
        if key in _RESERVED_RECORD_KEYS:
            key = f"x_{key}"
        if isinstance(value, str):
            cleaned[key] = redact(value)[:_MAX_FIELD_CHARS]
        elif isinstance(value, (bool, int, float)) or value is None:
            cleaned[key] = value
        else:
            cleaned[key] = redact(str(value))[:_MAX_FIELD_CHARS]
    return cleaned


def log_event(
    logger: logging.Logger,
    level: int,
    event: str,
    fields: Mapping[str, Any] | None = None,
    *,
    exc_info: BaseException | None = None,
) -> None:
    """Emite un evento estructurado; `event` es el contrato estable, no el texto."""
    if not logger.isEnabledFor(level):
        return
    # El trace_id se fija al emitir (no al formatear): la correlación survive a
    # handlers diferidos, colas o formatos en otro hilo.
    logger.log(
        level,
        event,
        exc_info=exc_info,
        extra={
            "event": event,
            "fields": _clean_fields(fields),
            "autonoma_trace_id": fields.get("trace_id") if fields and "trace_id" in fields else current_trace_id(),
        },
    )


class JsonLogFormatter(logging.Formatter):
    """Una línea JSON por registro: `ts`, nivel, `trace_id`, evento y campos."""

    def __init__(self, *, app_name: str = "Autonoma", version: str = "") -> None:
        super().__init__(datefmt="%Y-%m-%dT%H:%M:%S")
        self._app_name = app_name
        self._version = version

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            # Se lee del registro (fijado por el filtro al emitir): si el formato ocurre
            # más tarde o en otro hilo, la correlación no se pierde.
            "trace_id": str(getattr(record, "autonoma_trace_id", "") or current_trace_id()),
            "event": str(getattr(record, "event", "") or record.getMessage()),
            "app": self._app_name,
        }
        if self._version:
            payload["version"] = self._version
        fields = getattr(record, "fields", None)
        if isinstance(fields, Mapping) and fields:
            payload.update(dict(fields))
        if record.exc_info or record.exc_text:
            detail = record.exc_text or (self.formatException(record.exc_info) if record.exc_info else "")
            payload["exception"] = redact(detail)
        return json.dumps(payload, ensure_ascii=True, default=str, separators=(",", ":"))


class PlainLogFormatter(logging.Formatter):
    """Formato humano para consola, con `trace_id` para correlación manual."""

    def format(self, record: logging.LogRecord) -> str:
        event = getattr(record, "event", None)
        body = str(event) if event else record.getMessage()
        trace = str(getattr(record, "autonoma_trace_id", "") or current_trace_id())
        suffix = f" [{trace}]" if trace else ""
        return f"[{record.levelname[:1]}] {body}{suffix}"


class _RedactionFilter(logging.Filter):
    """Censura secretos conocidos en cualquier registro, incluido el traceback."""

    def __init__(self, secret_values: Sequence[str]) -> None:
        super().__init__()
        self._secrets: tuple[str, ...] = tuple(
            item for item in (value.strip() for value in secret_values) if len(item) >= _MIN_SECRET_SCAN_LENGTH
        )

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._secrets:
            return True
        record.msg = redact(str(record.msg), self._secrets)
        fields = getattr(record, "fields", None)
        if isinstance(fields, MutableMapping):
            for key, value in list(fields.items()):
                if isinstance(value, str):
                    fields[key] = redact(value, self._secrets)
        if record.exc_text:
            record.exc_text = redact(record.exc_text, self._secrets)
        return True


class _TraceIdFilter(logging.Filter):
    """Fija el `trace_id` vigente en el registro para todos los handlers."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "autonoma_trace_id"):
            record.autonoma_trace_id = current_trace_id()
        return True


# --------------------------------------------------------------------- ciclo de vida
@dataclass(frozen=True, slots=True)
class LoggingRuntime:
    """Estado del subsistema; `close()` devuelve el root logger a su estado previo."""

    log_file: Path | None
    level: int
    json_logs: bool
    handlers: tuple[logging.Handler, ...]
    _previous_handlers: tuple[logging.Handler, ...]
    _previous_level: int

    @property
    def handler_count(self) -> int:
        return len(self.handlers)

    def close(self) -> None:
        root = logging.getLogger()
        for handler in self.handlers:
            with contextlib.suppress(OSError):
                root.removeHandler(handler)
                handler.close()
        for handler in self._previous_handlers:
            root.addHandler(handler)
        root.setLevel(self._previous_level)


def configure_logging(
    *,
    log_dir: Path | None,
    level: int = logging.INFO,
    secret_values: Sequence[str] = (),
    json_logs: bool = True,
    console: bool = False,
    version: str = "",
) -> LoggingRuntime:
    """Sustituye `basicConfig` por handlers explícitos: JSON rotativo y consola opcional.

    Devuelve un `LoggingRuntime` inmutable que el dueño de la sesión debe cerrar;
    así los handlers no quedan colgando tras `--data-dir` o un `/key` (rebuild).
    """
    root = logging.getLogger()
    previous_handlers = tuple(root.handlers)
    for handler in previous_handlers:
        root.removeHandler(handler)
    handlers: list[logging.Handler] = []
    log_file: Path | None = None
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / _DEFAULT_LOG_NAME
        file_handler = RotatingFileHandler(log_file, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
        file_handler.setFormatter(JsonLogFormatter(version=version) if json_logs else PlainLogFormatter())
        handlers.append(file_handler)
    if console or log_file is None:
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(PlainLogFormatter())
        handlers.append(stream)
    redaction = _RedactionFilter(secret_values)
    trace = _TraceIdFilter()
    for handler in handlers:
        handler.addFilter(redaction)
        handler.addFilter(trace)
        root.addHandler(handler)
    root.setLevel(level)
    return LoggingRuntime(
        log_file=log_file,
        level=level,
        json_logs=json_logs,
        handlers=tuple(handlers),
        _previous_handlers=previous_handlers,
        _previous_level=root.level,
    )


# --------------------------------------------------------------------- temporizador
@contextlib.contextmanager
def measure(
    metrics: MetricsRegistry | None,
    name: str,
    *,
    logger: logging.Logger | None = None,
    event_end: str | None = None,
    fields: Mapping[str, Any] | None = None,
) -> Iterator[MetricOutcome]:
    """Cronómetro tipado: anota duración y desenlace, y re-lanza sin tragar nada."""
    outcome = MetricOutcome()
    active = logger if logger is not None else logging.getLogger("autonoma.metrics")
    started = time.perf_counter()
    try:
        yield outcome
    except BaseException as exc:
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        code = exc.code if isinstance(exc, AutonomaError) else ErrorCode.INTERNAL
        outcome.fail(code)
        if metrics is not None:
            metrics.increment(f"{name}.failure.{code.value}")
            metrics.observe_duration(name, elapsed_ms)
        log_event(
            active,
            exc.severity if isinstance(exc, AutonomaError) else logging.ERROR,
            event_end or f"{name}.error",
            {**(fields or {}), "duration_ms": round(elapsed_ms, 2), "error_code": code.value},
        )
        raise
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    outcome.succeed()
    if metrics is not None:
        metrics.increment(f"{name}.success")
        metrics.observe_duration(name, elapsed_ms)
    if event_end is not None:
        log_event(
            active,
            logging.DEBUG,
            event_end,
            {**(fields or {}), "duration_ms": round(elapsed_ms, 2)},
        )


def runtime_metadata() -> dict[str, Any]:
    """Datos de entorno para diagnósticos: congelado, SO, Python, proceso."""
    return {
        "app": "Autonoma",
        "version": _app_version(),
        "python": platform.python_version(),
        "platform": platform.system(),
        "machine": platform.machine(),
        "frozen": bool(getattr(sys, "frozen", False)),
        "pid": os.getpid(),
    }


def _app_version() -> str:
    try:
        from autonoma import __version__
    except ImportError:  # pragma: no cover - sólo en import parcial del empaquetado
        return ""
    return str(__version__)
