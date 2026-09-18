"""Supervisor de procesos: arranque, drenaje de tuberías y término del árbol.

Responsabilidad única de `filesystem.py`: aquí sólo hay gestión de procesos.
La salida se limita por cola (memoria acotada aunque el hijo escriba sin parar),
el reloj es monotónico y la cancelación cooperativa revisa `PanicController`.
"""

from __future__ import annotations

import contextlib
import logging
import math
import os
import shlex
import signal
import subprocess
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from autonoma.errors import ConfigurationError, ProcessLaunchError, ProcessTimeoutError
from autonoma.key_handler import PanicController

__all__ = ["CommandResult", "ProcessSupervisor"]

logger = logging.getLogger(__name__)

_STDOUT_CAP: Final[int] = 12_000
_STDERR_CAP: Final[int] = 8_000
_READ_CHUNK: Final[int] = 4_096
_MAX_TIMEOUT_SECONDS: Final[float] = 300.0
_MIN_TIMEOUT_SECONDS: Final[float] = 0.01
_KILL_GRACE_SECONDS: Final[float] = 5.0
_TERM_GRACE_SECONDS: Final[float] = 1.5
_POLL_INTERVAL: Final[float] = 0.05
_TAIL_HEADROOM: Final[float] = 2.0


@dataclass(frozen=True, slots=True)
class CommandResult:
    """Salida acotada de un proceso, medible y serializable sin claves privadas."""

    command: str | tuple[str, ...]
    returncode: int | None
    stdout: str
    stderr: str
    cwd: Path
    duration_ms: float
    truncated: bool = False

    @property
    def succeeded(self) -> bool:
        return self.returncode == 0

    @property
    def command_display(self) -> str:
        return self.command if isinstance(self.command, str) else " ".join(self.command)

    def as_dict(self) -> dict[str, Any]:
        return {
            "command": self.command,
            "returncode": self.returncode,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "cwd": str(self.cwd),
            "duration_ms": round(self.duration_ms, 1),
            "truncated": self.truncated,
        }

    def format_for_model(self) -> str:
        """Vista compacta para el modelo: comando, salida y señal de truncado."""
        parts = [f"$ {self.command_display}", f"exit={self.returncode}", f"cwd={self.cwd}",
                 f"duracion={self.duration_ms / 1000:.2f}s"]
        if self.stdout:
            parts.append("--- stdout ---\n" + self.stdout)
        if self.stderr:
            parts.append("--- stderr ---\n" + self.stderr)
        if self.truncated:
            parts.append("[salida truncada: se conservaron las colas de stdout/stderr]")
        return "\n".join(parts)


class _TailBuffer:
    """Cola de bloques con tope; evita copiar todo el flujo y acota la memoria."""

    __slots__ = ("_chunks", "_limit", "_size")

    def __init__(self, limit: int) -> None:
        self._chunks: list[str] = []
        self._limit = limit
        self._size = 0

    def append(self, chunk: str) -> None:
        if not chunk:
            return
        self._chunks.append(chunk)
        self._size += len(chunk)
        if self._size > self._limit * _TAIL_HEADROOM:
            self._trim()

    def _trim(self) -> None:
        joined = "".join(self._chunks)
        tail = joined[-self._limit:]
        self._chunks = [tail]
        self._size = len(tail)

    @property
    def overflowed(self) -> bool:
        return self._size > self._limit

    def text(self) -> str:
        """Texto con las últimas `_limit` caracteres."""
        return "".join(self._chunks)[-self._limit:]


def _drain(stream: Any, buffer: _TailBuffer) -> None:
    """Lector de un solo hilo por tubería: cierra siempre, aunque cancele el flujo."""
    if stream is None:
        return
    try:
        while chunk := stream.read(_READ_CHUNK):
            buffer.append(chunk)
    except (OSError, ValueError):
        # tubería cerrada por el proceso o por cancelación: la cola parcial es válida
        pass
    finally:
        with contextlib.suppress(OSError):
            stream.close()


class ProcessSupervisor:
    """Lanza, supervisa y limpia procesos hijos; cancelable por `PanicController`."""

    __slots__ = ("_active", "_default_timeout", "_lock", "_panic", "_stderr_cap", "_stdout_cap")

    def __init__(
        self,
        panic: PanicController,
        *,
        default_timeout: float = 60.0,
        stdout_cap: int = _STDOUT_CAP,
        stderr_cap: int = _STDERR_CAP,
    ) -> None:
        self._panic = panic
        self._default_timeout = _validated_timeout(default_timeout)
        self._stdout_cap = stdout_cap
        self._stderr_cap = stderr_cap
        self._active: set[subprocess.Popen[str]] = set()
        self._lock = threading.Lock()
        panic.register_cleanup(self.kill_all)

    # ------------------------------------------------------------------ API
    @property
    def default_timeout(self) -> float:
        return self._default_timeout

    @property
    def active_count(self) -> int:
        with self._lock:
            return len(self._active)

    def run(
        self,
        command: str | Sequence[str],
        *,
        cwd: Path,
        timeout: float | None = None,
        shell: bool = True,
    ) -> CommandResult:
        """Ejecuta un comando con salida acotada y devolución del resultado tipado."""
        argv = prepare_command(command, shell=shell)
        limit = self._default_timeout if timeout is None else _validated_timeout(timeout)
        process = self._spawn(argv, cwd=cwd, shell=shell)
        with self._lock:
            self._active.add(process)
        started = time.perf_counter()
        try:
            stdout_tail, stderr_tail = self._pump(process, limit)
        finally:
            with self._lock:
                self._active.discard(process)
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        result = CommandResult(
            command=command if isinstance(command, str) else tuple(str(item) for item in command),
            returncode=process.returncode,
            stdout=stdout_tail.text(),
            stderr=stderr_tail.text(),
            cwd=cwd,
            duration_ms=elapsed_ms,
            truncated=stdout_tail.overflowed or stderr_tail.overflowed,
        )
        logger.debug(
            "proceso terminado",
            extra={"event": "process.finished",
                   "fields": {"exit": result.returncode, "duration_ms": round(elapsed_ms, 1),
                              "shell": shell}},
        )
        return result

    def kill_all(self) -> None:
        """Termina el árbol de todos los procesos vivos y se desregistra del pánico."""
        self._panic.unregister_cleanup(self.kill_all)
        with self._lock:
            processes = list(self._active)
        for process in processes:
            terminate_process(process)

    # ------------------------------------------------------------- internos
    def _spawn(self, argv: str | list[str], *, cwd: Path, shell: bool) -> subprocess.Popen[str]:
        kwargs: dict[str, Any] = {
            "cwd": str(cwd),
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "stdin": subprocess.DEVNULL,
            "text": True,
            # utf-8-sig: quita un BOM inicial si el programa imprime UTF-8 con BOM
            # (típico en herramientas de Windows) sin penalizar el UTF-8 normal.
            "encoding": "utf-8-sig",
            "errors": "replace",
            "shell": shell,
        }
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
        else:
            kwargs["start_new_session"] = True
        try:
            return subprocess.Popen(argv, **kwargs)
        except OSError as exc:
            raise ProcessLaunchError(
                f"No se pudo lanzar el proceso: {exc}",
                context={"cwd": str(cwd), "error": type(exc).__name__},
            ) from exc

    def _pump(self, process: subprocess.Popen[str], limit: float) -> tuple[_TailBuffer, _TailBuffer]:
        """Drena ambas tuberías en paralelo y espera con cancelación cooperativa."""
        stdout_tail = _TailBuffer(self._stdout_cap)
        stderr_tail = _TailBuffer(self._stderr_cap)
        readers = [
            threading.Thread(target=_drain, args=(process.stdout, stdout_tail), daemon=True, name="autonoma-stdout"),
            threading.Thread(target=_drain, args=(process.stderr, stderr_tail), daemon=True, name="autonoma-stderr"),
        ]
        for reader in readers:
            reader.start()
        deadline = time.monotonic() + limit
        timed_out = False
        try:
            while process.poll() is None or any(reader.is_alive() for reader in readers):
                self._panic.check()
                if time.monotonic() >= deadline:
                    timed_out = True
                    break
                self._panic.wait(_POLL_INTERVAL)
        finally:
            terminate_process(process)
            for reader in readers:
                reader.join(timeout=_KILL_GRACE_SECONDS)
        if timed_out:
            raise ProcessTimeoutError(
                f"Timeout ({limit:g}s) ejecutando comando",
                context={
                    "stdout_tail": stdout_tail.text()[-240:],
                    "stderr_tail": stderr_tail.text()[-240:],
                },
            )
        return stdout_tail, stderr_tail


def prepare_command(command: str | Sequence[str], *, shell: bool) -> str | list[str]:
    """Normaliza el comando y rechaza lo que `subprocess` aceptaría de forma engañosa."""
    if isinstance(command, str):
        if not command.strip():
            raise ProcessLaunchError("Comando vacío")
        if "\x00" in command:
            raise ProcessLaunchError("El comando no puede contener NUL")
        return command
    argv = [str(item) for item in command]
    if not argv:
        raise ProcessLaunchError("argv vacío")
    if any("\x00" in item for item in argv):
        raise ProcessLaunchError("argv no puede contener NUL")
    if shell:
        # `shell=True` con lista ejecuta sólo el primer elemento y descarta el resto:
        # entrada ambigua, no un comando. Se exige decisión explícita del llamador.
        raise ProcessLaunchError(
            "argv requiere shell=False; con shell=True pasa un único string",
            context={"argv": shlex.join(argv)[:240]},
        )
    return argv


def _validated_timeout(value: float) -> float:
    try:
        timeout = float(value)
    except (TypeError, ValueError) as exc:
        # Un timeout inválido es configuración mal formada, no un plazo agotado.
        raise ConfigurationError("timeout debe ser numérico") from exc
    if not math.isfinite(timeout) or not _MIN_TIMEOUT_SECONDS <= timeout <= _MAX_TIMEOUT_SECONDS:
        raise ConfigurationError(
            f"timeout debe estar entre {_MIN_TIMEOUT_SECONDS:g} y {_MAX_TIMEOUT_SECONDS:g} segundos",
            context={"received": str(value)},
        )
    return timeout


def terminate_process(process: subprocess.Popen[str]) -> None:
    """Termina el árbol: grupo de procesos en POSIX, descendientes conocidos en Windows."""
    if process.poll() is not None:
        return
    if os.name == "nt":
        _kill_windows_tree(process)
        return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(os.getpgid(process.pid), signal.SIGTERM)
    with contextlib.suppress(subprocess.TimeoutExpired):
        process.wait(timeout=_TERM_GRACE_SECONDS)
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    if process.poll() is None:
        with contextlib.suppress(OSError):
            process.kill()


def _kill_windows_tree(process: subprocess.Popen[str]) -> None:
    """En Windows primero los hijos (para no dejar huérfanos) y luego el padre."""
    try:
        import psutil
    except ImportError:
        logger.warning(
            "psutil no disponible: limpieza de descendientes limitada",
            extra={"event": "process.psutil_missing", "fields": {}},
        )
    else:
        try:
            children = psutil.Process(process.pid).children(recursive=True)
        except psutil.Error as exc:
            logger.debug(
                "no se pudieron enumerar descendientes",
                extra={"event": "process.children_error", "fields": {"error": type(exc).__name__}},
            )
        else:
            for child in reversed(children):
                with contextlib.suppress(psutil.Error):
                    child.kill()
    if process.poll() is None:
        with contextlib.suppress(OSError):
            process.kill()
    with contextlib.suppress(subprocess.TimeoutExpired):
        process.wait(timeout=_KILL_GRACE_SECONDS)
