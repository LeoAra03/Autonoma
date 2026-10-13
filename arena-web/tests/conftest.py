"""Hace importables los módulos del directorio aunque `pytest` corra desde la raíz.

El gate se lanza como `python -m pytest arena-web/tests`, así que `arena-web/` no
está en `sys.path` por sí solo: estos son scripts, no un paquete instalado.
"""

from __future__ import annotations

import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))
