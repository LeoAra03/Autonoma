"""Presentación segura y previsualización local, independiente de Rich.

Aquí sólo hay *presentación*: nunca se ejecuta nada, se lee el mínimo necesario
para el diff y cualquier carácter de control se muestra escapado. Cada tipo de
operación tiene su propio helper (funciones cortas, un solo motivo de cambio).
"""

from __future__ import annotations

import difflib
import os
import unicodedata
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

from autonoma.path_policy import has_redirected_component

__all__ = ["approval_heading", "operation_preview", "safe_text"]

_CONTROL_SAFE: Final[str] = "\n\t"
_DIFF_CONTEXT_LINES: Final[int] = 100
_DIFF_MAX_CHARS: Final[int] = 12_000
_DIFF_READ_CHARS: Final[int] = 80_000
_LARGE_FILE_BYTES: Final[int] = 256_000
_INTENT_PREVIEW_CHARS: Final[int] = 240
_SHELL_LABELS: Final[Mapping[str, str]] = {"nt": "cmd.exe", "posix": "POSIX"}


def safe_text(value: str) -> str:
    """Representa controles (ESC, Bidi, BEL…) sin ejecutarlos en la terminal."""
    return "".join(
        f"\\u{ord(char):04x}"
        if _is_control_char(char)
        else char
        for char in value
    )


def _is_control_char(char: str) -> bool:
    """Categoría C* de Unicode: controles, separadores y formatos (incl. bidi)."""
    return unicodedata.category(char).startswith("C") and char not in _CONTROL_SAFE


def shell_label() -> str:
    """Nombre real del intérprete que se usará, sin prometer PowerShell implícito."""
    return "cmd.exe; PowerShell requiere invocación explícita" if os.name == "nt" else "shell POSIX"


def operation_preview(name: str, args: Mapping[str, Any]) -> str:
    """Advertión proporcional al impacto, antes de pedir aprobación."""
    if name == "run_command":
        return _run_command_preview()
    if name == "read_file":
        return "PRIVACIDAD: el contenido leído podrá enviarse al proveedor del modelo."
    if name in {"delete_path", "move_path"}:
        return "DESTRUCTIVO: no hay papelera ni deshacer automático. Revisa origen y destino."
    if name == "write_file":
        return _write_file_preview(args)
    if name in {"copy_path", "mkdir", "list_dir"}:
        return "Revisa las rutas; esta aprobación solo vale para la operación mostrada."
    return "Revisa los argumentos; esta aprobación solo vale para la operación mostrada."


def _run_command_preview() -> str:
    interpreter = _SHELL_LABELS.get(os.name, "shell")
    return (
        f"ALTO RIESGO: shell sin aislamiento ({interpreter}). Puede acceder al disco y a la red "
        "con tus permisos. No hay elevación automática."
    )


def _write_file_preview(args: Mapping[str, Any]) -> str:
    raw_path = args.get("path")
    content = args.get("content")
    if not isinstance(raw_path, str) or not isinstance(content, str):
        return "Faltan datos para calcular el diff; revisa los argumentos antes de autorizar."
    path = Path(raw_path).expanduser()
    try:
        if has_redirected_component(path):
            return "Destino simbólico: no se muestra diff; revisa el destino real antes de autorizar."
        original = _read_for_diff(path)
        if original is None:
            return "Archivo grande o no regular: diff omitido; sobrescritura potencial."
    except (OSError, UnicodeError):
        return "No se pudo obtener diff de texto; revisa el archivo antes de sobrescribir."
    return _diff_message(original, content)


def _read_for_diff(path: Path) -> str | None:
    """Devuelve `None` si el archivo es grande o no es regular; `''` si no existe."""
    if not path.exists():
        return ""
    if not path.is_file() or path.stat().st_size > _LARGE_FILE_BYTES:
        return None
    with path.open(encoding="utf-8", errors="strict") as stream:
        content = stream.read(_DIFF_READ_CHARS + 1)
    return None if len(content) > _DIFF_READ_CHARS else content


def _diff_message(original: str, proposed: str) -> str:
    lines = list(
        difflib.unified_diff(
            original.splitlines(),
            proposed.splitlines(),
            fromfile="actual",
            tofile="propuesto",
            lineterm="",
        )
    )
    body = "\n".join(lines[:_DIFF_CONTEXT_LINES])
    truncated = len(lines) > _DIFF_CONTEXT_LINES or len(body) > _DIFF_MAX_CHARS
    preview = "\n".join(lines[:_DIFF_CONTEXT_LINES])[:_DIFF_MAX_CHARS]
    suffix = "\n[Diff parcial; los argumentos completos se muestran arriba.]" if truncated else ""
    return safe_text(preview + suffix) or "Sin cambios de texto visibles."


def approval_heading(name: str, user_prompt: str = "") -> str:
    """Contexto de la aprobación: qué pide el modelo frente a qué pidió el usuario.

    Se imprime la instrucción recortada para que la persona pueda comparar intención
    y acción; los argumentos completos se muestran aparte, sin recortar.
    """
    intent = (user_prompt or "").replace("\n", " ").strip()
    if not intent:
        return f"Operación local: {name}"
    clipped = intent[:_INTENT_PREVIEW_CHARS] + ("…" if len(intent) > _INTENT_PREVIEW_CHARS else "")
    return f"Operación local: {name}\nTu instrucción: {safe_text(clipped)}"
