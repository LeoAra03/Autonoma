"""Registro de herramientas: adaptadores tipados entre el contrato y las capacidades.

- Un único diccionario `handlers` construido al instanciar; cada entrada es un
  adaptador pequeño (una responsabilidad) que devuelve texto ya acotado al modelo.
- Tipos estructurales (`SearchPort`, `FileSystemPort`, `NotesPort`) en lugar de `Any`.
- Duraciones y desenlaces medidos con `MetricsRegistry`, etiquetados por `ErrorCode`.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Mapping
from typing import Any, Final, cast

from autonoma.errors import ConfigurationError
from autonoma.observability import MetricsRegistry, measure
from autonoma.ports import FileSystemPort, JobsPort, NotesPort, SearchPort
from autonoma.processes import CommandResult
from autonoma.tool_contracts import LOCAL_TOOLS, tool_names

__all__ = ["ToolHandler", "ToolRegistry", "registered_tool_names"]

logger = logging.getLogger(__name__)

ToolHandler = Callable[[str, Mapping[str, Any], str], str]

_FETCH_PREVIEW_CHARS: Final[int] = 16_000
_NOTE_TITLE_CHARS: Final[int] = 60
_LIST_NOTES_LIMIT: Final[int] = 50
_EMPTY_KB_MESSAGE: Final[str] = "knowledge_base vacía"


def note_title(url: str) -> str:
    """Título de nota a partir del destino, sin convertir la URL en algo ilegible."""
    from urllib.parse import urlsplit

    parsed = urlsplit(url)
    tail = parsed.path.rstrip("/").rsplit("/", 1)[-1] if parsed.path else ""
    stem = re.sub(r"\.(html?|php|aspx?)$", "", tail, flags=re.IGNORECASE).strip()
    label = f"{parsed.netloc} {stem}".strip() if stem else (parsed.netloc or url)
    cleaned = re.sub(r"[\s_/]+", "-", label, flags=re.IGNORECASE).strip("-")
    return (cleaned[:_NOTE_TITLE_CHARS] or "descarga").rstrip("-.")


class ToolRegistry:
    """Despacha herramientas del contrato hacia buscador, almacén de notas y disco."""

    __slots__ = ("_handlers", "fs", "jobs", "metrics", "notes", "search")

    def __init__(
        self,
        search: SearchPort,
        fs: FileSystemPort,
        notes: NotesPort | None = None,
        *,
        metrics: MetricsRegistry | None = None,
        jobs: JobsPort | None = None,
    ) -> None:
        self.search = search
        self.fs = fs
        # `SearchEngine` es fachada del almacén: si no se inyecta uno propio, se usa el suyo.
        self.notes = notes if notes is not None else cast("NotesPort", search)
        self.metrics = metrics
        self.jobs = jobs
        self._handlers: dict[str, ToolHandler] = {
            "web_search": self._research,
            "fetch_url": self._fetch,
            "save_knowledge": self._save,
            "read_knowledge": self._read,
            "list_knowledge": self._list,
            "spawn_command": self._spawn_job,
            "job_status": self._job_status,
            "job_output": self._job_output,
            "kill_job": self._kill_job,
        }
        for name in sorted(LOCAL_TOOLS):
            # `setdefault`, no asignación: `spawn_command` y `kill_job` son locales (piden `SI`)
            # pero viven en el gestor de trabajos, no en `FileSystemManager`.
            self._handlers.setdefault(name, self._local)

    # ------------------------------------------------------------------ público
    @property
    def handlers(self) -> Mapping[str, ToolHandler]:
        return self._handlers

    @property
    def tool_names(self) -> frozenset[str]:
        return frozenset(self._handlers)

    def execute(self, name: str, args: Mapping[str, Any], *, user_prompt: str = "") -> str:
        """Ejecuta una herramienta ya validada; el fallo se propaga tipado al agente."""
        handler = self._handlers.get(name)
        if handler is None:
            raise KeyError(name)
        with measure(
            self.metrics,
            f"tool.{name}",
            logger=logger,
            event_end=f"tool.{name}.done",
            fields={"tool": name},
        ):
            return handler(name, args, user_prompt)

    # ---------------------------------------------------------------- handlers
    def _research(self, name: str, args: Mapping[str, Any], prompt: str) -> str:
        raw_pages = args.get("fetch_pages")
        fetch_pages = int(raw_pages) if isinstance(raw_pages, (int, float)) else None
        bundle = self.search.research(str(args["query"]), fetch_pages=fetch_pages, save=True)
        return self.search.format_bundle(bundle)

    def _fetch(self, name: str, args: Mapping[str, Any], prompt: str) -> str:
        raw_limit = args.get("max_chars")
        limit = int(raw_limit) if isinstance(raw_limit, (int, float)) else _FETCH_PREVIEW_CHARS
        start = int(args.get("start_char") or 0)
        url = str(args["url"])
        text = self.search.fetch_url(url, limit, start=start)
        if not args.get("save"):
            # Una descarga no duplica conocimiento por sí sola: `save=true` decide lo contrario.
            return text
        title = str(args.get("title") or "").strip() or note_title(url)
        path = self.notes.save_note(title, text, source=url)
        return f"Guardado {path.name} ({len(text)} caracteres leídos de {len(text)} devueltos)"

    def _save(self, name: str, args: Mapping[str, Any], prompt: str) -> str:
        path = self.notes.save_note(str(args["title"]), str(args["content"]))
        return f"Guardado {path.name}"

    def _read(self, name: str, args: Mapping[str, Any], prompt: str) -> str:
        try:
            return self.notes.read_note(str(args["query"]))
        except FileNotFoundError:
            return self.notes.search_notes(str(args["query"]))

    def _list(self, name: str, args: Mapping[str, Any], prompt: str) -> str:
        files = self.notes.list_notes(_LIST_NOTES_LIMIT)
        return "\n".join(f"- {path.name}" for path in files) or _EMPTY_KB_MESSAGE

    # ------------------------------------------------------------------ trabajos
    def _runner(self) -> JobsPort:
        if self.jobs is None:
            raise ConfigurationError(
                "Los trabajos en segundo plano no están disponibles en esta sesión; "
                "revisa que el directorio de datos sea escribible"
            )
        return self.jobs

    def _spawn_job(self, name: str, args: Mapping[str, Any], prompt: str) -> str:
        raw_cwd = args.get("cwd")
        info = self._runner().spawn(str(args["command"]), str(raw_cwd) if raw_cwd else None)
        return str(info.summary())

    def _job_status(self, name: str, args: Mapping[str, Any], prompt: str) -> str:
        raw = args.get("job_id")
        return self._runner().status(str(raw) if raw else None)

    def _job_output(self, name: str, args: Mapping[str, Any], prompt: str) -> str:
        raw = args.get("job_id")
        raw_chars = args.get("max_chars")
        return self._runner().output(
            str(raw) if raw else None,
            int(raw_chars) if isinstance(raw_chars, (int, float)) else None,
            tail=bool(args.get("tail", True)),
        )

    def _kill_job(self, name: str, args: Mapping[str, Any], prompt: str) -> str:
        if not args.get("confirm"):
            raise ConfigurationError("kill_job exige confirm=true: matar procesos no se hace por defecto")
        return self._runner().kill(str(args["job_id"]))

    def _local(self, name: str, args: Mapping[str, Any], prompt: str) -> str:
        method = getattr(self.fs, name, None)
        if method is None:
            raise AttributeError(f"El gestor de archivos no expone {name}")
        result: Any = method(**dict(args))
        if isinstance(result, CommandResult):
            return result.format_for_model()
        return str(result)


def registered_tool_names() -> frozenset[str]:
    """Nombres anunciados al modelo; se contrastan con el contrato en las pruebas."""
    return frozenset(tool_names())
