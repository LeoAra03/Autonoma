# -*- mode: python ; coding: utf-8 -*-
"""Spec de PyInstaller: un único ejecutable portable (`Autonoma.exe` en Windows).

Decisiones que importan:
- `collect_submodules("autonoma")`: el paquete declara su API en `autonoma/__init__.py`,
  así que añadir un módulo nuevo no puede dejarlo fuera del bundle.
- `optimize=2`: el paquete no usa `assert` ni depende de `__doc__` en tiempo de
  ejecución; el binario queda más pequeño y algo más rápido de arrancar.
- `excludes`: Playwright y el stack de numpy/pandas no viajan en el .exe; el backend
  de navegador es un extra opcional y se instala aparte (`pip install .[browser]`).
"""

import os
import tempfile
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPEC).resolve().parent  # noqa: F821 — inyectada por PyInstaller


def _package_version() -> str:
    """Lee `autonoma/_version.py` por AST: sin importar el paquete al empaquetar."""
    import ast

    source = (ROOT / "autonoma" / "_version.py").read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "__version__":
                    value = node.value
                    if isinstance(value, ast.Constant) and isinstance(value.value, str):
                        return value.value
    raise SystemExit("autonoma/_version.py no define __version__ como literal de texto")


__version__ = _package_version()

hidden = collect_submodules("autonoma")
hidden += collect_submodules("rich")
hidden += [
    "bs4",
    "lxml",
    "httpx",
    "httpcore",
    "anyio",
    "anyio._backends._asyncio",
    "psutil",
    "idna",
    "certifi",
    "h11",
    "sniffio",
]
if os.name == "nt":
    hidden += collect_submodules("pynput")

datas = []
datas += collect_data_files("rich")
datas += collect_data_files("lxml")
datas += collect_data_files("certifi")

icon = ROOT / "assets" / "autonoma.ico"

exe = None


def _version_tuple(text: str) -> tuple[int, int, int, int]:
    """`2.0.0rc1` -> (2, 0, 0, 0): Windows exige cuatro enteros en FixedFileInfo."""
    parts: list[int] = []
    for chunk in text.replace("-", ".").split("."):
        digits = ""
        for char in chunk:
            if not char.isdigit():
                break
            digits += char
        parts.append(int(digits) if digits else 0)
    parts += [0] * (4 - len(parts))
    return tuple(parts[:4])  # type: ignore[return-value]


def _windows_version_info() -> str | None:
    """Archivo de `VSVersionInfo` en el formato que PyInstaller `eval()` (no JSON).

    Si algo falla se devuelve `None`: el ejecutable sigue construyéndose, sólo pierde
    las propiedades visibles en el Explorador. Nunca debe tumbar el build.
    """
    if os.name != "nt":
        return None
    try:
        numeric = _version_tuple(__version__)
        pairs = [
            ("CompanyName", ""),
            ("FileDescription", "Autonoma - agente local de terminal"),
            ("FileVersion", __version__),
            ("InternalName", "autonoma"),
            ("LegalCopyright", "Uso personal"),
            ("LegalTrademarks", ""),
            ("OriginalFilename", "Autonoma.exe"),
            ("ProductName", "Autonoma"),
            ("ProductVersion", __version__),
            ("Comments", "Consola; aprobacion humana por operacion"),
        ]
        structs = ",\n        ".join(f"StringStruct({name!r}, {value!r})" for name, value in pairs)
        payload = (
            f"VSVersionInfo(\n"
            f"  ffi=FixedFileInfo(filevers={numeric}, prodvers={numeric}, mask=0x3f, "
            f"flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),\n"
            f"  kids=[\n"
            f"    StringFileInfo([StringTable('040904B0', [\n        {structs}\n      ])]),\n"
            f"    VarFileInfo([VarStruct('Translation', [1033, 1200])])\n"
            f"  ]\n)"
        )
        compile(payload, "<version_info>", "eval")  # sólo una expresión, sin comentarios
        target = Path(tempfile.gettempdir()) / "autonoma_version_info.txt"
        target.write_text(payload, encoding="ascii")
        return str(target)
    except Exception as exc:  # noqa: BLE001 - el metadato no puede romper el empaquetado
        print(f"AVISO: sin versión de Windows en el .exe ({exc!r})")
        return None


version_file = _windows_version_info()

a = Analysis(
    [str(ROOT / "run_autonoma.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["playwright", "selenium", "numpy", "pandas", "matplotlib", "PyQt5", "PySide6", "tkinter"],
    noarchive=False,
    optimize=2,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="Autonoma",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(icon) if icon.is_file() else None,
    version=version_file,
)
