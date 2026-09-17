"""Permite ejecutar las pruebas desde la raíz del repositorio o del proyecto."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
