#!/usr/bin/env python3
"""Empaqueta un ZIP portable "copiar-y-usar" alrededor del ejecutable ya construido.

No compila nada: toma lo que hay en `autonoma-agent/dist/` (el `Autonoma.exe` de Windows o
el binario/`autonoma.pyz` de Linux), le añade la plantilla de configuración y un LEEME
generado con la versión real, y produce `dist/Autonoma-Portable-<plataforma>.zip` con su
`.sha256`. Así quien sólo quiere el agente descomprime, pone su clave y escribe.

Uso:
    python scripts/make_bundle.py                  # detecta el SO actual
    python scripts/make_bundle.py --platform windows --dist-dir autonoma-agent/dist
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
import zipfile
from pathlib import Path
from typing import Final

ROOT: Final[Path] = Path(__file__).resolve().parent.parent
AGENT: Final[Path] = ROOT / "autonoma-agent"
TEMPLATE_ENV: Final[str] = ".env.example"
BUNDLE_DIR: Final[str] = "Autonoma"
ENV_KEYS: Final[tuple[str, ...]] = ("NOTRACK_API_KEY", "BRAVE_API_KEY")
OPTIONAL_FILES: Final[dict[str, tuple[str, ...]]] = {
    "windows": ("Autonoma.exe", "Autonoma.exe.sha256"),
    "linux": ("Autonoma", "Autonoma.sha256", "autonoma.pyz", "autonoma.pyz.sha256"),
}
SAFE_NAME: Final[re.Pattern[str]] = re.compile(r"[^A-Za-z0-9._+-]")


class BundleError(RuntimeError):
    """Fallo claro al armar el ZIP: el mensaje dice qué falta, sin traceback."""


def detect_platform() -> str:
    return "windows" if os.name == "nt" else "linux"


def version_from_source(agent: Path) -> str:
    """Lee `autonoma/_version.py` como texto: vale sin instalar dependencias ni el paquete."""
    source = (agent / "autonoma" / "_version.py").read_text(encoding="utf-8")
    match = re.search(r'__version__\s*=\s*["\']([^"\']+)["\']', source)
    if match is None:
        raise BundleError(f"no encuentro __version__ en {agent / 'autonoma' / '_version.py'}")
    return match.group(1)


def package_version() -> str:
    """Versión del paquete (fuente única en `autonoma/_version.py`).

    Se intenta importar porque el empaquetado normal corre con el proyecto instalado; si
    no hay entorno (CI recién clonado, otro intérprete) basta con leer el archivo.
    """
    try:
        sys.path.insert(0, str(AGENT))
        from autonoma import __version__  # sólo se necesita al empaquetar

        return str(__version__)
    except Exception:  # noqa: BLE001 - el respaldo por texto es exactamente igual de válido
        return version_from_source(AGENT)


def readme_text(version: str, platform: str, files: list[str]) -> str:
    """El LEEME que viaja dentro del ZIP: tres párrafos, cero suposiciones."""
    launcher = "Autonoma.exe" if platform == "windows" else ("Autonoma" if "Autonoma" in files else "autonoma.pyz")
    runner = f'".\\{launcher}" "tu instrucción"' if platform == "windows" else f'./{launcher} "tu instrucción"'
    lines = [
        f"Autonoma {version} — portable, sin instalador",
        "=" * 46,
        "",
        "1) Copia esta carpeta donde quieras (USB, Documentos, lo que sea).",
        f"2) Renombra {TEMPLATE_ENV} a .env y pega tu clave de NoTrack en NOTRACK_API_KEY",
        "   (se crea en https://notrack.ai/api-keys). Sin clave igual puedes leer/editar tus notas.",
        f"3) Doble clic a {launcher}, o en una terminal:",
        "",
        f"   {runner}",
        "",
        "Atajos: /help dentro del programa, Ctrl+C cancela lo que esté haciendo,",
        "--doctor --json explica qué está configurado, --selftest --json valida el paquete.",
        "",
        "Dónde guarda cosas: en esta misma carpeta se crean `knowledge_base/` (tus notas)",
        "y `logs/autonoma.jsonl` (registro local con trace_id). Nada sale de tu equipo salvo",
        "las llamadas a NoTrack.ai y a la búsqueda que tú pidas.",
        "",
        "Para ejecutar comandos en tu máquina hace falta --allow-commands y aprobaciones",
        "explícitas `SI` operación por operación: el agente corre con tus permisos, sin sandbox.",
        "",
        "Archivos incluidos: " + ", ".join(files),
    ]
    return "\n".join(lines) + "\n"


def collect_files(dist_dir: Path, platform: str) -> list[Path]:
    """Los artefactos presentes (uno de los dos binarios basta) más la plantilla de `.env`."""
    wanted = [dist_dir / name for name in OPTIONAL_FILES[platform] if (dist_dir / name).is_file()]
    template = AGENT / TEMPLATE_ENV
    if template.is_file():
        wanted.append(template)
    if not wanted:
        raise BundleError(
            f"no hay nada que empaquetar en {dist_dir}. Construye primero el ejecutable "
            "(scripts/build_windows.ps1 o scripts/build_portable.sh)."
        )
    return [path for path in wanted if path.is_file()]


def build_bundle(dist_dir: Path, platform: str, *, version: str | None = None) -> Path:
    """Crea el ZIP (con nombres seguros y sin rutas absolutas) y devuelve su ruta."""
    files = collect_files(dist_dir, platform)
    tag = version or package_version()
    safe_tag = SAFE_NAME.sub("-", tag)
    archive = dist_dir / f"Autonoma-Portable-{platform}-{safe_tag}.zip"
    payload = readme_text(tag, platform, sorted(path.name for path in files))
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as bundle:
        bundle.writestr(f"{BUNDLE_DIR}/LEEME.txt", payload)
        for path in files:
            bundle.write(path, f"{BUNDLE_DIR}/{path.name}")
    (archive.with_suffix(archive.suffix + ".sha256")).write_text(
        f"{sha256_of(archive)}  {archive.name}\n", encoding="utf-8"
    )
    return archive


def sha256_of(path: Path, *, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            block = stream.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "empaqueta el ZIP portable")
    parser.add_argument("--dist-dir", type=Path, default=AGENT / "dist", help="dónde están los binarios construidos")
    parser.add_argument("--platform", choices=("windows", "linux"), help="por defecto el SO actual")
    parser.add_argument("--version", help="por defecto se lee del paquete")
    options = parser.parse_args(argv)
    platform = options.platform or detect_platform()
    try:
        archive = build_bundle(options.dist_dir.expanduser().resolve(), platform, version=options.version)
    except (BundleError, OSError) as exc:
        print(f"autonoma: {exc}", file=sys.stderr)
        return 2
    print(f"listo: {archive.name} ({archive.stat().st_size / 1_048_576:.1f} MB)")
    print(f"hash:  {sha256_of(archive)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
