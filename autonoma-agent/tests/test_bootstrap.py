"""El instalador de un solo comando: descubrimiento de Python, marca de instalación, `.env` y enrutado.

Nada de esto crea entornos reales ni toca la red: `_run` corre en `--dry-run` o se sustituye,
que es exactamente la capa que hay que garantizar para que `npm start` no falle en la máquina
de alguien con un PATH raro.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import load_repo_script

bootstrap = load_repo_script("bootstrap")

BootstrapError = bootstrap.BootstrapError


# --------------------------------------------------------------- intérprete de Python
def test_find_python_accepts_the_current_interpreter() -> None:
    argv, version = bootstrap.find_python(sys.executable)
    assert argv == [sys.executable]
    assert version.count(".") >= 2


def test_setup_uses_the_interpreter_named_by_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """`--python` / `AUTONOMA_PYTHON` mandan: es la salida cuando el PATH tiene un Python viejo."""
    asked: list[str | None] = []

    def fake_find(explicit: str | None = None) -> tuple[list[str], str]:
        asked.append(explicit)
        return [sys.executable], "3.12.1"

    monkeypatch.setenv("AUTONOMA_PYTHON", "/opt/py/bin/python")
    monkeypatch.setattr(bootstrap, "find_python", fake_find)
    monkeypatch.setattr(bootstrap, "ensure_venv", lambda *a, **k: None)
    monkeypatch.setattr(bootstrap, "install_package", lambda *a, **k: None)
    monkeypatch.setattr(bootstrap, "contract_fingerprint", lambda: "fp")
    monkeypatch.setattr(bootstrap, "already_installed", lambda venv, *, fingerprint: True)
    bootstrap.setup(Path("/tmp/sin-importancia"), dry_run=True, interactive=False)
    assert asked == ["/opt/py/bin/python"]


def test_a_good_current_interpreter_needs_no_probing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Con un Python que ya cumple en marcha, `find_python` no lanza ni un subprocess."""

    def explode(*argv, **kwargs):
        raise AssertionError("no hay nada que sondear: el intérprete en curso vale")

    monkeypatch.delenv("AUTONOMA_PYTHON", raising=False)
    monkeypatch.setattr(bootstrap.subprocess, "run", explode)
    argv, version = bootstrap.find_python()
    if sys.version_info[:2] >= bootstrap.MIN_PYTHON:
        assert argv == [sys.executable]
        assert version == ".".join(str(part) for part in sys.version_info[:3])


def test_current_python_only_vets_what_is_already_running() -> None:
    assert bootstrap.current_python(version=(3, 12, 1), executable=sys.executable) == ([sys.executable], "3.12.1")
    assert bootstrap.current_python(version=(3, 9, 18), executable=sys.executable) is None  # viejo
    assert bootstrap.current_python(version=(3, 12, 0), executable="") is None  # pythonw/congelado
    assert bootstrap.current_python(version=(3, 12, 0), executable="/no-existe/python") is None


def test_find_python_reports_an_actionable_message_when_nothing_qualifies(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AUTONOMA_PYTHON", raising=False)
    monkeypatch.setattr(bootstrap, "current_python", lambda **kwargs: None)
    monkeypatch.setattr(
        bootstrap, "python_candidates", lambda explicit, *, windows=None: [[sys.executable, "--no-such-flag"]]
    )
    with pytest.raises(BootstrapError) as excinfo:
        bootstrap.find_python()
    message = str(excinfo.value)
    assert "3.10" in message and "python.org" in message  # qué instalar, no sólo "falló"


def test_probe_rejects_interpreters_below_the_minimum(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bootstrap, "MIN_PYTHON", (99, 0))
    assert bootstrap.probe_python([sys.executable]) is None
    assert bootstrap.probe_python([str(Path(sys.executable).parent / "no-existe-xyz")]) is None


def test_candidates_order_the_launcher_before_the_path_entries() -> None:
    assert bootstrap.python_candidates("/opt/py313/bin/python", windows=False)[0] == ["/opt/py313/bin/python"]
    assert bootstrap.python_candidates("/opt/py313/bin/python", windows=True)[1][0].startswith("py")
    assert all(entry != ["py", "-3"] for entry in bootstrap.python_candidates(None, windows=False))


# --------------------------------------------------------------- rutas del entorno
def test_venv_paths_follow_the_platform(tmp_path: Path) -> None:
    # Se pasa la plataforma como dato, nunca parcheando `os.name`: eso rompe `pathlib`
    # en el proceso de pruebas (lección ya aprendida en Windows, ver test_processes).
    assert bootstrap.venv_python(tmp_path, windows=True) == tmp_path / "Scripts" / "python.exe"
    assert bootstrap.venv_executable(tmp_path, "autonoma", windows=True) == tmp_path / "Scripts" / "autonoma.exe"
    assert bootstrap.venv_python(tmp_path, windows=False) == tmp_path / "bin" / "python"
    assert bootstrap.venv_executable(tmp_path, "autonoma", windows=False) == tmp_path / "bin" / "autonoma"


def test_resolved_venv_prefers_the_flag_then_the_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AUTONOMA_VENV", str(tmp_path / "por-env"))
    assert bootstrap.resolved_venv(str(tmp_path / "por-flag")) == (tmp_path / "por-flag").resolve()
    assert bootstrap.resolved_venv(None) == (tmp_path / "por-env").resolve()
    monkeypatch.delenv("AUTONOMA_VENV", raising=False)
    assert bootstrap.resolved_venv(None) == bootstrap.ROOT / ".venv"


# --------------------------------------------------------------- marca de instalación
def test_fingerprint_changes_when_the_manifest_changes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    monkeypatch.setattr(bootstrap, "PACKAGE", package)
    before = bootstrap.contract_fingerprint()
    (package / "pyproject.toml").write_text("[project]\nname='x'\ndependencies=['httpx']\n", encoding="utf-8")
    assert bootstrap.contract_fingerprint() != before
    (package / "requirements.txt").write_text("httpx==0.28\n", encoding="utf-8")
    assert bootstrap.contract_fingerprint() != before  # un extra nuevo también reinicia la caché


def test_stamp_round_trip_and_cache_invalidation(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    venv = tmp_path / ".venv"
    venv.mkdir()
    assert bootstrap.read_stamp(venv) == {}
    assert bootstrap.already_installed(venv, fingerprint="abc") is False

    bootstrap.write_stamp(venv, fingerprint="abc", python="3.12.1")
    assert bootstrap.read_stamp(venv)["fingerprint"] == "abc"

    monkeypatch.setattr(bootstrap, "venv_executable", lambda v, name: v / "bin" / name)
    monkeypatch.setattr(bootstrap, "probe_python", lambda argv: "3.12.1")
    assert bootstrap.already_installed(venv, fingerprint="abc") is False  # falta el entry point
    (venv / "bin").mkdir()
    (venv / "bin" / "autonoma").write_text("#!/bin/sh\n", encoding="utf-8")
    assert bootstrap.already_installed(venv, fingerprint="abc") is True
    assert bootstrap.already_installed(venv, fingerprint="otro") is False  # cambió el manifiesto


def test_corrupt_stamp_is_ignored_not_fatal(tmp_path: Path) -> None:
    venv = tmp_path / ".venv"
    venv.mkdir()
    (venv / bootstrap.STAMP_NAME).write_text("{esto no es json", encoding="utf-8")
    assert bootstrap.read_stamp(venv) == {}


# --------------------------------------------------------------- `.env` y la clave
def test_upsert_creates_replaces_and_appends(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    bootstrap.upsert_env_value(env, "NOTRACK_API_KEY", "sk-1")
    assert env.read_text(encoding="utf-8") == "NOTRACK_API_KEY=sk-1\n"

    env.write_text("# comentario\nNOTRACK_API_KEY=sk-vieja\nOTRA=1\n", encoding="utf-8")
    bootstrap.upsert_env_value(env, "NOTRACK_API_KEY", "sk-nueva")
    text = env.read_text(encoding="utf-8")
    assert "sk-nueva" in text and "sk-vieja" not in text and "OTRA=1" in text

    bootstrap.upsert_env_value(env, "BRAVE_API_KEY", "bs-1")
    assert "BRAVE_API_KEY=bs-1" in env.read_text(encoding="utf-8")


def test_upsert_replaces_the_commented_template_line(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("# NOTRACK_API_KEY=pega-aqui\nOTRA=1\n", encoding="utf-8")
    bootstrap.upsert_env_value(env, "NOTRACK_API_KEY", "sk-real")
    assert env.read_text(encoding="utf-8").splitlines() == ["NOTRACK_API_KEY=sk-real", "OTRA=1"]


@pytest.mark.parametrize("value", ["", "   ", "sk-notrack-pega-aqui-tu-clave", "pega-aqui"])
def test_placeholders_do_not_count_as_a_key(value: str) -> None:
    assert bootstrap.is_real_key(value) is False


def test_real_keys_are_recognised() -> None:
    assert bootstrap.is_real_key("sk-notrack-abcdef") is True


def test_env_state_reports_missing_template(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(bootstrap, "DATA_ENV", tmp_path / ".env")
    monkeypatch.setattr(bootstrap, "ENV_TEMPLATE", tmp_path / ".env.example")
    assert bootstrap.env_state()["exists"] is False


def test_ensure_env_file_copies_the_template_and_warns_without_a_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(bootstrap, "DATA_ENV", tmp_path / ".env")
    monkeypatch.setattr(bootstrap, "ENV_TEMPLATE", tmp_path / ".env.example")
    (tmp_path / ".env.example").write_text("NOTRACK_API_KEY=sk-notrack-pega-aqui-tu-clave\n", encoding="utf-8")

    assert bootstrap.ensure_env_file(dry_run=False, interactive=False) is False
    assert (tmp_path / ".env").is_file()  # el archivo existe: falta la clave, no el archivo
    assert "bootstrap.py key" in capsys.readouterr().out  # y se le dice a la persona


def test_ensure_env_file_stores_a_key_answered_interactively(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    env = tmp_path / ".env"
    monkeypatch.setattr(bootstrap, "DATA_ENV", env)
    monkeypatch.setattr(bootstrap, "ENV_TEMPLATE", tmp_path / "no-existe.example")
    monkeypatch.setattr(bootstrap, "ask_secret", lambda prompt: "sk-guardada")
    assert bootstrap.ensure_env_file(dry_run=False, interactive=True) is True
    assert bootstrap.parse_env_file(env)["NOTRACK_API_KEY"] == "sk-guardada"


def test_interruption_while_asking_is_not_a_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def interrupt(prompt: str) -> str:
        raise KeyboardInterrupt

    monkeypatch.setattr(bootstrap, "tty_available", lambda: True)
    monkeypatch.setattr(bootstrap.getpass, "getpass", interrupt)
    assert bootstrap.ask_secret("clave: ") == ""


def test_without_a_terminal_nobody_is_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sin TTY no se lee `stdin`: una tubería heredada del lanzador bloquearía para siempre."""

    def explode(prompt: str) -> str:
        raise AssertionError("getpass no debe llegarse a invocar sin terminal")

    monkeypatch.setattr(bootstrap, "tty_available", lambda: False)
    monkeypatch.setattr(bootstrap.getpass, "getpass", explode)
    assert bootstrap.ask_secret("clave: ") == ""
    assert bootstrap.interactive(SimpleNamespace(no_input=False)) is False


# --------------------------------------------------------------- comandos
def test_agent_command_prefers_the_installed_entry_point(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    entry = tmp_path / "bin" / "autonoma"
    entry.parent.mkdir(parents=True)
    entry.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(bootstrap, "venv_executable", lambda v, name, **kwargs: entry)
    assert bootstrap.agent_command(tmp_path, ["--doctor"]) == [str(entry), "--doctor"]

    entry.unlink()
    assert bootstrap.agent_command(tmp_path, ["x"]) == [str(bootstrap.venv_python(tmp_path)), "-m", "autonoma", "x"]


def test_dev_commands_run_inside_the_venv(tmp_path: Path) -> None:
    python = str(bootstrap.venv_python(tmp_path))
    assert bootstrap.dev_command(tmp_path, "test", [])[:3] == [python, "-m", "pytest"]
    assert "tests" in bootstrap.dev_command(tmp_path, "test", [])
    assert bootstrap.dev_command(tmp_path, "types", [])[:2] == [python, "-m"]
    assert bootstrap.dev_command(tmp_path, "build", [])[:3] == [python, "-m", "PyInstaller"]
    with pytest.raises(BootstrapError, match="desconocida"):
        bootstrap.dev_command(tmp_path, "telepatía", [])


def test_run_setup_skips_the_venv_when_it_already_exists(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(bootstrap, "find_python", lambda explicit=None: ([sys.executable], "3.12.1"))
    monkeypatch.setattr(
        bootstrap,
        "_run",
        lambda argv, **kwargs: calls.append(list(map(str, argv))) or subprocess.CompletedProcess(argv, 0, "", ""),
    )
    monkeypatch.setattr(bootstrap, "install_package", lambda *a, **k: None)
    monkeypatch.setattr(bootstrap, "ensure_env_file", lambda **kwargs: True)

    venv = tmp_path / ".venv"
    venv.mkdir()
    (venv / "pyvenv.cfg").write_text("home = /usr\n", encoding="utf-8")
    bootstrap.setup(venv, dry_run=False, interactive=False)
    assert not any("venv" in " ".join(cmd) and "-m" in cmd for cmd in calls)  # no recrea el entorno


def test_dev_commands_do_not_nag_about_the_key(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`bootstrap test` no tiene por qué quejarse de una clave que no usa."""
    seen: dict[str, object] = {}

    def fake_setup(venv: Path, *, dry_run: bool, interactive: bool, ensure_env: bool = True) -> None:
        seen["ensure_env"] = ensure_env

    monkeypatch.setattr(bootstrap, "setup", fake_setup)
    monkeypatch.setattr(bootstrap, "ensure_dev_tools", lambda *a, **k: None)
    monkeypatch.setattr(bootstrap, "venv_python", lambda venv, **k: Path(sys.executable))
    monkeypatch.setattr(bootstrap, "dev_command", lambda venv, name, args: [sys.executable, "-c", "pass"])
    bootstrap.run_in_venv(Path("/tmp/x"), opts("test"), "test")
    assert seen["ensure_env"] is False
    assert "NOTRACK_API_KEY" not in capsys.readouterr().out


def test_the_run_path_prepares_the_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    """`run` sí garantiza el `.env` (con `--no-input`: avisar, no preguntar)."""
    seen: dict[str, object] = {}

    def fake_setup(venv: Path, *, dry_run: bool, interactive: bool, ensure_env: bool = True) -> None:
        seen.update(dry_run=dry_run, interactive=interactive, ensure_env=ensure_env)

    monkeypatch.setattr(bootstrap, "setup", fake_setup)
    monkeypatch.setattr(bootstrap, "agent_command", lambda venv, args: [sys.executable, "-c", "pass", *args])
    monkeypatch.setattr(bootstrap.subprocess, "call", lambda *a, **k: 0)
    bootstrap.run_run(Path("/tmp/x"), opts("run", "--plain", "hola", dry_run=False))
    assert seen == {"dry_run": False, "interactive": False, "ensure_env": True}


def test_install_failure_explains_the_offline_escape_hatch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def failing(argv, **kwargs):
        return subprocess.CompletedProcess(list(argv), 1, "", "error: no se pudo contactar con PyPI")

    monkeypatch.setattr(bootstrap, "_run", failing)
    with pytest.raises(BootstrapError) as excinfo:
        bootstrap.install_package(tmp_path, dry_run=False)
    message = str(excinfo.value)
    assert "PyPI" in message and "dist/Autonoma.exe" in message
    assert "Traceback" not in message


def test_dry_run_never_executes(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    def explode(*args: object, **kwargs: object) -> None:
        raise AssertionError("dry-run no debe ejecutar nada")

    monkeypatch.setattr(subprocess, "run", explode)
    done = bootstrap._run([sys.executable, "-c", "raise SystemExit(3)"], dry_run=True)
    assert done.returncode == 0
    assert "(dry-run)" in capsys.readouterr().out


def test_missing_executable_is_reported_not_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        bootstrap.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("no está"))
    )
    with pytest.raises(BootstrapError, match="No se encontró el ejecutable"):
        bootstrap._run(["autonoma-inexistente-xyz"])


# --------------------------------------------------------------- la CLI del instalador
def opts(command: str, *extra: str, dry_run: bool = True) -> SimpleNamespace:
    return SimpleNamespace(command=command, args=list(extra), python=None, venv=None, no_input=True, dry_run=dry_run)


def test_main_infers_run_for_a_bare_prompt(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """`bootstrap "hola"` = `bootstrap run "hola"`, usando el entry point del entorno."""
    seen: list[list[str]] = []
    entry = bootstrap.venv_executable(tmp_path, "autonoma", windows=(os.name == "nt"))
    entry.parent.mkdir(parents=True)
    entry.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(bootstrap, "resolved_venv", lambda explicit: tmp_path)
    monkeypatch.setattr(bootstrap, "setup", lambda *a, **k: None)
    monkeypatch.setattr(bootstrap.subprocess, "call", lambda command, **k: seen.append(list(command)) or 0)
    assert bootstrap.main(["resume mis notas", "--plain"]) == 0
    assert seen[0][0] == str(entry)  # el entry point del venv, no un `python -c` suelto
    assert seen[0][1:] == ["resume mis notas", "--plain"]  # el prompt llega intacto


def test_main_drops_the_npm_separator(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []
    monkeypatch.setattr(bootstrap, "setup", lambda *a, **k: None)
    monkeypatch.setattr(bootstrap, "ensure_env_file", lambda **k: True)
    monkeypatch.setattr(bootstrap.subprocess, "call", lambda command, **k: seen.append(list(command)) or 0)
    assert bootstrap.main(["run", "--", "hola", "mundo"]) == 0
    assert "--" not in seen[0]
    assert seen[0][-2:] == ["hola", "mundo"]


def test_clean_reports_what_it_removed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    venv = tmp_path / ".venv"
    venv.mkdir()
    removed: list[Path] = []
    monkeypatch.setattr(bootstrap.shutil, "rmtree", lambda path, **k: removed.append(Path(path)))
    assert bootstrap.run_clean(venv, opts("clean", dry_run=False)) == 0
    assert removed == [venv]
    assert bootstrap.run_clean(tmp_path / "otra", opts("clean", dry_run=False)) == 0
    assert "nada que limpiar" in capsys.readouterr().out


def test_status_is_json_ready_and_exit_code_says_if_instalar(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(bootstrap, "already_installed", lambda venv, *, fingerprint, entry_name="autonoma": False)
    monkeypatch.setattr(
        bootstrap, "env_state", lambda: {"path": "x", "exists": False, "has_key": False, "has_brave_key": False}
    )
    assert bootstrap.print_status(tmp_path) == 1  # "falta instalar" también es un estado útil
    out = capsys.readouterr().out
    assert '"installed": false' in out and str(tmp_path) in out
