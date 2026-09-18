"""Puertos tipados: contratos estructurales entre orquestador, herramientas y UI.

Existen para eliminar `Any` de las fronteras sin herencias accidentales: son
`Protocol` (verificación estructural), así que `SearchEngine`, `KnowledgeStore` y
`FileSystemManager` los satisfacen sin importar este módulo. La UI (CLI) también
se tipa contra `ConsolePort`/`Approver`, lo que permite probarla con dobles ligeros.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import Enum
from pathlib import Path
from typing import Any, Final, Literal, Protocol, runtime_checkable

from autonoma.processes import CommandResult

__all__ = [
    "ApprovalDecision",
    "Approver",
    "ConsolePort",
    "EventKind",
    "EventSink",
    "FileSystemPort",
    "NotesPort",
    "ResourcePort",
    "SearchPort",
    "ToolName",
]

ToolName = str
CommandMode = Literal["argv", "shell"]
DEFAULT_MAX_READ_CHARS: Final[int] = 80_000


class EventKind(str, Enum):
    """Eventos de UI emitidos por el orquestador; cadenas estables y cerradas."""

    THINK = "think"
    TOOL = "tool"
    TOOL_RESULT = "tool_result"
    ANSWER = "answer"
    TIMING = "timing"
    PANIC = "panic"
    APPROVAL = "approval"


class ApprovalDecision(str, Enum):
    """Resultado de la aprobación humana, distinguible sin parsear mensajes."""

    GRANTED = "granted"
    DENIED = "denied"
    UNAVAILABLE = "unavailable"


@runtime_checkable
class ResourcePort(Protocol):
    """Cualquier recurso cerrable que el controlador de pánico supervisa."""

    def close(self) -> None: ...


@runtime_checkable
class EventSink(Protocol):
    def __call__(self, kind: EventKind | str, message: str) -> None: ...


@runtime_checkable
class Approver(Protocol):
    def __call__(self, name: ToolName, args: Mapping[str, Any], user_prompt: str = "") -> bool: ...


@runtime_checkable
class NotesPort(Protocol):
    """Almacén de notas: lo que las herramientas `*_knowledge` necesitan."""

    def save_note(self, title: str, body: str, source: str = "agente") -> Path: ...

    def read_note(self, name_or_path: str, max_chars: int = 20_000) -> str: ...

    def search_notes(self, query: str, limit: int = 6) -> str: ...

    def list_notes(self, limit: int = 40) -> list[Path]: ...

    def context_digest(self, limit_files: int = 8, per_file: int = 1800) -> str: ...


@runtime_checkable
class SearchPort(Protocol):
    """Investigación web y extracción de texto."""

    @property
    def knowledge_dir(self) -> Path: ...

    def research(self, query: str, fetch_pages: int | None = None, *, save: bool = True) -> Any: ...

    def format_bundle(self, bundle: Any, preview: int = 900) -> str: ...

    def fetch_url(self, url: str, limit: int = 12_000) -> str: ...

    def shutdown(self) -> None: ...


@runtime_checkable
class ConsolePort(Protocol):
    """Superficie mínima de salida que consume la CLI (Rich o texto simple)."""

    def print(self, *objects: Any, **kwargs: Any) -> None: ...

    def input(self, prompt: str = "") -> str: ...


@runtime_checkable
class FileSystemPort(Protocol):
    """Operaciones locales que requieren aprobación humana explícita."""

    def read_file(self, path: str, max_chars: int = DEFAULT_MAX_READ_CHARS) -> str: ...

    def write_file(self, path: str, content: str, *, force: bool = False) -> str: ...

    def append_file(self, path: str, content: str, *, force: bool = False) -> str: ...

    def copy_path(self, src: str, dst: str, *, force: bool = False) -> str: ...

    def move_path(self, src: str, dst: str, *, force: bool = False) -> str: ...

    def delete_path(self, path: str, *, force: bool = False) -> str: ...

    def list_dir(self, path: str, max_entries: int = 400) -> str: ...

    def mkdir(self, path: str, *, force: bool = False) -> str: ...

    def run_command(
        self,
        command: str | list[str],
        *,
        cwd: str | None = None,
        timeout: float | None = None,
        shell: bool = True,
    ) -> CommandResult: ...

    def format_command_result(self, result: CommandResult) -> str: ...

    def kill_all(self) -> None: ...
