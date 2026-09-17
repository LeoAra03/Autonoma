"""Presentación segura y previsualización local, independiente de Rich."""
from __future__ import annotations
import difflib
import os
import unicodedata
from pathlib import Path
from typing import Any
from autonoma.path_policy import has_redirected_component


def safe_text(value: str) -> str:
    """Representar controles (incluidos ESC/Bidi) sin ejecutarlos en la terminal."""
    return ''.join(
        f'\\u{ord(char):04x}' if unicodedata.category(char).startswith('C') and char not in '\n\t' else char
        for char in value
    )


def operation_preview(name: str, args: dict[str, Any]) -> str:
    if name == 'run_command':
        return ('ALTO RIESGO: shell sin aislamiento (' + ('cmd.exe' if os.name == 'nt' else 'POSIX') +
                '). Puede acceder al disco y a la red con tus permisos. No hay elevación automática.')
    if name == 'read_file':
        return 'PRIVACIDAD: el contenido leído podrá enviarse al proveedor del modelo.'
    if name in {'delete_path', 'move_path'}:
        return 'DESTRUCTIVO: no hay papelera ni deshacer automático. Revisa origen y destino.'
    if name != 'write_file':
        return 'Revisa las rutas; esta aprobación solo vale para la operación mostrada.'
    path = Path(args['path']).expanduser()
    try:
        if has_redirected_component(path):
            return 'Destino simbólico: no se muestra diff; revisa el destino real antes de autorizar.'
        if path.exists():
            if not path.is_file() or path.stat().st_size > 256_000:
                return 'Archivo grande o no regular: diff omitido; sobrescritura potencial.'
            with path.open(encoding='utf-8', errors='strict') as stream:
                old = stream.read(80_001)
            if len(old) > 80_000:
                return 'Archivo grande: diff omitido; sobrescritura potencial.'
        else:
            old = ''
        lines = list(difflib.unified_diff(old.splitlines(), args['content'].splitlines(),
                                         fromfile='actual', tofile='propuesto', lineterm=''))
        preview = '\n'.join(lines[:100])[:12_000]
        suffix = '\n[Diff parcial; los argumentos completos se muestran arriba.]' if len(lines) > 100 or len('\n'.join(lines)) > 12_000 else ''
        return safe_text(preview + suffix) or 'Sin cambios de texto visibles.'
    except (OSError, UnicodeError):
        return 'No se pudo obtener diff de texto; revisa el archivo antes de sobrescribir.'
