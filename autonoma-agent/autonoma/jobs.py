"""Trabajos en segundo plano: lanzar, mirar, y matar procesos que siguen vivos.

Hasta ahora el agente sólo sabía esperar: `run_command` bloquea hasta que el programa
termina, así que un `npm run dev`, una compilación larga o un servidor eran imposibles de
automatizar. Aquí el stdout va a un archivo y el `Popen` se queda en una tabla en memoria:
consultar el progreso es leer texto, no vigilar tuberías.

No es un gestor de servicios: los trabajos viven mientras vive el proceso del agente.
`close()` (invocado al cerrar la sesión y por el controlador de pánico) limpia el árbol.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from autonoma.errors import FileSystemError
from autonoma.key_handler import PanicController
from autonoma.processes import is_windows, prepare_command, terminate_process

__all__ = ["JobInfo", "JobRunner"]

_ID_PREFIX: Final[str] = "j"
_DEFAULT_TAIL_CHARS: Final[int] = 4_000
_MAX_TRACKED_JOBS: Final[int] = 16
_EMPTY_LOG: Final[str] = "(el trabajo aún no ha escrito nada)"
_MAX_ARCHIVED: Final[int] = 64
_PRIVATE_MODE: Final[int] = 0o600


@dataclass(frozen=True, slots=True)
class JobInfo:
    """Ficha de un trabajo: lo que el modelo necesita para decidir si esperar o matar."""

    job_id: str
    command: str
    pid: int
    started_at: float
    log_path: str
    alive: bool
    returncode: int | None

    @property
    def runtime_seconds(self) -> float:
        return max(0.0, time.time() - self.started_at)

    def summary(self) -> str:
        state = (
            f"vivo (pid {self.pid}, {self.runtime_seconds:.0f} s)"
            if self.alive
            else f"terminado (exit={self.returncode})"
        )
        return f"{self.job_id} · {state} · {self.command[:160]}\nlog: {self.log_path}"


@dataclass(slots=True)
class _Job:
    """Estado mutable interno: el proceso y su archivo de salida."""

    process: subprocess.Popen[str]
    command: str
    started_at: float
    log_path: Path


class JobRunner:
    """Tabla de procesos en segundo plano con salida en archivo."""

    __slots__ = ("_archive", "_counter", "_jobs", "directory", "max_jobs", "panic", "tail_chars")

    def __init__(
        self,
        panic: PanicController,
        directory: Path,
        *,
        max_jobs: int = _MAX_TRACKED_JOBS,
        tail_chars: int = _DEFAULT_TAIL_CHARS,
    ) -> None:
        self.panic = panic
        self.directory = Path(directory)
        self.max_jobs = max(1, int(max_jobs))
        self.tail_chars = max(200, int(tail_chars))
        self._jobs: dict[str, _Job] = {}
        # Register de trabajos terminados que salieron de la tabla por falta de sitio: sus logs
        # siguen siendo legibles por id, así que `job_output` no se vuelve ciego tras 16 spawns.
        self._archive: dict[str, _Job] = {}
        self._counter = 0
        panic.register_cleanup(self.close)

    # ------------------------------------------------------------------ público
    @property
    def active_count(self) -> int:
        return sum(1 for job in self._jobs.values() if job.process.poll() is None)

    def spawn(self, command: str, cwd: str | None = None) -> JobInfo:
        """Lanza y desengancha: devuelve la ficha al instante, no el resultado."""
        self.panic.check()
        argv = prepare_command(command, shell=True)
        self._ensure_capacity()
        working = self._resolve_cwd(cwd)
        log_path = self._next_log_path()
        try:
            handle = log_path.open("w", encoding="utf-8", errors="replace", newline="")
        except OSError as exc:
            raise FileSystemError(
                f"No se pudo abrir el log del trabajo {log_path}: {exc}", context={"path": str(log_path)}
            ) from exc
        if not is_windows():  # el log puede llevar secretos: sólo el usuario lo lee
            with contextlib.suppress(OSError):
                os.chmod(log_path, _PRIVATE_MODE)
        process = self._launch(argv, working=working, handle=handle)
        job_id = self._new_id()
        self._jobs[job_id] = _Job(process=process, command=str(command), started_at=time.time(), log_path=log_path)
        return self.info(job_id)

    def info(self, job_id: str | None = None) -> JobInfo:
        job = self._require(self._resolve_id(job_id))
        return JobInfo(
            job_id=self._resolve_id(job_id),
            command=job.command,
            pid=int(job.process.pid),
            started_at=job.started_at,
            log_path=str(job.log_path),
            alive=job.process.poll() is None,
            returncode=job.process.poll(),
        )

    def status(self, job_id: str | None = None) -> str:
        """Un trabajo concreto, o el panorama completo."""
        if job_id:
            return self.info(job_id).summary()
        if not self._jobs:
            return "No hay trabajos lanzados en esta sesión."
        lines = [f"{self.active_count} vivos de {len(self._jobs)} en esta sesión:"]
        lines.extend(f"  {self.info(key).summary().splitlines()[0]}" for key in sorted(self._jobs, reverse=True))
        if self._archive:
            lines.append(f"  ({len(self._archive)} terminados siguen legibles con job_output por su id)")
        return "\n".join(lines)

    def output(self, job_id: str | None = None, max_chars: int | None = None, *, tail: bool = True) -> str:
        """Cola (o cabeza) del log, con aviso de recorte para que se pueda pedir más."""
        wanted = self._resolve_id(job_id)
        job = self._require(wanted)
        limit = self.tail_chars if max_chars is None else max(200, int(max_chars))
        try:
            text = job.log_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise FileSystemError(
                f"No se pudo leer {job.log_path}: {exc}", context={"path": str(job.log_path)}
            ) from exc
        state = "vivo" if job.process.poll() is None else f"exit={job.process.poll()}"
        if not text.strip():
            return f"{wanted} ({state}): {_EMPTY_LOG}"
        clipped = text[-limit:] if tail else text[:limit]
        missing = len(text) - len(clipped)
        note = (
            f"\n\n…[{missing} caracteres {'anteriores' if tail else 'restantes'}; sube max_chars o usa tail=false]"
            if missing > 0
            else ""
        )
        return f"{wanted} ({state}, {len(text)} B)\n{clipped}{note}"

    def kill(self, job_id: str) -> str:
        """Mata el árbol del trabajo; el id debe ser explícito (nada de 'el último')."""
        wanted = (job_id or "").strip()
        job = self._require(wanted)
        terminate_process(job.process)
        remaining = self.output(wanted, 1_200)
        return f"Detenido {wanted} (exit={job.process.poll()})\n{remaining}"

    def close(self) -> None:
        """`ResourcePort`: se van todos los árboles, no sólo el proceso directo.

        Se desregistra del `PanicController`: si no, cada recarga del agente dejaría un
        callback colgando sobre un runner muerto.
        """
        self.panic.unregister_cleanup(self.close)
        for job in list(self._jobs.values()):
            terminate_process(job.process)
        self._jobs.clear()
        self._archive.clear()

    # ------------------------------------------------------------------ internos
    def _launch(self, argv: Any, *, working: Path, handle: Any) -> subprocess.Popen[str]:
        kwargs: dict[str, Any] = {
            "cwd": str(working),
            "stdout": handle,
            "stderr": subprocess.STDOUT,
            "stdin": subprocess.DEVNULL,
            # `shell=True` con un único string: el mismo compromiso de `run_command`, para que
            # pipes y comillas significuen lo mismo en primer y segundo plano.
            "shell": True,
            "text": True,
            "encoding": "utf-8-sig",
            "errors": "replace",
        }
        new_group: int = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if is_windows() else 0
        if new_group:
            kwargs["creationflags"] = new_group
        else:
            kwargs["start_new_session"] = True
        try:
            return subprocess.Popen(argv, **kwargs)
        except OSError as exc:
            with contextlib.suppress(OSError):
                handle.close()
            raise FileSystemError(
                f"No se pudo lanzar el trabajo: {exc}", context={"cwd": str(working), "error": type(exc).__name__}
            ) from exc

    def _ensure_capacity(self) -> None:
        self._prune_finished()
        if len(self._jobs) >= self.max_jobs:
            raise FileSystemError(
                f"Ya hay {len(self._jobs)} trabajos en esta sesión; mata los que no sirvan con kill_job"
            )

    def _prune_finished(self) -> None:
        """Los terminados salen de la tabla pero quedan en el archivo: la salida no se pierde."""
        for key in [key for key, job in self._jobs.items() if job.process.poll() is not None][-8:]:
            self._archive[key] = self._jobs.pop(key)
        while len(self._archive) > _MAX_ARCHIVED:
            self._archive.pop(next(iter(self._archive)))

    def _resolve_id(self, job_id: str | None) -> str:
        if job_id:
            return job_id.strip()
        pool = self._jobs or self._archive
        if not pool:
            raise FileSystemError("No hay trabajos lanzados en esta sesión")
        return max(pool, key=lambda key: pool[key].started_at)

    def _require(self, job_id: str) -> _Job:
        job = self._jobs.get(job_id) or self._archive.get(job_id)
        if job is None:
            known = ", ".join(sorted(self._jobs)) or "ninguno"
            raise FileSystemError(f"No hay un trabajo llamado {job_id}; vivos: {known}", context={"job_id": job_id})
        return job

    def _resolve_cwd(self, cwd: str | None) -> Path:
        base = Path(cwd).expanduser().resolve() if cwd else Path.cwd()
        if not base.is_dir():
            raise FileSystemError(f"cwd inválido: {base}", context={"cwd": str(base)})
        return base

    def _new_id(self) -> str:
        self._counter += 1
        return f"{_ID_PREFIX}{self._counter:02d}-{int(time.time()) % 100_000:05d}"

    def _next_log_path(self) -> Path:
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            if not is_windows():  # mismo criterio que `sessions/`: el árbol de datos es privado
                with contextlib.suppress(OSError):
                    os.chmod(self.directory, 0o700)
        except OSError as exc:
            raise FileSystemError(
                f"No se pudo crear el directorio de trabajos: {exc}", context={"path": str(self.directory)}
            ) from exc
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
        suffix = os.getpid()
        return self.directory / f"job-{stamp}-{suffix:04x}-{self._counter + 1:02d}.log"
