"""Supervisor de procesos: límites de salida, timeout con kill-tree y cancelación."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from autonoma import processes
from autonoma.errors import ConfigurationError, ProcessLaunchError, ProcessTimeoutError
from autonoma.filesystem import FileSystemManager
from autonoma.key_handler import PanicController
from autonoma.processes import CommandResult, prepare_command, terminate_process

PY = sys.executable


def manager(timeout: float = 20.0) -> FileSystemManager:
    return FileSystemManager(PanicController(), command_timeout=timeout)


def run(command: str | list[str], *, timeout: float | None = None, shell: bool = True) -> CommandResult:
    return manager().run_command(command, cwd=str(Path.cwd()), timeout=timeout, shell=shell)


def test_result_is_structured_and_displayable() -> None:
    result = run([PY, "-c", "print('hola')"], shell=False)
    assert result.succeeded and result.returncode == 0
    assert result.stdout.strip() == "hola"
    payload = result.as_dict()
    assert payload["cwd"] and payload["duration_ms"] >= 0.0
    assert payload["truncated"] is False
    assert tuple(payload["command"]) == (PY, "-c", "print('hola')")
    text = result.format_for_model()
    assert "exit=0" in text and "--- stdout ---" in text and "stdout:" not in text.splitlines()[0]


def test_nonzero_exit_is_reported_not_raised() -> None:
    result = run([PY, "-c", "import sys; sys.stderr.write('mal\\n'); sys.exit(3)"], shell=False)
    assert not result.succeeded and result.returncode == 3
    assert result.stderr.strip() == "mal"


def test_large_output_is_tail_bounded_and_flagged() -> None:
    result = run([PY, "-c", "import sys; sys.stdout.write('x'*200000)"], shell=False)
    assert len(result.stdout) <= 12_000
    assert result.truncated and "salida truncada" in result.format_for_model()


def test_timeout_kills_the_process_and_leaves_no_active_handles() -> None:
    fs = manager()
    with pytest.raises(ProcessTimeoutError, match="Timeout"):
        fs.run_command([PY, "-c", "import time; time.sleep(30)"], cwd=str(Path.cwd()), shell=False, timeout=0.3)
    assert fs.active_processes == 0


def test_panic_cancels_a_running_command() -> None:
    panic = PanicController()
    fs = FileSystemManager(panic)
    panic.panic()
    with pytest.raises(Exception) as excinfo:  # PanicError desde el propio worker
        fs.run_command([PY, "-c", "import time; time.sleep(30)"], cwd=str(Path.cwd()), shell=False, timeout=10)
    assert type(excinfo.value).__name__ == "PanicError"
    assert fs.active_processes == 0
    panic.reset()


def test_timeout_boundaries_are_validated_before_spawning() -> None:
    with pytest.raises(ConfigurationError, match="timeout"):
        run("echo x", timeout=0)
    with pytest.raises(ConfigurationError, match="timeout"):
        run("echo x", timeout=float("nan"))
    with pytest.raises(ConfigurationError, match="timeout"):
        run("echo x", timeout=10_000)


@pytest.mark.parametrize("command", ["", "   ", "\x00", []])
def test_ambiguous_commands_are_rejected(command: str | list[str]) -> None:
    with pytest.raises(ProcessLaunchError):
        prepare_command(command, shell=True)


def test_argv_list_requires_shell_false() -> None:
    """Con `shell=True` una lista ejecuta sólo el primer elemento: se exige decisión explícita."""
    with pytest.raises(ProcessLaunchError, match="shell=False"):
        prepare_command(["echo", "hola"], shell=True)
    assert prepare_command(["echo", "hola"], shell=False) == ["echo", "hola"]


def test_crlf_and_bom_are_normalized_in_stdout() -> None:
    result = run(
        [PY, "-c", "import sys; sys.stdout.buffer.write(b'\\xef\\xbb\\xbflinea1\\r\\nlinea2\\r\\n')"], shell=False
    )
    assert result.stdout == "linea1\nlinea2\n"


def test_terminate_process_is_idempotent_on_dead_children() -> None:
    import subprocess

    process = subprocess.Popen([PY, "-c", "pass"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    process.wait(timeout=10)
    terminate_process(process)  # no lanza aunque el hijo ya haya muerto
    terminate_process(process)


def test_non_windows_path_kills_without_job_objects(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fuera de Windows no hay job object: se mata por señal y no queda nada vivo."""
    import subprocess

    process = subprocess.Popen([PY, "-c", "import time; time.sleep(30)"], start_new_session=True)
    pid = process.pid
    monkeypatch.setattr(processes, "_is_windows", lambda: False)
    terminate_process(process)
    _assert_dead(pid)


def test_fallback_when_the_platform_has_no_process_groups(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sin `killpg` (plataforma exótica o Windows forzando la rama) se degrada a terminate/kill."""
    import subprocess

    process = subprocess.Popen([PY, "-c", "import time; time.sleep(30)"], start_new_session=True)
    pid = process.pid
    monkeypatch.setattr(processes, "_is_windows", lambda: False)
    monkeypatch.delattr(os, "killpg", raising=False)
    monkeypatch.delattr(os, "getpgid", raising=False)
    terminate_process(process)  # no debe lanzar aunque falte la API de grupos
    _assert_dead(pid)


def test_windows_branch_delegates_to_the_tree_killer(monkeypatch: pytest.MonkeyPatch) -> None:
    """La rama de Windows se delega en un único helpers; aquí se verifica el enrutado."""
    calls: list[int] = []
    monkeypatch.setattr(processes, "_is_windows", lambda: True)
    monkeypatch.setattr(processes, "_kill_windows_tree", lambda proc: calls.append(proc.pid))
    terminate_process(SimpleNamespace(pid=4242, poll=lambda: None))
    assert calls == [4242]


def _assert_dead(pid: int) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, PermissionError):
            return
        time.sleep(0.05)
    pytest.fail("el proceso sobrevivió a terminate_process")
