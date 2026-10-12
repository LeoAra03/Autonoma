"""Permite ejecutar las pruebas desde la raíz del repositorio o del proyecto."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def load_repo_script(name: str) -> ModuleType:
    """Carga `scripts/<name>.py` de la raíz del repo como módulo aislado.

    Los lanzadores del repositorio (instalador, empaquetador) son scripts stdlib que no
    forman parte del paquete publicado; se prueban por su camino real de carga, sin
    ensuciar `sys.path` ni depender de un `__init__.py` inexistente.
    """
    path = REPO_ROOT / "scripts" / f"{name}.py"
    if not path.is_file():
        raise FileNotFoundError(f"no existe el script del repo: {path}")
    spec = importlib.util.spec_from_file_location(f"repo_script_{name}", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"no se puede importar {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module
