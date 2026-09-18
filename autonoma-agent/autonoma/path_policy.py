"""Política conservadora de rutas y reparse points; no promete una sandbox.

Devuelve errores tipados (`PathPolicyError`) con el motivo exacto, para que la UI
y los logs distingan "ruta mal formada" de "ruta protegida".
"""

from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from pathlib import Path, PureWindowsPath
from typing import Final

from autonoma.errors import PathPolicyError

__all__ = [
    "has_redirected_component",
    "is_redirected",
    "redirect_from_stat",
    "validate_confined",
    "validate_windows_path",
]

_REPARSE_ATTRIBUTE: Final[int] = 0x400
_FORBIDDEN_PREFIXES: Final[tuple[str, ...]] = ("\\\\?\\", "\\\\.\\")
_AMBIGUOUS_TRAILERS: Final[tuple[str, ...]] = (" ", ".")
_RELATIVE_PARTS: Final[frozenset[str]] = frozenset({".", "..", "\\"})


def _reparse_flag() -> int:
    return int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", _REPARSE_ATTRIBUTE))


def is_redirected(path: Path) -> bool:
    """Incluye symlinks Unix y cualquier reparse point de Windows (junctions)."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    # Otros fallos de `lstat` (permisos) se propagan: asumir "seguro" sería silenciar
    # una condición que el llamador sí puede reportar al usuario.
    return redirect_from_stat(info)


def _components(absolute: Path) -> Iterator[Path]:
    yield absolute
    yield from absolute.parents


def has_redirected_component(path: Path) -> bool:
    """Recorre el nodo y sus ancestros; corta en el primer redireccionamiento."""
    return any(is_redirected(component) for component in _components(path.expanduser().absolute()))


def validate_windows_path(raw: str) -> None:
    """Rechaza dispositivos, ADS y rutas ambiguas antes de resolverlas."""
    if not raw:
        raise PathPolicyError("Ruta vacía")
    path = PureWindowsPath(raw)
    if raw.startswith(_FORBIDDEN_PREFIXES):
        raise PathPolicyError("Rutas de dispositivo Windows no permitidas", context={"prefix": raw[:4]})
    if (path.drive and not path.is_absolute()) or (path.root and not path.drive):
        raise PathPolicyError("Usa una ruta absoluta de unidad, por ejemplo C:\\carpeta\\archivo")
    parts = path.parts[1:] if path.drive else path.parts
    if any(PureWindowsPath(part).is_reserved() or ":" in part for part in parts):
        raise PathPolicyError("Dispositivos reservados y alternate data streams no permitidos")
    if any(part not in _RELATIVE_PARTS and part.endswith(_AMBIGUOUS_TRAILERS) for part in parts):
        raise PathPolicyError("Ruta Windows ambigua: componente termina en punto o espacio")


def redirect_from_stat(info: os.stat_result) -> bool:
    """Detecta redirecciones a partir de un `stat` ya obtenido (evita lstat extra)."""
    return bool(stat.S_ISLNK(info.st_mode)) or bool(getattr(info, "st_file_attributes", 0) & _reparse_flag())


def validate_confined(candidate: Path, base: Path, *, label: str = "ruta") -> Path:
    """Resuelve y exige que `candidate` siga dentro de `base` tras resolver enlaces."""
    resolved = candidate.resolve()
    if not resolved.is_relative_to(base):
        raise PathPolicyError(
            f"La {label} debe estar dentro de {base.name}",
            context={"label": label},
        )
    return resolved
