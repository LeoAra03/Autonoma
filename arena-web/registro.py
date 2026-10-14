"""El expediente: una linea JSON por cosa que pasa.

Si algún día alguien pregunta qué hizo el bot, la respuesta está aquí: qué prompt se
envió, cuánto tardó, qué respondió (extracto) y cómo terminó. Se añade sin releer, así
que un corte de luz sólo se lleva la última linea.
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

__all__ = ["registrar", "ruta_log"]

_EXTRACTO: int = 300


def ruta_log(directorio: Path, archivo: str) -> Path:
    """Crea el directorio si hace falta y devuelve la ruta del archivo."""
    directorio.mkdir(parents=True, exist_ok=True)
    return directorio / archivo


def registrar(ruta: Path, evento: str, **campos: Any) -> None:
    """Añade una linea. Nunca lanza: un log que falla no puede tumbar el bot."""
    linea: dict[str, Any] = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "evento": evento,
    }
    for clave, valor in campos.items():
        if isinstance(valor, Mapping):
            linea[clave] = dict(valor)
        else:
            linea[clave] = valor
    try:
        ruta.parent.mkdir(parents=True, exist_ok=True)
        with ruta.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(linea, ensure_ascii=False) + "\n")
    except OSError:
        pass


def extracto(texto: str, largo: int = _EXTRACTO) -> str:
    """Un trozo legible del texto, para el log y la pantalla."""
    plano = " ".join(str(texto).split())
    return plano if len(plano) <= largo else plano[:largo] + "…"
