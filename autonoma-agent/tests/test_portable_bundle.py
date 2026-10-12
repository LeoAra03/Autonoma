"""El ZIP portable: qué entra, qué dice el LEEME y qué falla (y cómo lo reporta)."""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

import pytest

from scripts_tools import (
    BundleError,
    build_bundle,
    collect_files,
    make_bundle,
    package_version,
    readme_text,
    sha256_of,
    version_from_source,
)


@pytest.fixture
def fake_dist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Un `dist/` simulado con exe + hash + plantilla de .env, sin compilar nada."""
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "Autonoma.exe").write_bytes(b"MZ" + b"\x00" * 40)
    (dist / "Autonoma.exe.sha256").write_text("abc123  Autonoma.exe\n", encoding="utf-8")
    agent = tmp_path / "autonoma-agent"
    (agent / "dist").mkdir(parents=True)
    (agent / ".env.example").write_text("NOTRACK_API_KEY=\nBRAVE_API_KEY=\n", encoding="utf-8")
    monkeypatch.setattr(make_bundle, "AGENT", agent)
    monkeypatch.setattr(make_bundle, "TEMPLATE_ENV", ".env.example")
    return dist


def test_bundle_contains_the_binary_the_env_template_and_a_readme(fake_dist: Path) -> None:
    archive = build_bundle(fake_dist, "windows", version="2.0.0")
    with zipfile.ZipFile(archive) as bundle:
        names = sorted(bundle.namelist())
    assert names == [
        "Autonoma/.env.example",
        "Autonoma/Autonoma.exe",
        "Autonoma/Autonoma.exe.sha256",
        "Autonoma/LEEME.txt",
    ]
    assert archive.name == "Autonoma-Portable-windows-2.0.0.zip"


def test_bundle_publishes_its_own_sha256(fake_dist: Path) -> None:
    archive = build_bundle(fake_dist, "windows", version="2.0.0")
    record = Path(str(archive) + ".sha256").read_text(encoding="utf-8").split()
    assert record[1] == archive.name
    assert record[0] == sha256_of(archive)  # el hash sirve para verificar exactamente este archivo


def test_readme_names_the_real_launcher_and_never_invents_a_key(fake_dist: Path) -> None:
    text = readme_text("2.0.0", "windows", ["Autonoma.exe", ".env.example"])
    assert "Autonoma.exe" in text
    assert "notrack.ai/api-keys" in text
    assert "--allow-commands" in text  # el riesgo queda documentado en el propio paquete
    assert "sk-" not in text  # nunca una clave de ejemplo que parezca real


def test_linux_bundle_falls_back_to_the_zipapp_launcher(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "autonoma.pyz").write_bytes(b"#!/usr/bin/env python3\n")
    agent = tmp_path / "autonoma-agent"
    agent.mkdir()
    monkeypatch.setattr(make_bundle, "AGENT", agent)
    text = readme_text("2.0.0", "linux", ["autonoma.pyz"])
    assert "autonoma.pyz" in text
    archive = build_bundle(dist, "linux", version="2.0.0")
    with zipfile.ZipFile(archive) as bundle:
        assert "Autonoma/autonoma.pyz" in bundle.namelist()


def test_missing_artifacts_is_an_actionable_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    monkeypatch.setattr(make_bundle, "AGENT", tmp_path / "nada")
    with pytest.raises(BundleError, match="Construye primero"):
        collect_files(dist, "windows")
    with pytest.raises(BundleError):
        build_bundle(dist, "windows")


def test_collect_skips_the_platforms_absent_binaries(fake_dist: Path) -> None:
    names = [path.name for path in collect_files(fake_dist, "linux")]
    assert "Autonoma.exe" not in names  # el exe de Windows no viaja en el bundle de Linux
    assert names == [".env.example"]  # y sin binario construido, sólo queda la plantilla


def test_version_from_source_reads_the_file_without_importing_anything(tmp_path: Path) -> None:
    """El respaldo por texto debe valer en un checkout recién clonado, sin entorno."""
    agent = tmp_path / "autonoma-agent"
    (agent / "autonoma").mkdir(parents=True)
    (agent / "autonoma" / "_version.py").write_text('__version__ = "2.1.3"\n', encoding="utf-8")
    assert version_from_source(agent) == "2.1.3"

    (agent / "autonoma" / "_version.py").write_text("x = 1\n", encoding="utf-8")
    with pytest.raises(BundleError, match="no encuentro __version__"):
        version_from_source(agent)


def test_package_version_falls_back_to_the_source_when_import_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Sin entorno instalado (checkout pelado), la versión sale del archivo y el ZIP se arma igual."""
    agent = tmp_path / "autonoma-agent"
    (agent / "autonoma").mkdir(parents=True)
    (agent / "autonoma" / "_version.py").write_text('__version__ = "9.9.9"\n', encoding="utf-8")
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "Autonoma.exe").write_bytes(b"MZ fake")
    monkeypatch.setattr(make_bundle, "AGENT", agent)
    monkeypatch.setattr(sys, "path", [str(tmp_path)])  # ni el paquete ni su caché valen aquí
    monkeypatch.setitem(sys.modules, "autonoma", None)  # fuerza el ModuleNotFoundError
    assert package_version() == "9.9.9"
    assert build_bundle(dist, "windows").name == "Autonoma-Portable-windows-9.9.9.zip"


def test_unsafe_version_characters_never_reach_the_file_name(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    agent = tmp_path / "autonoma-agent"
    (agent / "autonoma").mkdir(parents=True)
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "Autonoma.exe").write_bytes(b"MZ")
    monkeypatch.setattr(make_bundle, "AGENT", agent)
    archive = build_bundle(dist, "windows", version=r"2.0.0/..\..\evil")
    assert archive.name == "Autonoma-Portable-windows-2.0.0-..-..-evil.zip"
    assert "/" not in archive.name and "\\" not in archive.name
