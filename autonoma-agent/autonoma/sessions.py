"""Sesiones persistidas: la conversación sobrevive a cerrar la terminal.

Un turno por línea JSON (`jsonl`) en `<raza de datos>/sessions/<id>.jsonl`: se añade sin
leer el archivo, lo lee una persona con `cat`, y si el proceso muere a mitad sólo se pierde
la última línea. No hay base de datos porque no hay nada que indexar.

Los permisos son 0600 por un motivo: aquí quedan los prompts, y un prompt puede contener
cualquier cosa. El directorio se ignora en git junto con `logs/` y `.env`.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from autonoma.errors import FileSystemError

__all__ = ["SessionInfo", "SessionStore", "StoredTurn", "session_id_for"]

_SUFFIX: Final[str] = ".jsonl"
_SAFE: Final[re.Pattern[str]] = re.compile(r"[^A-Za-z0-9._-]+")
_MAX_LINE_BYTES: Final[int] = 1_000_000
DEFAULT_LIST_LIMIT: Final[int] = 20
DEFAULT_MAX_TURNS: Final[int] = 2_000
_PRIVATE_MODE: Final[int] = 0o600


@dataclass(frozen=True, slots=True)
class StoredTurn:
    """Un turno en disco. `role` es plano (`user`/`assistant`): sin tool-calls raras."""

    role: str
    content: str
    at: float

    def as_payload(self) -> dict[str, Any]:
        return {"role": self.role, "content": self.content}

    def as_line(self) -> str:
        return json.dumps({"role": self.role, "content": self.content, "at": self.at}, ensure_ascii=False)


@dataclass(frozen=True, slots=True)
class SessionInfo:
    """Ficha de una sesión guardada, para listar sin cargar todo el contenido."""

    session_id: str
    turns: int
    updated_at: float
    preview: str

    def summary(self) -> str:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(self.updated_at))
        return f"{self.session_id} · {self.turns} turnos · {when} · {self.preview}"


def session_id_for(when: float) -> str:
    """Identificador ordenable y legible: `2026-09-18-053012-a1b2`."""
    stamp = time.strftime("%Y-%m-%d-%H%M%S", time.localtime(when))
    return f"{stamp}-{os.getpid():04x}"


class SessionStore:
    """Escribe y lee sesiones; con `enabled=False` es un sumidero silencioso."""

    __slots__ = ("directory", "enabled", "max_turns", "session_id")

    def __init__(
        self,
        directory: Path,
        *,
        enabled: bool = True,
        session_id: str | None = None,
        max_turns: int = DEFAULT_MAX_TURNS,
    ) -> None:
        self.directory = Path(directory)
        self.enabled = bool(enabled)
        self.max_turns = max(2, int(max_turns))
        self.session_id = _SAFE.sub("-", session_id) if session_id else None

    # ------------------------------------------------------------------ caminos
    def path_for(self, session_id: str) -> Path:
        cleaned = _SAFE.sub("-", (session_id or "").strip())
        if not cleaned:
            raise FileSystemError("El identificador de sesión no puede quedar vacío")
        return self.directory / f"{cleaned}{_SUFFIX}"

    def _ensure_directory(self) -> None:
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            os.chmod(self.directory, 0o700)
        except OSError as exc:
            raise FileSystemError(
                f"No se pudo preparar el directorio de sesiones: {exc}",
                context={"path": str(self.directory)},
            ) from exc

    # ------------------------------------------------------------------ escritura
    def start(self, *, session_id: str | None = None) -> str:
        """Fija la sesión activa y devuelve su identificador.

        No toca el disco: una sesión que nunca llega a escribir un turno no debe dejar un
        archivo vacío en `sessions/`. El modo privado se aplica al crear el archivo, en el
        primer `append` (abrir en "a" heredaría 0644).
        """
        self.session_id = _SAFE.sub("-", session_id) if session_id else session_id_for(time.time())
        return self.session_id

    def append(self, role: str, content: str, *, at: float | None = None) -> None:
        """Añade un turno. Sin persistencia activada se queda en memoria de la sesión."""
        turn = StoredTurn(role=role, content=content, at=time.time() if at is None else at)
        if not self.enabled or self.session_id is None or not content.strip():
            return
        self._ensure_directory()
        path = self.path_for(self.session_id)
        if not path.exists():
            self._write_lines((), self.session_id)  # nace con 0600; el `open("a")` no decide permisos
        try:
            with path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(turn.as_line()[:_MAX_LINE_BYTES] + "\n")
        except OSError as exc:
            raise FileSystemError(
                f"No se pudo guardar el turno en {path.name}: {exc}",
                context={"path": str(path)},
            ) from exc

    def _write_lines(self, lines: tuple[str, ...], session_id: str) -> Path:
        path = self.path_for(session_id)
        descriptor, temporary = tempfile.mkstemp(dir=str(self.directory), prefix=".sesion-", suffix=".tmp")
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                handle.write("".join(f"{line}\n" for line in lines))
            os.chmod(temporary, _PRIVATE_MODE)
            os.replace(temporary, path)
        except OSError as exc:
            with contextlib.suppress(OSError):
                os.unlink(temporary)
            raise FileSystemError(f"No se pudo escribir {path.name}: {exc}", context={"path": str(path)}) from exc
        return path

    # ------------------------------------------------------------------ lectura
    def history(self, session_id: str | None = None) -> list[dict[str, Any]]:
        """Los turnos en el formato exacto que acepta `Agent.load_history`."""
        return [turn.as_payload() for turn in self.turns(session_id)]

    def turns(self, session_id: str | None = None) -> tuple[StoredTurn, ...]:
        """Turnos de una sesión; una línea final corrupta se ignora, no invalida el resto."""
        wanted = session_id or self.session_id
        if not wanted or not self.enabled:
            # Sin persistencia no hay nada que leer: vacío, no un error (la sesión sigue viva).
            return ()
        path = self.path_for(wanted)
        if not path.is_file():
            raise FileSystemError(f"No hay sesión guardada con ese nombre: {path.name}", context={"path": str(path)})
        collected: list[StoredTurn] = []
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        for index, line in enumerate(lines):
            turn = _parse_line(line)
            if turn is None:
                # Sólo se tolera lo corrupto al final: en medio indica un archivo tocado a mano.
                if index == len(lines) - 1:
                    break
                raise FileSystemError(
                    f"Línea {index + 1} de {path.name} no es un turno válido; la sesión sigue legible a mano",
                    context={"path": str(path), "line": index + 1},
                )
            collected.append(turn)
            if len(collected) >= self.max_turns:
                break
        return tuple(collected)

    def list(self, limit: int = DEFAULT_LIST_LIMIT) -> tuple[SessionInfo, ...]:
        """Sesiones en el disco, de la más reciente a la más antigua."""
        if not self.directory.is_dir():
            return ()
        found: list[SessionInfo] = []
        for candidate in sorted(
            self.directory.glob(f"*{_SUFFIX}"), key=lambda item: item.stat().st_mtime, reverse=True
        ):
            info = _inspect(candidate)
            if info is not None:
                found.append(info)
            if len(found) >= limit:
                break
        return tuple(found)

    def latest_id(self, *, exclude_current: bool = False) -> str | None:
        for info in self.list(limit=2 if exclude_current else 1):
            if not exclude_current or info.session_id != self.session_id:
                return info.session_id
        return None

    def delete(self, session_id: str | None = None) -> str | None:
        """Borra la sesión y devuelve su nombre; `None` si no existía."""
        wanted = session_id or self.session_id
        if not wanted:
            return None
        path = self.path_for(wanted)
        try:
            path.unlink()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise FileSystemError(f"No se pudo borrar {path.name}: {exc}", context={"path": str(path)}) from exc
        if wanted == self.session_id:
            self.session_id = None
        return wanted


def _parse_line(line: str) -> StoredTurn | None:
    text = (line or "").strip()
    if not text:
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    role = str(payload.get("role") or "")
    content = str(payload.get("content") or "")
    if role not in {"user", "assistant"} or not content.strip():
        return None
    raw_at = payload.get("at")
    return StoredTurn(role=role, content=content, at=float(raw_at) if isinstance(raw_at, (int, float)) else 0.0)


def _inspect(path: Path) -> SessionInfo | None:
    try:
        stat = path.stat()
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    turns = sum(1 for line in lines if _parse_line(line) is not None)
    preview = ""
    for line in lines:
        turn = _parse_line(line)
        if turn is not None and turn.role == "user":
            preview = turn.content.replace("\n", " ").strip()[:80]
            break
    return SessionInfo(session_id=path.stem, turns=turns, updated_at=stat.st_mtime, preview=preview or "(vacía)")
