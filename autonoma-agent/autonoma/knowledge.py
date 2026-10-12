"""Almacén de notas: rutas confinadas, lecturas acotadas y búsqueda local tipada.

- `list_notes` usa una sola pasada de `os.scandir` y detecta symlinks/reparse points
  sobre el propio `stat` de la entrada (antes: `is_file` + `lstat` + `stat` por nota).
- `search_notes` deduplica términos y puntúa con conteos C-level, sin releer el
  texto por término ni construir contadores por palabra.
- Errores tipados (`FileSystemError`, `PathPolicyError`) y creación exclusiva.
"""

from __future__ import annotations

import logging
import os
import re
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final
from uuid import uuid4

from autonoma.errors import FileSystemError, PathPolicyError
from autonoma.key_handler import PanicController
from autonoma.path_policy import is_redirected, redirect_from_stat, validate_confined

__all__ = ["KnowledgeStore", "NoteHit"]

logger = logging.getLogger(__name__)

_NOTE_SUFFIXES: Final[frozenset[str]] = frozenset({".md", ".txt"})
_MAX_SCAN_FILES: Final[int] = 200
_MAX_LISTED_FILES: Final[int] = 4_000
_READ_LIMIT_CHARS: Final[int] = 80_000
_TOKEN_RE: Final[re.Pattern[str]] = re.compile(r"\w+", re.UNICODE)
_SLUG_RE: Final[re.Pattern[str]] = re.compile(r"[^\w-]")
_MAX_SNIPPET_CHARS: Final[int] = 2_500
_NOTE_READ_LIMIT: Final[int] = 20_000


def _slug(text: str) -> str:
    return _SLUG_RE.sub("-", text.lower()).strip("-")[:60] or "nota"


def _now_stamp() -> str:
    """Timestamp con sufijo aleatorio: dos notas homónimas no se sobrescriben."""
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S") + "_" + uuid4().hex[:12]


@dataclass(frozen=True, slots=True)
class NoteHit:
    """Coincidencia de búsqueda con su puntaje, ordenable y explicable."""

    path: Path
    score: int
    excerpt: str

    @property
    def name(self) -> str:
        return self.path.name


class KnowledgeStore:
    """Notas en `knowledge_base/`: escrituras exclusivas y lecturas confinadas."""

    def __init__(self, panic: PanicController, knowledge_dir: Path) -> None:
        self.panic = panic
        self.knowledge_dir = Path(knowledge_dir)
        self._ensure_dir()

    def _ensure_dir(self) -> None:
        try:
            self.knowledge_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise FileSystemError(
                f"No se pudo preparar knowledge_base: {exc}",
                context={"path": str(self.knowledge_dir), "error": type(exc).__name__},
            ) from exc

    # ------------------------------------------------------------------ escribir
    @staticmethod
    def _write_exclusive(path: Path, text: str) -> None:
        try:
            with path.open("x", encoding="utf-8") as stream:
                stream.write(text)
        except FileExistsError as exc:
            raise FileSystemError("La nota ya existe; se generó un nombre nuevo para no pisarla") from exc
        except OSError as exc:
            raise FileSystemError(
                f"No se pudo escribir la nota {path.name}: {exc}",
                context={"path": str(path), "error": type(exc).__name__},
            ) from exc

    def save_findings(self, query: str, hits: list[Any], notes: str = "") -> Path:
        self.panic.check()
        self._ensure_dir()
        path = self.knowledge_dir / f"{_now_stamp()}_{_slug(query)}.md"
        blocks = [
            f"# Investigación: {query}",
            "",
            f"- fecha: {datetime.now(timezone.utc).isoformat()}",
            f"- consulta: {query}",
            f"- resultados: {len(hits)}",
            "",
        ]
        if notes:
            blocks += ["## Notas del agente", "", notes.strip(), ""]
        for position, hit in enumerate(hits, 1):
            blocks += [f"## {position}. {hit.title}", "", f"- url: {hit.url}", "", hit.snippet.strip(), ""]
            if hit.extra:
                blocks += [hit.extra.strip(), ""]
            if hit.content:
                blocks += ["### Contenido extraído", "", hit.content.strip(), ""]
        self._write_exclusive(path, "\n".join(blocks).strip() + "\n")
        logger.info(
            "knowledge guardado",
            extra={"event": "knowledge.saved", "fields": {"path": path.name, "hits": len(hits)}},
        )
        return path

    def save_note(self, title: str, body: str, source: str = "agente") -> Path:
        self.panic.check()
        self._ensure_dir()
        path = self.knowledge_dir / f"{_now_stamp()}_{_slug(title)}.md"
        text = f"# {title}\n\n- fecha: {datetime.now(timezone.utc).isoformat()}\n- fuente: {source}\n\n{body.strip()}\n"
        self._write_exclusive(path, text)
        return path

    # -------------------------------------------------------------------- leer
    def _scan_notes(self) -> list[tuple[Path, float]]:
        """`scandir` con un solo stat por entrada; el stat ya revela symlinks y reparse points."""
        try:
            with os.scandir(self.knowledge_dir) as entries:
                observed: list[tuple[Path, float]] = []
                for entry in entries:
                    if _suffix(entry.name) not in _NOTE_SUFFIXES:
                        continue
                    try:
                        info = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    if not _is_plain_note(info):
                        continue
                    observed.append((Path(entry.path), info.st_mtime))
                return observed
        except OSError as exc:
            logger.debug(
                "knowledge_base no legible",
                extra={"event": "knowledge.scan_error", "fields": {"error": type(exc).__name__}},
            )
            return []

    def list_notes(self, limit: int = 40) -> list[Path]:
        """Notas más recientes primero; ordena en memoria sin stat adicional."""
        observed = self._scan_notes()[:_MAX_LISTED_FILES]
        observed.sort(key=lambda item: item[1], reverse=True)
        return [path for path, _mtime in observed[:limit]]

    def _read_limited(self, path: Path, limit: int = _READ_LIMIT_CHARS) -> str:
        try:
            with path.open(encoding="utf-8", errors="replace") as stream:
                return stream.read(limit)
        except OSError:
            return ""

    def read_note(self, name_or_path: str, max_chars: int = _NOTE_READ_LIMIT) -> str:
        candidate = self._confined_path(name_or_path)
        if candidate is None or not candidate.is_file():
            matches = self._match_by_name(name_or_path)
            if not matches:
                raise FileNotFoundError(f"No hay nota que coincida con {name_or_path!r}")
            candidate = matches[0]
        data = self._read_limited(candidate, max_chars + 1)
        if len(data) > max_chars:
            return data[:max_chars] + "\n\n…[truncado]"
        return f"# archivo: {candidate.name}\n\n{data}"

    def _confined_path(self, name_or_path: str) -> Path | None:
        """Sólo rutas dentro de `knowledge_base`, sin seguir symlinks hacia fuera."""
        base = self.knowledge_dir.absolute()
        candidate = base / name_or_path
        if is_redirected(candidate):
            raise PathPolicyError("Las notas enlazadas simbólicamente no se leen")
        return validate_confined(candidate, base, label="nota")

    def _match_by_name(self, needle: str) -> list[Path]:
        lowered = needle.lower()
        return [path for path in self.list_notes(_MAX_SCAN_FILES) if lowered in path.name.lower()]

    def context_digest(self, limit_files: int = 8, per_file: int = 1800) -> str:
        """Resumen del knowledge_base para inyectar como contexto (datos, no instrucciones)."""
        files = self.list_notes(limit_files)
        if not files:
            return "(knowledge_base vacía)"
        chunks = ["Notas recientes en knowledge_base:"]
        for path in files:
            text = self._read_limited(path)
            if text:
                chunks.append(f"\n--- {path.name} ---\n{text[:per_file]}")
        return "\n".join(chunks)

    def search_notes(self, query: str, limit: int = 6) -> str:
        """Coincidencias por recuento de términos; sin lecturas ilimitadas."""
        scored = self.score_notes(query)
        if not scored:
            return f"Sin coincidencias en knowledge_base para {query!r}"
        return "\n\n".join(f"### {hit.name} (score={hit.score})\n{hit.excerpt}" for hit in scored[:limit])

    def score_notes(self, query: str, limit_files: int = _MAX_SCAN_FILES) -> list[NoteHit]:
        """O(texto total) con un conteo por término único; el orden es determinista."""
        tokens = tuple(dict.fromkeys(token.lower() for token in _TOKEN_RE.findall(query)))
        hits: list[NoteHit] = []
        for path in self.list_notes(limit_files):
            self.panic.check()
            text = self._read_limited(path)
            blob = f"{path.name}\n{text}".lower()
            score = sum(blob.count(token) for token in tokens) if tokens else 1
            if score:
                hits.append(NoteHit(path=path, score=score, excerpt=text[:_MAX_SNIPPET_CHARS]))
        hits.sort(key=lambda hit: (-hit.score, hit.name))
        return hits


def _suffix(name: str) -> str:
    dot = name.rfind(".")
    return name[dot:].lower() if dot > 0 else ""


def _is_plain_note(info: os.stat_result) -> bool:
    """Regular, no symlink y sin reparse point: un solo stat decide las tres cosas."""
    if not stat.S_ISREG(info.st_mode):
        return False
    return not redirect_from_stat(info)
