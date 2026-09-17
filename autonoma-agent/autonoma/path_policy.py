"""Política conservadora de rutas y reparse points, sin prometer una sandbox."""
from __future__ import annotations
import stat
from pathlib import Path, PureWindowsPath


def is_redirected(path: Path) -> bool:
    """Incluye symlinks Unix y cualquier reparse point de Windows (junctions)."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)
    )


def has_redirected_component(path: Path) -> bool:
    absolute = path.expanduser().absolute()
    return any(is_redirected(part) for part in (absolute, *absolute.parents))


def validate_windows_path(raw: str) -> None:
    """Rechazar dispositivos/ADS y rutas ambiguas antes de resolverlas."""
    path = PureWindowsPath(raw)
    if raw.startswith(('\\\\?\\', '\\\\.\\')):
        raise ValueError('Rutas de dispositivo Windows no permitidas')
    if (path.drive and not path.is_absolute()) or (path.root and not path.drive):
        raise ValueError('Usa una ruta absoluta de unidad, por ejemplo C:\\carpeta\\archivo')
    parts = path.parts[1:] if path.drive else path.parts
    if any(PureWindowsPath(part).is_reserved() or ':' in part for part in parts):
        raise ValueError('Dispositivos reservados y alternate data streams no permitidos')
    if any(part not in {'.', '..', '\\'} and part.endswith((' ', '.')) for part in parts):
        raise ValueError('Ruta Windows ambigua: componente termina en punto o espacio')
