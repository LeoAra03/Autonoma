"""Coherencia de la capa de instalación del repo: npm, lanzadores y documentación.

Estos archivos viven fuera del paquete (raíz del repo), así que no los cubre ningún import:
lo que se comprueba aquí es que lo que se promete se puede ejecutar y que las piezas se
referencian entre sí con los nombres reales.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from conftest import REPO_ROOT

AGENT = REPO_ROOT / "autonoma-agent"


def _read(path: Path) -> str:
    assert path.is_file(), f"falta {path}"
    return path.read_text(encoding="utf-8")


def test_package_json_declares_a_usable_command_surface() -> None:
    manifest = json.loads(_read(REPO_ROOT / "package.json"))
    assert manifest["private"] is True
    assert manifest["scripts"]["start"] == "node scripts/bootstrap.mjs"
    for name, command in manifest["scripts"].items():
        assert "bootstrap.mjs" in command, f"`{name}` no delega en el instalador común"
    assert int(manifest["engines"]["node"].lstrip(">=")) >= 18  # node <18 no trae `--experimental` nada útil


def test_documented_npm_commands_all_exist() -> None:
    """`INSTALL.md` promete atajos: cada `npm run x` escrito debe existir de verdad."""
    manifest = json.loads(_read(REPO_ROOT / "package.json"))
    documented = set()
    for line in _read(REPO_ROOT / "INSTALL.md").splitlines():
        for token in line.split("`"):
            if token.startswith("npm run "):
                documented.add(token.split()[-1])
    assert documented, "el documento de instalación perdió su tabla de comandos"
    assert documented <= set(manifest["scripts"]), sorted(documented - set(manifest["scripts"]))


@pytest.mark.parametrize("script", ["scripts/bootstrap.py", "scripts/bootstrap.mjs", "scripts/make_bundle.py"])
def test_launchers_exist_and_are_executable_where_it_matters(script: str) -> None:
    path = REPO_ROOT / script
    assert path.is_file()
    if path.suffix == ".py" and os.name != "nt":
        # En NTFS no hay bit de ejecución: allí el lanzador válido es el `.bat`/`node`.
        assert path.stat().st_mode & 0o111, f"{script} debería poder ejecutarse directo"


def test_node_wrapper_delegates_your_arguments_to_python() -> None:
    source = _read(REPO_ROOT / "scripts" / "bootstrap.mjs")
    assert "spawnSync" in source
    assert "bootstrap.py" in source
    assert "process.argv.slice(2)" in source  # nada se interpone entre `npm start --` y el agente
    assert "python.org/downloads" in source  # el mensaje sin Python explica la alternativa portable


@pytest.mark.skipif(shutil.which("node") is None, reason="Node no está en este entorno")
def test_node_wrapper_is_syntactically_valid() -> None:
    node = shutil.which("node")
    assert node is not None
    done = subprocess.run(
        [node, "--check", str(REPO_ROOT / "scripts" / "bootstrap.mjs")],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
        stdin=subprocess.DEVNULL,  # nunca heredar la consola del runner: bloquea en Windows
    )
    assert done.returncode == 0, done.stderr[-400:]


def test_windows_launcher_is_crlf_and_pauses_instead_of_vanishing() -> None:
    raw = (REPO_ROOT / "Run-Autonoma.bat").read_bytes()
    assert b"\r\n" in raw, "un .bat con saltos LF puede abrirse y cerrarse sin decir nada"
    text = raw.decode("utf-8")
    assert r"scripts\bootstrap.py" in text
    assert "AUTONOMA_NO_PAUSE" in text  # la pausa es opt-out, no sorpresa
    assert "pause" in text
    assert "API_KEY=" not in text  # el lanzador nunca escribe una clave


def test_posix_launcher_uses_the_bootstrap_and_honours_python_overrides() -> None:
    text = _read(REPO_ROOT / "run-autonoma.sh")
    assert text.startswith("#!/bin/sh")
    assert "AUTONOMA_PYTHON" in text
    assert '"$BOOT" run' in text
    assert "scripts/bootstrap.py" in text
    if os.name != "nt":
        # NTFS no tiene bit de ejecución: allí el lanzador que la gente doble-clickeá es
        # `Run-Autonoma.bat`, y comprobar el modo del `.sh` sólo daría un falso negativo.
        assert (REPO_ROOT / "run-autonoma.sh").stat().st_mode & 0o111


@pytest.mark.skipif(shutil.which("sh") is None or os.name == "nt", reason="sin sh POSIX")
def test_posix_launcher_is_parseable() -> None:
    sh = shutil.which("sh")
    assert sh is not None
    done = subprocess.run(
        [sh, "-n", str(REPO_ROOT / "run-autonoma.sh")],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
        stdin=subprocess.DEVNULL,  # nunca heredar la consola del runner: bloquea en Windows
    )
    assert done.returncode == 0, done.stderr[-300:]


def test_ci_installs_the_project_through_the_same_entry_point() -> None:
    """CI no puede inventarse su propio camino de instalación: usa el instalador del repo."""
    workflow = _read(REPO_ROOT / ".github" / "workflows" / "tests.yml")
    assert "python scripts/bootstrap.py --no-input setup" in workflow
    assert "npm start --silent" in workflow
    assert "make_bundle.py --platform windows --dist-dir dist" in workflow
    assert "$global:LASTEXITCODE = 0" in workflow  # el ZIP no puede tumbar el smoke del .exe
    assert "Autonoma-Portable-windows*.zip" in workflow  # el ZIP llega como artefacto, no se pierde
    assert "mypy --platform win32" in workflow


def test_build_scripts_wire_the_bundle_step() -> None:
    """`dist/` se empaqueta desde los propios scripts: CI y local no pueden divergir."""
    portable = _read(AGENT / "scripts" / "build_portable.sh")
    assert 'ROOT="$PWD"' in portable  # sin esto, `set -u` revienta el paso del ZIP
    assert "make_bundle.py" in portable
    assert portable.rstrip().endswith("build_zipapp\nbundle") or "bundle" in portable.split("build_zipapp")[-1]
    windows = _read(AGENT / "scripts" / "build_windows.ps1")
    assert "make_bundle.py" in windows and "SkipSmoke" in windows
    assert "Write-Warning" in windows  # un ZIP que no se arma no puede tumbar el build del .exe


def test_lock_and_extras_do_not_drift_apart() -> None:
    """El lock auditado tiene que contener lo que declara el extra `[test]`.

    Si se añade una herramienta de pruebas al manifiesto y no al lock, `locked-linux`
    falla lejos de aquí con un error opaco; se comprueba la lista, no sólo que existan.
    """
    import re

    pyproject = _read(AGENT / "pyproject.toml")
    extra = re.search(r"test = \[(.*?)\]", pyproject, re.S).group(1)
    # Se recogen las cadenas "paquete>=x,<y" y se queda con el nombre: partir por comas
    # mezclaría los especificadores de versión con los nombres de paquete.
    declared = {re.split(r"[<>=!~\[; ]", req.strip())[0] for req in re.findall(r'"([^"]*)"', extra)}
    assert declared, "el extra [test] quedó vacío"
    lock = _read(AGENT / "requirements-lock.txt")
    pinned = set(re.findall(r"^([A-Za-z0-9._-]+)==", lock, re.M))
    assert {name.lower() for name in declared} <= pinned, sorted(declared - pinned)
    assert "pytest-timeout" in pinned  # la puerta anti-colgues del job `test`


def test_install_doc_covers_every_distribution_route() -> None:
    doc = _read(REPO_ROOT / "INSTALL.md")
    for needle in ("Autonoma.exe", "npm start", "Run-Autonoma.bat", "run-autonoma.sh", "SmartScreen", "--data-dir"):
        assert needle in doc, f"INSTALL.md ya no habla de {needle}"
    assert "notrack.ai/api-keys" in doc  # sin ese enlace la gente se queda fuera en el paso 1
