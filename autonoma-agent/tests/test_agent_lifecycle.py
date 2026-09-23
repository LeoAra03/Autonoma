"""Ciclo de vida del orquestador: métricas por desenlace, limpieza y aislamiento de recursos."""

from __future__ import annotations

from pathlib import Path

import pytest

from autonoma.agent import SYSTEM_PROMPT, Agent, build_agent
from autonoma.config import Settings
from autonoma.errors import ErrorCode
from autonoma.filesystem import FileSystemManager
from autonoma.key_handler import PanicController, PanicError


def PanicError_() -> type[Exception]:
    """La excepción de cancelación vive en key_handler; el test la referencia sin importarla dos veces."""
    return PanicError


from autonoma.notrack_client import NoTrackClient
from autonoma.observability import MetricsRegistry
from autonoma.search_engine import SearchEngine


def scripted_client(
    replies: list[dict[str, object]], *, seen: list[list[dict[str, str]]] | None = None
) -> NoTrackClient:
    """Cliente NoTrack con respuestas fijas: el bucle del agente, sin red."""
    client = NoTrackClient("test-key-123456", PanicController())
    iterator = iter(replies)

    def chat(messages, **kwargs):
        if seen is not None:
            seen.append(list(messages))
        return next(iterator)

    client.chat = chat  # type: ignore[method-assign]
    return client


def message(content: object = None, calls: list[dict[str, object]] | None = None) -> dict[str, object]:
    return {"choices": [{"message": {"content": content, "tool_calls": calls}}]}


def make_agent(tmp_path: Path, client: NoTrackClient, panic: PanicController) -> Agent:
    settings = Settings(notrack_api_key="test-key-123456", root_dir=tmp_path)
    return Agent(settings, panic, client, SearchEngine(panic, tmp_path / "kb"), FileSystemManager(panic))


def test_successful_turn_counts_once_and_reports_timing(tmp_path: Path) -> None:
    panic = PanicController()
    metrics = MetricsRegistry()
    client = NoTrackClient("test-key-123456", panic)
    client.chat = lambda *a, **k: message("listo")  # type: ignore[method-assign]
    agent = Agent(
        Settings(notrack_api_key="k"),
        panic,
        client,
        SearchEngine(panic, tmp_path),
        FileSystemManager(panic),
        metrics=metrics,
    )
    events: list[tuple[str, str]] = []
    assert agent.run("pregunta", lambda kind, text: events.append((kind, text))) == "listo"
    counters = metrics.counters()
    assert counters["turn.started"] == 1
    assert counters["turn.ok"] == 1  # no se cuenta dos veces: ni `completed` ni `finished`
    assert "turn.failed" not in counters and "turn.cancelled" not in counters
    kinds = [kind for kind, _ in events]
    assert kinds[0] == "think" and kinds[-1] == "timing"
    assert "answer" in kinds
    assert metrics.summary("turn") is not None


def test_failed_turn_counts_only_failure(tmp_path: Path) -> None:
    panic = PanicController()
    metrics = MetricsRegistry()
    agent = make_agent(tmp_path, scripted_client([message("x")]), panic)
    agent.metrics = metrics

    def broken(messages, **kwargs):
        raise RuntimeError("provider roto")

    agent.notrack.chat = broken  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        agent.run("pregunta")
    counters = metrics.counters()
    assert counters["turn.failed"] == 1 and "turn.ok" not in counters


def _cancelling_chat(panic: PanicController):
    from autonoma.key_handler import PanicError

    def chat(messages, **kwargs):
        panic.panic()
        raise PanicError

    return chat


def test_cancelled_turn_is_not_an_error(tmp_path: Path) -> None:
    panic = PanicController()
    metrics = MetricsRegistry()
    agent = make_agent(tmp_path, scripted_client([message("x")]), panic)
    agent.metrics = metrics
    agent.notrack.chat = _cancelling_chat(panic)  # type: ignore[method-assign]
    with pytest.raises(PanicError_()):
        agent.run("pregunta")
    counters = metrics.counters()
    assert counters["turn.cancelled"] == 1
    assert "turn.failed" not in counters
    panic.reset()


def test_close_unregisters_cleanups_so_rebuild_does_not_leak(tmp_path: Path) -> None:
    """Reconstruir el agente (p. ej. tras `/key`) no debe acumular limpiezas sobre objetos muertos."""
    panic = PanicController()
    settings = Settings(notrack_api_key="test-key-123456", root_dir=tmp_path)
    first = build_agent(settings, panic)
    registered_after_first = panic.registered_cleanups
    assert registered_after_first > 0
    first.close()
    assert panic.registered_cleanups == 0
    second = build_agent(settings, panic)
    assert panic.registered_cleanups == registered_after_first
    second.close()


def test_double_close_is_safe_and_resources_are_released(tmp_path: Path) -> None:
    panic = PanicController()
    agent = build_agent(Settings(notrack_api_key="test-key-123456", root_dir=tmp_path), panic)
    agent.close()
    agent.close()
    assert panic.registered_cleanups == 0
    assert agent.resources is None


def test_unknown_and_local_tools_never_reach_the_registry(tmp_path: Path) -> None:
    agent = make_agent(tmp_path, scripted_client([]), PanicController())
    unknown = agent.run_tool("inexistent", {})
    assert not unknown.ok and unknown.error_code is ErrorCode.TOOL_CONTRACT
    denied = agent.run_tool("read_file", {"path": str(tmp_path / "secreto")})
    assert not denied.ok and denied.requires_approval
    assert "secreto" not in denied.output


def test_history_window_survives_a_turn_and_is_capped(tmp_path: Path) -> None:
    call = {"type": "function", "id": "1", "function": {"name": "mkdir", "arguments": '{"path":"x"}'}}
    seen: list[list[dict[str, str]]] = []
    client = scripted_client([message(calls=[call]), message("fin")], seen=seen)
    panic = PanicController()
    agent = make_agent(tmp_path, client, panic)
    assert agent.run("crea") == "fin"
    assert len(agent.history) <= 32
    snapshot = agent.history
    snapshot.clear()  # la vista es copia: vaciarla no toca el historial real
    assert agent.history


def test_tool_message_carries_the_denial_back_to_the_model(tmp_path: Path) -> None:
    call = {"type": "function", "id": "7", "function": {"name": "run_command", "arguments": '{"command":"id"}'}}
    seen: list[list[dict[str, str]]] = []
    client = scripted_client([message(calls=[call]), message("cancelado por política")], seen=seen)
    agent = make_agent(tmp_path, client, PanicController())
    assert agent.run("ejecuta") == "cancelado por política"
    tool_messages = [entry for entry in seen[1] if entry["role"] == "tool"]
    assert tool_messages and "ERROR" in tool_messages[-1]["content"]


def test_cancellation_leaves_no_busy_flag_or_live_processes(tmp_path: Path) -> None:
    """Un turno cancelado devuelve el estado a idle y no deja hijos colgando."""
    panic = PanicController()
    fs = FileSystemManager(panic)
    agent = make_agent(tmp_path, scripted_client([message("x")]), panic)
    agent.fs = fs
    agent.notrack.chat = _cancelling_chat(panic)  # type: ignore[method-assign]
    with pytest.raises(PanicError_()):
        agent.run("cualquiera")
    assert not panic.busy
    assert fs.active_processes == 0
    panic.reset()


@pytest.mark.parametrize(
    "needle",
    ["edit_file", "start_line", "search_files", "fetch_url", "start_char", "spawn_command", "job_output", "kill_job"],
)
def test_system_prompt_teaches_every_capability(needle: str) -> None:
    """Una herramienta que el modelo no sabe que existe no existe: el prompt es parte del contrato."""
    assert needle in SYSTEM_PROMPT
