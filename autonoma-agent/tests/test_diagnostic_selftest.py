"""`--selftest` y el informe de diagnóstico: estructura estable para CI y para el .exe."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from autonoma import __version__
from autonoma.config import Settings
from autonoma.diagnostics import (
    Check,
    CheckStatus,
    DiagnosticReport,
    collect_diagnostics,
    format_report,
    is_elevated,
    run_selftest,
    writable_directory,
)
from autonoma.observability import MetricsRegistry
from autonoma.runtime import DataOrigin, DataRoot


def test_check_status_knows_what_counts_as_failure() -> None:
    assert CheckStatus.OK.is_failure is False
    assert CheckStatus.WARNING.is_failure is False
    assert CheckStatus.ERROR.is_failure is True
    assert Check("x", CheckStatus.WARNING, "aviso").as_dict() == {"name": "x", "status": "warning", "message": "aviso"}


def test_report_behaves_like_a_mapping_and_serializes() -> None:
    report = DiagnosticReport(
        checks=(Check("a", CheckStatus.OK, "bien"), Check("b", CheckStatus.ERROR, "mal")),
        frozen=False,
        platform_name="linux",
        python_version="3.11",
        version=__version__,
        data_root={"path": "/x", "origin": "flag"},
    )
    assert report["local_checks_passed"] is False
    assert report["network_tested"] is False
    assert {"checks", "version", "frozen", "data_root", "app"} <= set(report)
    payload = json.loads(json.dumps(report.as_dict()))
    assert [check["name"] for check in payload["checks"]] == ["a", "b"]
    assert len(report.failures) == 1
    with pytest.raises(TypeError):
        report["checks"] = ()  # type: ignore[index]


def test_collected_report_is_stable_and_secret_free(tmp_path: Path) -> None:
    settings = Settings(notrack_api_key="sk-diagnostico-123456", root_dir=tmp_path)
    first = collect_diagnostics(metrics=MetricsRegistry().snapshot(), data_root=tmp_path)
    second = collect_diagnostics(metrics=MetricsRegistry().snapshot(), data_root=tmp_path)
    assert [check.name for check in first.checks] == [check.name for check in second.checks]
    rendered = format_report(first)
    assert settings.notrack_api_key not in rendered
    assert "sk-diagnostico" not in json.dumps(first.as_dict(), default=str)


def test_privileges_and_host_checks_come_first(tmp_path: Path) -> None:
    report = collect_diagnostics(data_root=tmp_path)
    names = [check.name for check in report.checks]
    assert names[0] == "privileges"
    assert names.index("host_access") < names.index("configuration")
    assert {"notrack_key", "notrack_url", "data_directory", "dependencies"} <= set(names)


def _root_that_cannot_be_created(tmp_path: Path) -> Path:
    """Raíz inutilizable en cualquier SO: el padre es un archivo regular.

    Sustituye a `chmod 0o500` (en Windows el atributo de sólo-lectura no impide escribir
    en un directorio) y a rutas tipo `/proc` (no existen fuera de Linux): el fallo de
    `mkdir` es idéntico en POSIX y NTFS.
    """
    occupied = tmp_path / "ocupado"
    occupied.write_text("esto es un archivo, no un directorio", encoding="utf-8")
    return occupied / "autonoma"


def test_unusable_data_root_fails_the_diagnosis(tmp_path: Path) -> None:
    """Si la raíz de datos no puede usarse, el diagnóstico lo dice sin crudos técnicos."""
    report = collect_diagnostics(data_root=_root_that_cannot_be_created(tmp_path))
    assert report.local_checks_passed is False
    text = " ".join(f"{check.name}:{check.message}" for check in report.checks)
    assert any(word in text for word in ("escritura", "permisos", "Configuración"))
    assert "Traceback" not in text  # el usuario nunca ve una traceback


def test_unwritable_data_root_is_reported_as_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """La rama `sin escritura` se reporta como ERROR accionable, no como fallo misterioso."""
    monkeypatch.setattr("autonoma.diagnostics.writable_directory", lambda path: False)
    report = collect_diagnostics(data_root=tmp_path)
    assert report.local_checks_passed is False
    failing = [check for check in report.checks if check.status is CheckStatus.ERROR]
    assert {"data_directory", "knowledge_directory", "log_directory"} <= {check.name for check in failing}
    assert all("sin escritura" in check.message for check in failing)


@pytest.mark.skipif(os.name == "nt", reason="Windows no honra el modo de un directorio")
def test_read_only_directory_is_detected_on_posix(tmp_path: Path) -> None:
    """Evidencia real de permisos (POSIX): `writable_directory` no se fía de `os.access`."""
    blocked = tmp_path / "bloqueado"
    blocked.mkdir()
    blocked.chmod(0o500)
    try:
        assert writable_directory(blocked) is False
    finally:
        blocked.chmod(0o700)
    assert writable_directory(blocked) is True  # el bloqueo era del modo, no del sistema


def test_writable_directory_helper_matches_the_report(tmp_path: Path) -> None:
    assert writable_directory(tmp_path) is True
    assert writable_directory(tmp_path / "inexistente" / "sub") is True  # se crea si hace falta
    assert writable_directory(_root_that_cannot_be_created(tmp_path)) is False


def test_elevation_is_tri_state() -> None:
    assert is_elevated() in (True, False, None)


def test_selftest_json_has_the_executable_contract(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = run_selftest(as_json=True, data_root=DataRoot(tmp_path, DataOrigin.FLAG))
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["ok"] is True
    assert payload["version"] == __version__
    assert {"frozen_mode", "bundle_imports", "data_root", "cli_entry"} <= {check["name"] for check in payload["checks"]}
    assert all({"name", "status", "message"} <= set(check) for check in payload["checks"])


def test_selftest_reports_an_unwritable_data_root(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """El smoke del .exe debe fallar si la raíz de datos no admite escritura."""
    code = run_selftest(as_json=True, data_root=DataRoot(_root_that_cannot_be_created(tmp_path), DataOrigin.FLAG))
    payload = json.loads(capsys.readouterr().out)
    assert code == 1
    assert payload["ok"] is False
    assert any(check["name"] == "data_root" and check["status"] == "error" for check in payload["checks"])


def test_selftest_text_mode_is_human_readable(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert run_selftest(as_json=False, data_root=tmp_path) == 0
    output = capsys.readouterr().out
    assert "autonoma" in output.lower()
    assert "Traceback" not in output
