"""Registro de herramientas: adaptadores tipados entre el contrato y las capacidades.

- Un único diccionario `handlers` construido al instanciar; cada entrada es un
  adaptador pequeño (una responsabilidad) que devuelve texto ya acotado al modelo.
- Tipos estructurales (`SearchPort`, `FileSystemPort`, `NotesPort`) en lugar de `Any`.
- Duraciones y desenlaces medidos con `MetricsRegistry`, etiquetados por `ErrorCode`.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Any, Final, cast

from autonoma.observability import MetricsRegistry, measure
from autonoma.ports import FileSystemPort, NotesPort, SearchPort
from autonoma.processes import CommandResult
from autonoma.tool_contracts import LOCAL_TOOLS, tool_names

__all__ = ["ToolHandler", "ToolRegistry", "registered_tool_names"]

logger = logging.getLogger(__name__)

ToolHandler = Callable[[str, Mapping[str, Any], str], str]

_FETCH_PREVIEW_CHARS: Final[int] = 16_000
_LIST_NOTES_LIMIT: Final[int] = 50
_EMPTY_KB_MESSAGE: Final[str] = "knowledge_base vacía"


class ToolRegistry:
    """Despacha herramientas del contrato hacia buscador, almacén de notas y disco."""

    __slots__ = ("_handlers", "fs", "metrics", "notes", "search")

    def __init__(
        self,
        search: SearchPort,
        fs: FileSystemPort,
        notes: NotesPort | None = None,
        *,
        metrics: MetricsRegistry | None = None,
    ) -> None:
        self.search = search
        self.fs = fs
        # `SearchEngine` es fachada del almacén: si no se inyecta uno propio, se usa el suyo.
        self.notes = notes if notes is not None else cast("NotesPort", search)
        self.metrics = metrics
        self._handlers: dict[str, ToolHandler] = {
            "web_search": self._research,
            "fetch_url": self._fetch,
            "save_knowledge": self._save,
            "read_knowledge": self._read,
            "list_knowledge": self._list,
        }
        for name in sorted(LOCAL_TOOLS):
            self._handlers[name] = self._local

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
        # Una descarga no duplica conocimiento: para persistir existe save_knowledge.
        return self.search.fetch_url(str(args["url"]))[:_FETCH_PREVIEW_CHARS]

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
