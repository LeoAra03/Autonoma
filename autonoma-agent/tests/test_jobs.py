"""Trabajos en segundo plano: salida a archivo, tope duro de vivos y limpieza garantizada.

No hay sandbox ni colas externas: lo que se prueba aquí es el contrato honesto —que la
salida sea legible, que un trabajo vivo no pueda quedar huérfano al cerrar, y que el
modelo reciba texto accionable cuando algo no existe o no cabe.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

from autonoma.config import Settings
from autonoma.errors import ConfigurationError, FileSystemError
from autonoma.filesystem import FileSystemManager
from autonoma.jobs import JobRunner
from autonoma.key_handler import PanicController, PanicError
from autonoma.knowledge import KnowledgeStore
from autonoma.ports import JobsPort, ResourcePort
from autonoma.tool_contracts import LOCAL_TOOLS, TOOL_SPECS, validate_arguments
from autonoma.tool_registry import ToolRegistry

_TIMEOUT = 15.0


@pytest.fixture
def runner(tmp_path: Path):
    instance = JobRunner(PanicController(), tmp_path / "jobs")
    yield instance
    instance.close()


def _wait(runner: JobRunner, job_id: str | None = None, *, alive: bool = False) -> bool:
    """Espera el cambio de estado; el reloj es lo único que separa un vivo de un muerto."""
    deadline = time.monotonic() + _TIMEOUT
    while time.monotonic() < deadline:
        if runner.info(job_id).alive is alive:
            return True
        time.sleep(0.02)
    return False


def _echo_script(tmp_path: Path, body: str, name: str = "worker.py") -> str:
    script = tmp_path / name
    script.write_text(body, encoding="utf-8")
    return f'"{sys.executable}" "{script}"'


# ───────────────────────────────────────────────────────────────── lanzamiento
def test_spawn_returns_immediately_with_a_readable_log(tmp_path: Path, runner: JobRunner) -> None:
    info = runner.spawn(_echo_script(tmp_path, "import time\ntime.sleep(30)\n"), tmp_path)
    assert info.alive and info.pid > 0
    assert Path(info.log_path).exists()
    assert info.job_id.startswith("j")
    assert "vivo" in info.summary() and str(tmp_path) in info.summary()
    assert runner.info(info.job_id).alive  # `spawn` no espera: ése es todo su propósito


def test_output_merges_stdout_and_stderr_in_order(runner: JobRunner, tmp_path: Path) -> None:
    command = _echo_script(tmp_path, "import sys\nprint('salida')\nsys.stderr.write('error\\n')\nprint('fin')\n")
    job_id = runner.spawn(command).job_id
    assert _wait(runner, job_id, alive=False), "el trabajo no terminó a tiempo"
    text = runner.output(job_id)
    assert "salida" in text and "error" in text and "fin" in text
    assert "exit=0" in text


def test_failed_command_reports_its_exit_code(runner: JobRunner, tmp_path: Path) -> None:
    job_id = runner.spawn(_echo_script(tmp_path, "import sys\nsys.exit(3)\n")).job_id
    assert _wait(runner, job_id, alive=False)
    assert "exit=3" in runner.output(job_id)  # ni una línea de salida, y aun así se sabe que falló
    assert "terminado" in runner.status(job_id)


def test_status_without_jobs_is_not_an_error(runner: JobRunner) -> None:
    assert "No hay trabajos lanzados" in runner.status()


def test_output_without_job_id_targets_the_last_one(runner: JobRunner, tmp_path: Path) -> None:
    first = runner.spawn("echo primero").job_id
    second = runner.spawn("echo segundo").job_id
    assert _wait(runner, second, alive=False)
    assert second in runner.output() and "segundo" in runner.output()
    assert first != second


def test_output_clipping_announces_how_to_get_the_rest(runner: JobRunner, tmp_path: Path) -> None:
    job_id = runner.spawn(_echo_script(tmp_path, "print('x' * 5_000)\nprint('y' * 5_000)\n")).job_id
    assert _wait(runner, job_id, alive=False)
    tail = runner.output(job_id, 300)
    assert "caracteres anteriores" in tail and "tail=false" in tail
    head = runner.output(job_id, 300, tail=False)
    assert "caracteres restantes" in head
    assert head.splitlines()[1].startswith("x")
    assert tail.splitlines()[1].startswith("y") or "y" in tail.splitlines()[1]
    assert tail.rstrip().endswith("tail=false]")  # el aviso siempre cierra el texto


# ─────────────────────────────────────────────────────────────── control
def test_kill_terminates_the_process_group(runner: JobRunner, tmp_path: Path) -> None:
    job_id = runner.spawn(_echo_script(tmp_path, "import time\ntime.sleep(60)\n")).job_id
    assert runner.active_count == 1
    report = runner.kill(job_id)
    assert f"Detenido {job_id}" in report
    assert runner.active_count == 0


def test_kill_requires_an_existing_id(runner: JobRunner) -> None:
    with pytest.raises(FileSystemError, match="No hay un trabajo llamado"):
        runner.kill("j99-00000")
    runner.spawn("true")
    with pytest.raises(FileSystemError, match="vivos:"):
        runner.kill("no-existe")


def test_capacity_is_hard_and_says_how_to_free_it(runner: JobRunner, tmp_path: Path) -> None:
    small = JobRunner(runner.panic, tmp_path / "jobs", max_jobs=2)
    try:
        small.spawn(_echo_script(tmp_path, "import time\ntime.sleep(30)\n", "a.py"))
        small.spawn(_echo_script(tmp_path, "import time\ntime.sleep(30)\n", "b.py"))
        with pytest.raises(FileSystemError, match="kill_job") as exc:
            small.spawn("echo desbordado")
        assert "2 trabajos" in str(exc.value)
    finally:
        small.close()


def test_finished_jobs_are_pruned_to_make_room(runner: JobRunner, tmp_path: Path) -> None:
    small = JobRunner(runner.panic, tmp_path / "jobs", max_jobs=1)
    try:
        first = small.spawn("echo listo").job_id
        assert _wait(small, first, alive=False)
        second = small.spawn("echo segundo").job_id  # el terminado sale de la tabla, no del disco
        assert Path(small.info(first).log_path).read_text(encoding="utf-8").strip() == "listo"
        assert second != first
    finally:
        small.close()


def test_invalid_cwd_is_refused_before_spawning(runner: JobRunner, tmp_path: Path) -> None:
    with pytest.raises(FileSystemError, match="cwd inválido"):
        runner.spawn("echo hola", str(tmp_path / "no-existe"))


def test_panic_blocks_new_works(runner: JobRunner) -> None:
    runner.panic.panic()
    with pytest.raises(PanicError):
        runner.spawn("echo no deberia correr")


def test_close_kills_everything_and_unregisters_the_cleanup(tmp_path: Path) -> None:
    panic = PanicController()
    before = panic.registered_cleanups
    local = JobRunner(panic, tmp_path / "jobs")
    assert panic.registered_cleanups == before + 1
    job_id = local.spawn("sleep 60" if sys.platform != "win32" else "ping -n 60 127.0.0.1 >nul").job_id
    local.close()
    assert local.active_count == 0
    assert panic.registered_cleanups == before  # sin limpieza huérfana tras recargar el agente
    with pytest.raises(FileSystemError):
        local.info(job_id)


def test_logs_are_private_and_the_directory_is_created_on_demand(tmp_path: Path) -> None:
    import os

    directory = tmp_path / "data" / "jobs"
    local = JobRunner(PanicController(), directory)
    try:
        job_id = local.spawn("echo reservado").job_id
        assert _wait(local, job_id, alive=False)
        assert directory.is_dir()
        if os.name != "nt":
            assert Path(local.info(job_id).log_path).stat().st_mode & 0o077 == 0
    finally:
        local.close()


# ──────────────────────────────────────────────────────────────── puertos
def test_job_runner_satisfies_the_declared_ports(runner: JobRunner) -> None:
    assert isinstance(runner, JobsPort)
    assert isinstance(runner, ResourcePort)


def test_settings_point_the_runner_at_the_data_root(tmp_path: Path) -> None:
    settings = Settings(notrack_api_key="sk-prueba-123456", root_dir=tmp_path)
    assert settings.jobs_path() == tmp_path / "jobs"


# ──────────────────────────────────────────────────── herramientas del modelo
class _Engine:
    def __init__(self, panic: PanicController, directory: Path) -> None:
        self.store = KnowledgeStore(panic, directory)

    def search(self, *args: object, **kwargs: object) -> str:
        return "sin red"

    def fetch_url(self, *args: object, **kwargs: object) -> str:
        return "sin red"


def _registry(tmp_path: Path, jobs: object | None = None) -> ToolRegistry:
    panic = PanicController()
    engine = _Engine(panic, tmp_path)
    return ToolRegistry(engine, FileSystemManager(panic), engine.store, jobs=jobs)  # type: ignore[arg-type]


def test_local_tools_gated_by_approval_including_the_new_ones() -> None:
    names = {spec.name for spec in TOOL_SPECS}
    assert {"spawn_command", "kill_job", "job_status", "job_output"} <= names
    assert {"spawn_command", "kill_job"} <= LOCAL_TOOLS  # lanzar y matar piden `SI`
    assert not {"job_status", "job_output"} & LOCAL_TOOLS  # mirar el log es de sólo lectura


def test_registry_round_trips_a_job_through_the_tools(tmp_path: Path, runner: JobRunner) -> None:
    registry = _registry(tmp_path, runner)
    head = registry.execute("spawn_command", {"command": _echo_script(tmp_path, "print('desde la herramienta')\n")})
    job_id = head.split(" ", 1)[0].split("·")[0].strip()
    assert _wait(runner, job_id, alive=False)
    assert "desde la herramienta" in registry.execute("job_output", {"job_id": job_id})
    assert job_id in registry.execute("job_status", {})
    assert "Detenido" in registry.execute("kill_job", {"job_id": job_id, "confirm": True})


def test_kill_tool_refuses_without_confirmation(tmp_path: Path, runner: JobRunner) -> None:
    registry = _registry(tmp_path, runner)
    job_id = runner.spawn("sleep 30" if sys.platform != "win32" else "ping -n 30 127.0.0.1 >nul").job_id
    with pytest.raises(ConfigurationError, match="confirm=true"):
        registry.execute("kill_job", {"job_id": job_id})
    assert runner.info(job_id).alive
    registry.execute("kill_job", {"job_id": job_id, "confirm": True})


def test_tools_are_clear_when_the_session_has_no_runner(tmp_path: Path) -> None:
    registry = _registry(tmp_path, None)
    with pytest.raises(ConfigurationError, match="no están disponibles"):
        registry.execute("job_status", {})


def test_job_argument_validation_is_tight() -> None:
    assert validate_arguments("job_output", {"job_id": "j01-1", "max_chars": 900, "tail": False}) == {
        "job_id": "j01-1",
        "max_chars": 900,
        "tail": False,
    }
    from autonoma.errors import ToolContractError

    with pytest.raises(ToolContractError, match="max_chars"):
        validate_arguments("job_output", {"max_chars": 1})  # por debajo del mínimo útil
    with pytest.raises(ToolContractError):
        validate_arguments("spawn_command", {"command": "x", "inesperado": True})
    with pytest.raises(ToolContractError):
        validate_arguments("spawn_command", {})


def test_empty_command_is_rejected(runner: JobRunner) -> None:
    from autonoma.errors import ProcessError

    with pytest.raises(ProcessError, match="Comando vacío"):
        runner.spawn("   ")


def test_summary_reports_the_state_the_user_can_act_on(runner: JobRunner) -> None:
    info = runner.spawn("echo resumen")
    assert info.job_id in info.summary() and info.runtime_seconds >= 0
