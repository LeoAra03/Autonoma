import json
import os
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from autonoma import cli, diagnostics
from autonoma.config import Settings
from autonoma.filesystem import FileSystemError, FileSystemManager
from autonoma.key_handler import PanicController
from autonoma.path_policy import has_redirected_component, is_redirected, validate_windows_path


@pytest.mark.parametrize("raw", [r"C:relative.txt", r"C:\file.txt:secret", r"\\.\PhysicalDrive0",
    r"\\?\C:\file", r"C:\CON", r"C:\AUX.txt", r"C:\NUL", r"C:\folder.\file", r"C:\folder \file", r"C:\CON\file", r"\Windows\file"])
def test_invalid_windows_paths(raw):
    with pytest.raises(ValueError):
        validate_windows_path(raw)


@pytest.mark.parametrize("raw", [r"C:\Users\Persona\file.txt", r"C:\carpeta con espacios\nota.md",
    r"notas\hola.txt", r"\\servidor\recurso\nota.txt"])
def test_normal_windows_paths(raw):
    validate_windows_path(raw)


def test_reparse_point_without_symlink_mode():
    # Windows junctions do not need to have S_IFLNK mode.
    fake = SimpleNamespace(lstat=lambda: SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400))
    assert is_redirected(fake)
    fake.lstat = lambda: SimpleNamespace(st_mode=stat.S_IFREG, st_file_attributes=0)
    assert not is_redirected(fake)


def test_missing_and_inaccessible_path(tmp_path):
    assert not is_redirected(tmp_path/"missing")
    def denied():
        raise PermissionError("denied")
    with pytest.raises(PermissionError):
        is_redirected(SimpleNamespace(lstat=denied))


@pytest.mark.skipif(os.name != "nt", reason="Requiere NTFS/Windows real")
def test_native_windows_junction(tmp_path):
    target = tmp_path/"target"
    target.mkdir()
    (target/"keep.txt").write_text("original")
    junction = tmp_path/"junction"
    cmd = str(Path(os.environ["SYSTEMROOT"])/"System32"/"cmd.exe")
    subprocess.run([cmd, "/c", "mklink", "/J", str(junction), str(target)], check=True,
                   capture_output=True, timeout=10)
    assert is_redirected(junction)
    assert has_redirected_component(junction/"keep.txt")
    fs = FileSystemManager(PanicController())
    with pytest.raises(FileSystemError):
        fs.write_file(str(junction/"keep.txt"), "changed")
    assert (target/"keep.txt").read_text() == "original"


@pytest.fixture
def offline_settings(tmp_path, monkeypatch):
    settings = Settings(notrack_api_key="do-not-display-this-secret",
                        knowledge_dir=str(tmp_path/"kb"), log_dir=str(tmp_path/"logs"))
    monkeypatch.setattr(diagnostics, "load_settings", lambda: settings)
    monkeypatch.setattr(diagnostics, "project_root", lambda: tmp_path)
    monkeypatch.setattr(diagnostics, "is_elevated", lambda: False)
    monkeypatch.setattr(diagnostics.importlib.util, "find_spec", lambda _: None)
    return settings


def test_doctor_does_not_disclose_secrets(offline_settings, capsys):
    assert diagnostics.run_diagnostics(as_json=True) == 0
    raw = capsys.readouterr().out
    report = json.loads(raw)
    assert report["local_checks_passed"] and not report["network_tested"]
    assert offline_settings.notrack_api_key not in raw
    assert any(c["name"] == "host_access" and c["status"] == "warning" for c in report["checks"])


def test_doctor_bad_url_redacted(offline_settings, monkeypatch, capsys):
    from dataclasses import replace
    monkeypatch.setattr(diagnostics, "load_settings", lambda: replace(offline_settings, notrack_base_url="https://user:secret@example.com"))
    assert diagnostics.run_diagnostics() == 1
    raw = capsys.readouterr().out
    assert "secret" not in raw and "ERROR" in raw


def test_doctor_invalid_config(monkeypatch, capsys):
    def fail():
        raise ValueError("private-secret")
    monkeypatch.setattr(diagnostics, "load_settings", fail)
    assert diagnostics.run_diagnostics(as_json=True) == 1
    assert "private-secret" not in capsys.readouterr().out


def test_doctor_unwritable(offline_settings, monkeypatch):
    monkeypatch.setattr(diagnostics, "writable_directory", lambda _: False)
    assert not diagnostics.collect_diagnostics()["local_checks_passed"]


def test_writable_probe_no_leftover(tmp_path):
    assert diagnostics.writable_directory(tmp_path)
    assert list(tmp_path.iterdir()) == []
    file = tmp_path/"not-a-directory"
    file.write_text("keep")
    assert not diagnostics.writable_directory(file)


@pytest.mark.parametrize("args", [["--json"], ["--doctor", "prompt"], ["--doctor", "--allow-commands"], ["--doctor", "--global-hotkey"]])
def test_doctor_rejects_ambiguous_cli(args):
    with pytest.raises(SystemExit) as exc:
        cli.parse_args(args)
    assert exc.value.code == 2


def test_doctor_main_no_session(tmp_path, monkeypatch):
    """`--data-dir` se pasa explícitamente: el entorno del proceso no se muta."""
    monkeypatch.setattr(cli, "Session", lambda *a, **k: pytest.fail("No iniciar sesión"))
    monkeypatch.delenv("AUTONOMA_HOME", raising=False)
    called = []

    def fake_diagnostics(**kwargs):
        called.append(kwargs)
        return 0

    monkeypatch.setattr(cli, "run_diagnostics", fake_diagnostics)
    with pytest.raises(SystemExit) as exc:
        cli.main(["--doctor", "--json", "--data-dir", str(tmp_path)])
    assert exc.value.code == 0
    assert called[0]["as_json"] is True
    assert Path(called[0]["data_root"].path) == tmp_path
    assert "AUTONOMA_HOME" not in os.environ


def test_global_hotkey_is_opt_in():
    assert not cli.parse_args([]).global_hotkey
    assert cli.parse_args(["--global-hotkey"]).global_hotkey


def test_failed_session_initialization_cleans_up(offline_settings, monkeypatch):
    stopped, closed = [], []
    monkeypatch.setattr(cli, "load_settings", lambda **kwargs: offline_settings)
    monkeypatch.setattr(cli, "configure_logging", lambda **kwargs: SimpleNamespace(close=lambda: closed.append(True)))
    handler = SimpleNamespace(
        status=SimpleNamespace(state=__import__("autonoma.key_handler", fromlist=["x"]).ListenerState.STOPPED),
        stop=lambda: stopped.append(True),
        start=lambda: SimpleNamespace(running=False, describe=lambda: "simulado"),
    )
    monkeypatch.setattr(cli, "KeyHandler", lambda *a, **k: handler)

    def fail(*a, **k):
        raise ValueError("bad configuration")

    monkeypatch.setattr(cli, "build_agent", fail)
    with pytest.raises(ValueError):
        cli.Session(SimpleNamespace(), global_hotkey=True)
    assert stopped == [True]
    assert closed == [True]
