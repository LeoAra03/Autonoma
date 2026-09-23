"""Texto sobre archivos: paginación, edición quirúrgica y búsqueda en árbol.

Módulo deliberadamente **puro**: aquí no hay política de rutas ni escritura a disco, así
que se prueba sin sistema de archivos. `FileSystemManager` aplica sus protecciones y luego
llama; el resultado de cada operación es un tipo congelado con el resumen que ve el modelo.

Por qué existe: sin un `edit_file` el agente sólo sabía reescribir archivos enteros, lo que
hace caro y arriesgado cualquier cambio pequeño (y directamente imposible en archivos
grandes). La edición por coincidencia única es la operación que un desarrollador espera.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Final, NoReturn

from autonoma.errors import FileSystemError

__all__ = [
    "EditOutcome",
    "SearchOutcome",
    "apply_edit",
    "grep_tree",
    "read_window",
    "split_lines",
]

#: Tope de lectura para editar o buscar: un archivo de más de este tamaño no se toca a ciegas.
MAX_SCANNED_BYTES: Final[int] = 8_000_000
#: Por archivo en `grep_tree`: los artefactos binarios gigantes no se leen para buscar texto.
DEFAULT_SEARCH_FILE_BYTES: Final[int] = 2_000_000
DEFAULT_MAX_RESULTS: Final[int] = 40
DEFAULT_MAX_LINES: Final[int] = 400
DEFAULT_READ_CHARS: Final[int] = 80_000

_LINE_PREVIEW_CHARS: Final[int] = 220
_TRUNCATION_NOTE: Final[str] = "\n\n…[truncado: el archivo sigue después de {limit} caracteres]"
_SCAN_TRUNCATION_NOTE: Final[str] = "\n\n…[archivo truncado a {limit} bytes]"
_SKIP_DIRECTORIES: Final[frozenset[str]] = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".nox",
        "dist",
        "build",
        ".next",
        ".nuxt",
        ".output",
        "site-packages",
    }
)


@dataclass(frozen=True, slots=True)
class EditOutcome:
    """Qué cambió, en términos que el modelo pueda citar sin volver a leer el archivo."""

    target: str
    replacements: int
    first_line: int
    line_delta: int
    bytes_written: int

    def summary(self) -> str:
        where = (
            f"línea {self.first_line}"
            if self.replacements == 1
            else f"{self.replacements} sitios desde la línea {self.first_line}"
        )
        delta = f"{self.line_delta:+d}" if self.line_delta else "0"
        return f"Editado {self.target}: {where}; líneas {delta}; {self.bytes_written} B escritos"


@dataclass(frozen=True, slots=True)
class SearchOutcome:
    """Coincidencias ya formateadas, con lo que quedó fuera del recuento."""

    matches: tuple[str, ...]
    files_scanned: int
    files_visited: int
    truncated: bool
    skipped_binary: int

    def summary(self, pattern: str, root: str) -> str:
        hits = len(self.matches)
        head = f"{hits} coincidencia{'s' if hits != 1 else ''} de {pattern!r} bajo {root}"
        if self.truncated:
            head += f" (cortado en {len(self.matches)}; amplía max_results)"
        if self.skipped_binary:
            head += f"; {self.skipped_binary} archivos binarios omitidos"
        head += f" de {self.files_visited} archivos leídos"
        return "\n".join((head, *self.matches)) if self.matches else f"{head}: sin coincidencias"


def _fail(message: str, **context: object) -> NoReturn:
    """Un solo camino de error: siempre `FileSystemError` con contexto accionable."""
    raise FileSystemError(message, context=dict(context))


def split_lines(text: str) -> list[str]:
    return text.splitlines()


def read_window(
    text: str,
    *,
    start_line: int = 1,
    max_lines: int = 0,
    max_chars: int = DEFAULT_READ_CHARS,
    path: str = "",
) -> str:
    """Ventana de líneas (1-based) si `max_lines` > 0; si no, corte por caracteres.

    El camino por caracteres se mantiene idéntico al de siempre para no romper a quien
    ya consumía `read_file`; la paginación por líneas añade una cabecera con el rango,
    porque sin ella el modelo no sabe si está viendo el principio del archivo.
    """
    if max_lines <= 0:
        if start_line > 1:
            lines = split_lines(text)
            if start_line > len(lines):
                _fail(
                    "start_line queda más allá del final del archivo",
                    path=path,
                    start_line=start_line,
                    line_count=len(lines),
                )
            return "\n".join(lines[start_line - 1 :])
        return text if len(text) <= max_chars else text[:max_chars] + _TRUNCATION_NOTE.format(limit=max_chars)
    lines = split_lines(text)
    if start_line > len(lines):
        _fail(
            "start_line queda más allá del final del archivo",
            path=path,
            start_line=start_line,
            line_count=len(lines),
        )
    end = min(len(lines), start_line - 1 + max_lines)
    window = lines[start_line - 1 : end]
    body = "\n".join(window)
    header = f"{path or 'archivo'}:{start_line}-{end} de {len(lines)} líneas"
    note = _SCAN_TRUNCATION_NOTE.format(limit=MAX_SCANNED_BYTES) if _ends_mid_file(text) else ""
    return f"{header}\n{body}{note}"


def _ends_mid_file(text: str) -> bool:
    """Señal débil de que el texto leído no era el archivo completo."""
    return len(text) >= MAX_SCANNED_BYTES


def apply_edit(text: str, find: str, replace: str, *, all_occurrences: bool, path: str = "") -> tuple[str, EditOutcome]:
    """Sustituye `find` por `replace` exigiendo coincidencias inequívocas.

    El rechazo explícito de "varias coincidencias sin `all`" es el punto: una edición
    ambigua aplicada a medias deja un archivo que compila y no hace lo que se pidió.
    """
    if not find:
        _fail("`find` no puede estar vacío", path=path)
    if find == replace:
        _fail("No hay cambio: `find` y `replace` son idénticos", path=path)
    occurrences = text.count(find)
    if occurrences == 0:
        _fail(
            "Texto no encontrado: el archivo quedó intacto",
            path=path,
            buscado=find[:120],
            sugerencia="amplía el contexto de `find` o revisa `read_file` con start_line",
        )
    if occurrences > 1 and not all_occurrences:
        _fail(
            f"{occurrences} coincidencias; la edición sería ambigua",
            path=path,
            sugerencia="usa all=true para reemplazarlas todas o añade contexto a `find`",
        )
    updated = text.replace(find, replace) if all_occurrences else text.replace(find, replace, 1)
    first_line = text[: text.find(find)].count("\n") + 1
    return updated, EditOutcome(
        target=path or "archivo",
        replacements=occurrences if all_occurrences else 1,
        first_line=first_line,
        line_delta=updated.count("\n") - text.count("\n"),
        bytes_written=len(updated.encode("utf-8")),
    )


@lru_cache(maxsize=32)
def _glob_regex(glob: str) -> re.Pattern[str]:
    """Traduce un glob a regex con la semántica de `Path.glob`, no la de `fnmatch`.

    La diferencia importa: `fnmatch` trata `*` como "cualquier cosa, incluida `/`" y
    `**/*` **no** casa con `README.md`, que es el primer archivo que quien busca espera ver.
    Aquí `**/` significa "cero o más segmentos", igual que en `pathlib`.
    """
    out: list[str] = ["(?s)"]
    index, size = 0, len(glob)
    while index < size:
        char = glob[index]
        if char == "*":
            if glob.startswith("**", index):
                index += 2
                if glob.startswith("/", index):
                    index += 1
                out.append("(?:[^/]*/)*")
            else:
                index += 1
                out.append("[^/]*")
        elif char == "?":
            index += 1
            out.append("[^/]")
        elif char == "[":
            closing = glob.find("]", index + 1)
            if closing < 0:
                index += 1
                out.append(re.escape(char))
            else:
                clause = glob[index : closing + 1]
                out.append("[^" + clause[2:-1] + "]" if clause.startswith("[!") else clause)
                index = closing + 1
        else:
            out.append(re.escape(char))
            index += 1
    out.append(r"\Z")
    return re.compile("".join(out))


def _glob_match(relative: str, glob: str) -> bool:
    return bool(_glob_regex(glob).match(relative))


def _walk_text_files(
    root: Path,
    *,
    glob: str,
    max_file_bytes: int,
    tick: Callable[[], None] | None,
) -> Iterator[tuple[Path, str | None]]:
    """Archivos candidatos: sin symlinks, sin directorios de artefactos, sin gigantes.

    Produce `(ruta, error)` —`error` no `None` cuando el archivo se omitió por binario o
    ilegible, para poder contarlo sin callarlo.
    """
    if root.is_file():
        yield root, _oversized(root, max_file_bytes)
        return
    for parent, directories, files in os.walk(root, followlinks=False):
        if tick is not None:
            tick()
        directories[:] = sorted(
            name for name in directories if name not in _SKIP_DIRECTORIES and not _is_link(Path(parent) / name)
        )
        for name in sorted(files):
            candidate = Path(parent) / name
            if _is_link(candidate) or not _glob_match(_relative(candidate, root), glob):
                continue
            if tick is not None:
                tick()
            yield candidate, _oversized(candidate, max_file_bytes)


def _relative(candidate: Path, root: Path) -> str:
    try:
        return candidate.relative_to(root).as_posix()
    except ValueError:
        return candidate.name


def _is_link(path: Path) -> bool:
    try:
        return path.is_symlink()
    except OSError:
        return True


def _oversized(candidate: Path, max_file_bytes: int) -> str | None:
    try:
        if candidate.stat().st_size > max_file_bytes:
            return "demasiado grande"
    except OSError:
        return "ilegible"
    return None


def grep_tree(
    root: Path,
    pattern: str,
    *,
    glob: str = "**/*",
    max_results: int = DEFAULT_MAX_RESULTS,
    max_file_bytes: int = DEFAULT_SEARCH_FILE_BYTES,
    ignore_case: bool = True,
    tick: Callable[[], None] | None = None,
) -> SearchOutcome:
    """Búsqueda tipo `grep -n` sobre texto, con el mismo límite duro que la edición."""
    try:
        compiled = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
    except re.error as exc:
        _fail(f"Patrón inválido: {exc}", pattern=pattern[:120])
    matches: list[str] = []
    visited = 0
    skipped = 0
    truncated = False
    for candidate, skipped_reason in _walk_text_files(root, glob=glob, max_file_bytes=max_file_bytes, tick=tick):
        visited += 1
        if skipped_reason is not None:
            skipped += 1
            continue
        try:
            raw = candidate.read_bytes()[:MAX_SCANNED_BYTES]
        except OSError:
            skipped += 1
            continue
        if b"\x00" in raw:
            skipped += 1
            continue
        text = raw.decode("utf-8", errors="replace")
        for number, line in enumerate(split_lines(text), start=1):
            if compiled.search(line):
                matches.append(f"{_relative(candidate, root)}:{number}: {line.strip()[:_LINE_PREVIEW_CHARS]}")
                if len(matches) >= max_results:
                    truncated = True
                    break
        if truncated:
            break
    return SearchOutcome(
        matches=tuple(matches),
        files_scanned=visited - skipped,
        files_visited=visited,
        truncated=truncated,
        skipped_binary=skipped,
    )
