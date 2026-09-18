"""CRUD de disco con política de rutas del sistema, sobre un supervisor de procesos.

Cambios de fondo respecto a la versión anterior:
- La política de rutas protegidas se resuelve **una vez** al construir el gestor y
  se indexa por tuplas de componentes: `is_protected` deja de llamar a
  `Path.resolve()` por cada raíz protegida (antes ~15 llamadas a `realpath` por
  operación) y pasa a ser O(profundidad) con búsquedas en `frozenset`.
- En Windows la comparación se normaliza a minúsculas: `C:\\WINDOWS` ya no evade
  la política por diferencia de mayúsculas.
- Ejecución de procesos delegada en `ProcessSupervisor` (una sola responsabilidad).
- Escrituras atómicas (temporal + `os.replace`): un fallo a mitad no deja archivo
  truncado. `force` se conserva como parámetro decorativo y se registra que no
  elude la política; `user_prompt` deja de viajar hasta la capa de E/S.
"""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
import stat
import tempfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from autonoma.errors import FileSystemError, PathPolicyError
from autonoma.key_handler import PanicController
from autonoma.path_policy import has_redirected_component, is_redirected, validate_windows_path
from autonoma.processes import CommandResult, ProcessSupervisor

__all__ = ["CommandResult", "FileSystemError", "FileSystemManager", "ProtectedPolicy"]

logger = logging.getLogger(__name__)

_READ_DEFAULT_CHARS: Final[int] = 80_000
_LIST_DEFAULT_ENTRIES: Final[int] = 400
_TRUNCATION_NOTE: Final[str] = "\n\n…[truncado, más de {limit} caracteres]"
_PROTECTED_ENV_WINDOWS: Final[tuple[str, ...]] = (
    "WINDIR", "SYSTEMROOT", "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMDATA",
)
_UNIX_ROOT: Final[tuple[str, ...]] = ("/",)


def _win_env_path(name: str, env: dict[str, str] | None = None) -> Path | None:
    raw = (os.environ if env is None else env).get(name)
    if not raw:
        return None
    try:
        return Path(raw).resolve()
    except OSError:
        return None


def default_protected_roots(env: dict[str, str] | None = None) -> tuple[Path, ...]:
    """Rutas críticas del SO para este plataforma (no es una lista de allow-list)."""
    if os.name == "nt":
        candidates: list[Path] = [
            Path(r"C:\Windows"),
            Path(r"C:\Windows\System32"),
            Path(r"C:\Windows\SysWOW64"),
            Path(r"C:\Program Files"),
            Path(r"C:\Program Files (x86)"),
            Path(r"C:\ProgramData"),
            Path(r"C:\Recovery"),
            Path(r"C:\$Recycle.Bin"),
            Path(r"C:\System Volume Information"),
            Path(r"C:\Boot"),
            Path(r"C:\EFI"),
        ]
        candidates.extend(
            extra for name in _PROTECTED_ENV_WINDOWS if (extra := _win_env_path(name, env)) is not None
        )
    else:
        candidates = [
            Path("/"), Path("/etc"), Path("/usr"), Path("/bin"), Path("/sbin"), Path("/lib"),
            Path("/lib64"), Path("/boot"), Path("/dev"), Path("/proc"), Path("/sys"), Path("/root"),
            Path("/run"), Path("/snap"), Path("/System"), Path("/Library"), Path("/private/etc"),
            Path("/private/var"), Path("/Applications"), Path("/usr/local/bin"),
        ]
    resolved: list[Path] = []
    for candidate in candidates:
        try:
            resolved.append(candidate.resolve())
        except OSError:
            resolved.append(candidate)
    return tuple(resolved)


@dataclass(frozen=True, slots=True)
class ProtectedPolicy:
    """Índices inmutables de rutas protegidas: consultas por hash, no por barrido."""

    display: tuple[str, ...]
    _prefixes: frozenset[tuple[str, ...]]
    _depths: frozenset[int]
    _exact_only: frozenset[tuple[str, ...]]
    _ancestors: frozenset[tuple[str, ...]]

    @classmethod
    def build(cls, roots: Iterable[Path]) -> ProtectedPolicy:
        prefixes: set[tuple[str, ...]] = set()
        exact_only: set[tuple[str, ...]] = set()
        ancestors: set[tuple[str, ...]] = set()
        display: list[str] = []
        for root in roots:
            parts = normalize_parts(root)
            display.append(str(root))
            if os.name != "nt" and parts == _UNIX_ROOT:
                # "/" sólo bloquea borrar el propio nodo raíz, no todo lo que cuelga de él.
                exact_only.add(parts)
                continue
            prefixes.add(parts)
            ancestors.update(all_prefixes(parts))
        return cls(
            display=tuple(display),
            _prefixes=frozenset(prefixes),
            _depths=frozenset(len(parts) for parts in prefixes),
            _exact_only=frozenset(exact_only),
            _ancestors=frozenset(ancestors),
        )

    def covers(self, parts: tuple[str, ...]) -> bool:
        """`True` si la ruta es o está dentro de una ubicación crítica."""
        if parts in self._exact_only or parts in self._prefixes:
            return True
        return any(parts[:depth] in self._prefixes for depth in self._depths if 0 < depth < len(parts))

    def contains_a_protected_root(self, parts: tuple[str, ...]) -> bool:
        """`True` si borrar/mover esta ruta dejaría huérfana una ubicación crítica."""
        return parts in self._ancestors

    @property
    def root_count(self) -> int:
        return len(self.display)


def normalize_parts(path: Path) -> tuple[str, ...]:
    """Componentes comparables: minúsculas en Windows, donde el FS no distingue caso."""
    parts = tuple(str(part) for part in path.parts)
    return tuple(part.lower() for part in parts) if os.name == "nt" else parts


def all_prefixes(parts: tuple[str, ...]) -> tuple[tuple[str, ...], ...]:
    return tuple(parts[:length] for length in range(1, len(parts) + 1))


class FileSystemManager:
    """Leer, escribir, copiar, mover, borrar y lanzar procesos con protecciones."""

    def __init__(
        self,
        panic: PanicController,
        extra_protected: Sequence[str] | None = None,
        command_timeout: float = 60.0,
        *,
        env: dict[str, str] | None = None,
    ) -> None:
        self.panic = panic
        self._protected = ProtectedPolicy.build(
            [*default_protected_roots(env), *(Path(raw).expanduser() for raw in (extra_protected or ()))],
        )
        self._supervisor = ProcessSupervisor(panic, default_timeout=command_timeout)

    # ------------------------------------------------------------------ paths
    def resolve(self, path: str | Path) -> Path:
        if os.name == "nt":
            validate_windows_path(str(path))
        return Path(path).expanduser().resolve()

    def is_protected(self, path: str | Path) -> bool:
        """`True` si la ruta es (o cuelga de) un directorio crítico del SO."""
        try:
            target = self.resolve(path)
        except (OSError, PathPolicyError):
            target = Path(path).expanduser()
        return self._protected.covers(normalize_parts(target))

    @property
    def protected_roots(self) -> tuple[str, ...]:
        return self._protected.display

    # -------------------------------------------------------------- políticas
    def _reject_if_redirected(self, raw: str | Path) -> None:
        if has_redirected_component(Path(raw).expanduser()):
            raise FileSystemError(
                "Las operaciones mediante enlaces simbólicos/reparse points están bloqueadas",
                context={"path": str(raw)},
            )

    def _assert_writable(self, path: str | Path, *, force: bool) -> Path:
        target = self.resolve(path)
        protected = self._protected.covers(normalize_parts(target))
        if protected:
            raise FileSystemError(
                f"Ruta protegida del sistema operativo: {target}. Operación bloqueada.",
                context={"path": str(target)},
            )
        if force:
            logger.info(
                "force solicitado y descartado",
                extra={"event": "fs.force_ignored", "fields": {"path": str(target)}},
            )
        self._reject_if_redirected(path)
        return target

    def _assert_deletable(self, path: str | Path, *, force: bool) -> Path:
        target = self._assert_writable(path, force=force)
        if self._protected.contains_a_protected_root(normalize_parts(target)):
            raise FileSystemError(
                "La ruta contiene una ubicación protegida",
                context={"path": str(target)},
            )
        return target

    def _assert_plain_tree(self, source: Path) -> None:
        if has_redirected_component(source):
            raise FileSystemError("No se permiten enlaces simbólicos en copias recursivas")
        if not source.is_dir():
            return
        for parent, directories, files in os.walk(source, followlinks=False):
            self.panic.check()
            if any(is_redirected(Path(parent) / name) for name in (*directories, *files)):
                raise FileSystemError("No se permiten enlaces simbólicos en copias recursivas")

    def _prepare_destination(self, source: Path, dest: Path) -> None:
        if not source.exists():
            raise FileSystemError(f"Origen no existe: {source}", context={"source": str(source)})
        if dest.exists():
            raise FileSystemError("El destino ya existe; elige una ruta nueva explícita")
        if dest.is_relative_to(source):
            raise FileSystemError("El destino no puede estar dentro del origen")

    # ------------------------------------------------------------------- CRUD
    def read_file(self, path: str, max_chars: int = _READ_DEFAULT_CHARS) -> str:
        """Lectura acotada; nunca se devuelve un archivo de tamaño arbitrario."""
        self.panic.check()
        target = self.resolve(path)
        if not target.exists():
            raise FileSystemError(f"No existe: {target}", context={"path": str(target)})
        if target.is_dir():
            raise FileSystemError(f"Es un directorio, no un archivo: {target}")
        try:
            with target.open(encoding="utf-8", errors="replace") as stream:
                data = stream.read(max_chars + 1)
        except OSError as exc:
            raise FileSystemError(f"No se pudo leer {target}: {exc}", context={"path": str(target)}) from exc
        if len(data) > max_chars:
            return data[:max_chars] + _TRUNCATION_NOTE.format(limit=max_chars)
        return data

    def write_file(self, path: str, content: str, *, force: bool = False) -> str:
        """Escritura atómica: o el contenido completo, o el archivo anterior intacto."""
        self.panic.check()
        target = self._assert_writable(path, force=force)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(target, content)
        except OSError as exc:
            raise FileSystemError(f"No se pudo escribir {target}: {exc}", context={"path": str(target)}) from exc
        return f"Escrito {target} ({len(content)} caracteres)"

    def append_file(self, path: str, content: str, *, force: bool = False) -> str:
        self.panic.check()
        target = self._assert_writable(path, force=force)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("a", encoding="utf-8") as handle:
                handle.write(content)
        except OSError as exc:
            raise FileSystemError(f"No se pudo anexar {target}: {exc}", context={"path": str(target)}) from exc
        return f"Anexado a {target}"

    def copy_path(self, src: str, dst: str, *, force: bool = False) -> str:
        self.panic.check()
        self._assert_plain_tree(Path(src).expanduser())
        source = self.resolve(src)
        dest = self._assert_writable(dst, force=force)
        self._prepare_destination(source, dest)
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            if source.is_dir():
                shutil.copytree(source, dest)
            else:
                shutil.copy2(source, dest)
        except (OSError, shutil.Error) as exc:
            raise FileSystemError(f"Copia falló: {exc}", context={"source": str(source)}) from exc
        return f"Copiado {source} → {dest}"

    def move_path(self, src: str, dst: str, *, force: bool = False) -> str:
        self.panic.check()
        source = self._assert_deletable(src, force=force)
        dest = self._assert_writable(dst, force=force)
        self._prepare_destination(source, dest)
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(dest))
        except (OSError, shutil.Error) as exc:
            raise FileSystemError(f"Movimiento falló: {exc}", context={"source": str(source)}) from exc
        return f"Movido {source} → {dest}"

    def delete_path(self, path: str, *, force: bool = False) -> str:
        self.panic.check()
        target = self._assert_deletable(path, force=force)
        if not target.exists():
            raise FileSystemError(f"No existe: {target}", context={"path": str(target)})
        try:
            if target.is_dir():
                shutil.rmtree(target)
                return f"Directorio eliminado: {target}"
            target.unlink()
            return f"Archivo eliminado: {target}"
        except OSError as exc:
            raise FileSystemError(f"No se pudo eliminar {target}: {exc}", context={"path": str(target)}) from exc

    def list_dir(self, path: str, max_entries: int = _LIST_DEFAULT_ENTRIES) -> str:
        """Listado ordenado con una sola pasada de `scandir` (stat cacheado)."""
        self.panic.check()
        target = self.resolve(path)
        if not target.exists():
            raise FileSystemError(f"No existe: {target}", context={"path": str(target)})
        if not target.is_dir():
            raise FileSystemError(f"No es un directorio: {target}")
        try:
            observed = _scan_dir(target)
        except OSError as exc:
            raise FileSystemError(f"No se pudo listar {target}: {exc}", context={"path": str(target)}) from exc
        observed.sort(key=lambda item: (not item[1], item[0].lower()))
        lines: list[str] = [f"{target} ({len(observed)} entradas)"]
        for name, is_dir, size in observed[:max_entries]:
            kind = "dir " if is_dir else "file"
            suffix = f" {size}B" if not is_dir and size is not None else ""
            lines.append(f"  [{kind}] {name}{suffix}")
        if len(observed) > max_entries:
            lines.append(f"  … +{len(observed) - max_entries} más")
        return "\n".join(lines)

    def mkdir(self, path: str, *, force: bool = False) -> str:
        self.panic.check()
        target = self._assert_writable(path, force=force)
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise FileSystemError(f"No se pudo crear directorio {target}: {exc}", context={"path": str(target)}) from exc
        return f"Directorio listo: {target}"

    # -------------------------------------------------------------- procesos
    def run_command(
        self,
        command: str | list[str],
        *,
        cwd: str | None = None,
        timeout: float | None = None,
        shell: bool = True,
    ) -> CommandResult:
        """Ejecuta un programa externo; cancelable con la tecla P y con plazo duro."""
        self.panic.check()
        working_dir = self.resolve(cwd) if cwd else Path.cwd()
        if not working_dir.is_dir():
            raise FileSystemError(f"cwd inválido: {working_dir}", context={"cwd": str(working_dir)})
        return self._supervisor.run(command, cwd=working_dir, timeout=timeout, shell=shell)

    def format_command_result(self, result: CommandResult | dict[str, Any]) -> str:
        """Vista para el modelo. Acepta el dict antiguo para no romper llamadores."""
        if isinstance(result, CommandResult):
            return result.format_for_model()
        parts = [f"$ {result.get('command')}", f"exit={result.get('returncode')}"]
        for stream in ("stdout", "stderr"):
            payload = result.get(stream)
            if payload:
                parts.append(f"--- {stream} ---\n{payload}")
        return "\n".join(parts)

    @property
    def active_processes(self) -> int:
        return self._supervisor.active_count

    def kill_all(self) -> None:
        self._supervisor.kill_all()

    def close(self) -> None:
        """`ResourcePort`: cerrar el gestor significa dejar el árbol de procesos a cero."""
        self._supervisor.kill_all()


def _scan_dir(target: Path) -> list[tuple[str, bool, int | None]]:
    """`os.scandir` cachea `is_dir`/`stat` por entrada: menos syscalls que `iterdir`."""
    observed: list[tuple[str, bool, int | None]] = []
    with os.scandir(target) as entries:
        for entry in entries:
            try:
                is_dir = bool(entry.is_dir(follow_symlinks=False))
            except OSError:
                is_dir = False
            size: int | None = None
            if not is_dir:
                try:
                    size = int(entry.stat(follow_symlinks=False).st_size)
                except OSError:
                    size = None
            observed.append((entry.name, is_dir, size))
    return observed


def atomic_write_text(target: Path, content: str, *, private_default_mode: int = 0o600) -> None:
    """Escribe en un temporal del mismo directorio y lo publica con `os.replace`.

    Preserva el modo del archivo existente y evita dejar contenido truncado si el
    proceso muere a mitad de escritura. `os.fsync` protege ante caída de energía en
    el host; en archivos de configuración y notas el coste es irrelevante.
    """
    previous_mode = _current_mode(target)
    descriptor, temporary = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
    published = False
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, previous_mode if previous_mode is not None else private_default_mode)
        os.replace(temporary, target)
        published = True
    finally:
        if not published:
            with contextlib.suppress(OSError):
                os.unlink(temporary)


def _current_mode(target: Path) -> int | None:
    try:
        return stat.S_IMODE(target.stat().st_mode)
    except OSError:
        return None
