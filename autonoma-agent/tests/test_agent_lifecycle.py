"""Ciclo de vida del orquestador: métricas por desenlace, limpieza y aislamiento de recursos."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from autonoma.agent import SYSTEM_PROMPT, Agent, build_agent
from autonoma.config import Settings
from autonoma.errors import ConfigurationError, ErrorCode
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


# ════════════════════ puerta de comandos, identidad de autorización y continuidad del tope
def bare_agent(settings: Settings, *, approve: object = None) -> Agent:
    """Agente sin proveedor ni red: basta para probar la puerta de aprobación."""
    return Agent(settings, PanicController(), None, None, None, approve=approve)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "tool,args",
    [
        ("run_command", {"command": "ls"}),
        ("spawn_command", {"command": "sleep 1"}),
        ("kill_job", {"job_id": "j1", "confirm": True}),
    ],
)
def test_los_tres_comandos_exigen_el_flag(tool: str, args: dict[str, object]) -> None:
    """`spawn_command` y `kill_job` no podían esquivar `--allow-commands`: ahora responden igual."""
    agent = bare_agent(Settings())
    outcome = agent.run_tool(tool, args, user_prompt="x")
    assert not outcome.ok
    assert "comandos deshabilitados" in outcome.output


def test_con_el_flag_la_puerta_es_la_aprobacion_humana() -> None:
    agent = bare_agent(Settings(allow_commands=True))
    outcome = agent.run_tool("spawn_command", {"command": "sleep 1"}, user_prompt="x")
    assert not outcome.ok
    assert "requiere aprobación humana" in outcome.output


@pytest.mark.parametrize(
    "args",
    [
        {"command": "git status", "argv": ["git", "status"]},  # las dos formas a la vez
        {},  # ninguna
        {"argv": []},  # lista vacía
        {"argv": ["ok", "  "]},  # elemento en blanco
    ],
)
def test_la_forma_del_comando_se_valida_antes_de_aprobar(args: dict[str, object]) -> None:
    """Nada llega a la puerta de aprobación con una identidad de comando ambigua."""
    aprobada: list[tuple[str, dict]] = []
    agent = bare_agent(
        Settings(allow_commands=True), approve=lambda name, sent, prompt="": aprobada.append((name, dict(sent))) or True
    )
    outcome = agent.run_tool("run_command", args, user_prompt="x")
    assert not outcome.ok
    assert aprobada == []


def test_identidad_de_autorizacion_lleva_el_tool_call_id() -> None:
    """La aprobación se pide por `herramienta + tool_call_id`: dos llamadas gemelas no son la misma."""
    vistos: list[dict[str, str]] = []

    def approve(_name: str, _args: dict, _prompt: str = "") -> bool:
        vistos.append(agent.approval_identity)
        return False

    agent = bare_agent(Settings(allow_commands=True), approve=approve)
    agent.run_tool("run_command", {"argv": ["git", "status"]}, user_prompt="x", tool_call_id="call-77")
    assert vistos == [{"run_command": "call-77"}]
    assert agent.approval_identity == {}  # terminada la consulta, no queda nada colgado


def test_una_negacion_cuenta_como_gasto_de_la_politica(tmp_path: Path) -> None:
    from autonoma.approval import Budget, PolicyState, parse_policy_json

    policy = parse_policy_json(
        '{"roots": ["."], "allow": [{"tool": "list_dir", "paths": ["**"], "requires_approval": false}]}',
        default_root=tmp_path,
    )
    agent = bare_agent(Settings(root_dir=tmp_path, allow_commands=True), approve=lambda *a: False)
    agent.set_approval_policy(policy)
    assert isinstance(agent.policy_state, PolicyState)
    agent.run_tool("delete_path", {"path": str(tmp_path / "x")}, user_prompt="x")
    assert agent.policy_state.denials == 1
    assert agent.policy_state.consecutive_denials == 1
    agent.set_approval_policy(None)
    assert agent.approval_policy is None
    assert agent.policy_state.ops == 0 and isinstance(agent.policy_state.budget, Budget)


# ──────────────────────────────────────────────── el tope de iteraciones no borra el trabajo
def limit_agent(tmp_path: Path, *, content: str | None) -> tuple[Agent, PanicController]:
    panic = PanicController()
    call = [{"id": "c1", "type": "function", "function": {"name": "list_dir", "arguments": {"path": str(tmp_path)}}}]
    client = scripted_client([message(content, call) for _ in range(12)])
    settings = Settings(notrack_api_key="k", root_dir=tmp_path, max_tool_iterations=2)
    return Agent(settings, panic, client, SearchEngine(panic, tmp_path / "kb"), FileSystemManager(panic)), panic


def test_el_corte_por_tope_conserva_el_next_del_modelo(tmp_path: Path) -> None:
    agent, _panic = limit_agent(tmp_path, content="voy por la mitad\nNEXT: leer el informe de cobertura")
    answer = agent.run("sigue")
    assert "límite de pasos" in answer
    assert "NEXT: leer el informe de cobertura" in answer  # no se sustituye la señal: se anade el aviso
    assert answer.count("NEXT:") == 1  # y no se duplica unaContinuidad sintética


def test_el_corte_por_tope_sin_senal_emite_una_sintetica(tmp_path: Path) -> None:
    agent, _panic = limit_agent(tmp_path, content="trabajo a medias")
    answer = agent.run("sigue")
    assert answer.rstrip().endswith("NEXT: retomar el objetivo donde quedó interrumpido por el tope de iteraciones.")
    # El historial guarda el texto con la señal: `--resume` no puede perder cómo seguir.
    turn = agent.history[-1]
    assert "límite de pasos" in turn["content"] and "NEXT:" in turn["content"]


def test_el_presupuesto_de_la_politica_se_reinicia_por_turno(tmp_path: Path) -> None:
    panic = PanicController()
    client = scripted_client([message("listo")])
    agent = Agent(
        Settings(notrack_api_key="k", root_dir=tmp_path),
        panic,
        client,
        SearchEngine(panic, tmp_path),
        FileSystemManager(panic),
    )
    agent.policy_state.ops = 9
    agent.policy_state.consecutive_denials = 4
    agent.run("hola")
    assert agent.policy_state.ops == 0
    assert agent.policy_state.consecutive_denials == 0


# ──────────────────────────────────────────────── el prompt canónico describe el lazo y el techo
@pytest.mark.parametrize(
    "frase",
    [
        "línea de control",
        "NEXT:",
        "BLOCKED:",
        "run_command con `argv`",
        "techo duro de seguridad",
        "aprobación delegada",
        "--allow-commands rige run_command, spawn_command y kill_job",
        "Ni siquiera la política lo",
        "No amplíes tu propio alcance",
        ".env",
        "config.json",
    ],
)
def test_system_prompt_promete_lo_que_el_codigo_cumple(frase: str) -> None:
    """El prompt no puede prometer en silencio: cada garantía del lazo y del techo está anclada aquí."""
    assert frase in SYSTEM_PROMPT


def test_el_archivo_de_prompt_sobrescribe_el_incoporado(tmp_path: Path) -> None:
    from autonoma.agent import read_system_prompt

    archivo = tmp_path / "prompt.txt"
    settings = Settings(root_dir=tmp_path)
    assert read_system_prompt(settings) == SYSTEM_PROMPT
    archivo.write_text("Eres otro agente, con el mismo contrato de herramientas.", encoding="utf-8")
    assert read_system_prompt(replace(settings, system_prompt_file="prompt.txt")).startswith("Eres otro agente")
    (tmp_path / "vacio.txt").write_text("   ", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="está vacío"):
        read_system_prompt(replace(settings, system_prompt_file="vacio.txt"))
    with pytest.raises(ConfigurationError, match="No se pudo leer"):
        read_system_prompt(replace(settings, system_prompt_file="no-existe.txt"))
    archivo.write_text("x" * 40_001, encoding="utf-8")
    with pytest.raises(ConfigurationError, match="excede"):
        read_system_prompt(replace(settings, system_prompt_file="prompt.txt"))


def test_build_agent_pone_su_propia_configuracion_bajo_proteccion(tmp_path: Path) -> None:
    """El agente compuesto por `build_agent` no puede reescribirse a sí mismo: es código, no promesa."""
    (tmp_path / ".env").write_text("NOTRACK_API_KEY=x\n", encoding="utf-8")
    (tmp_path / "config.json").write_text('{"allow_commands": true}', encoding="utf-8")
    panic = PanicController()
    agent = build_agent(Settings(notrack_api_key="k", root_dir=tmp_path), panic, approve=lambda *a: True)
    try:
        for nombre in (".env", "config.json"):
            with pytest.raises(Exception, match="protegida"):
                agent.fs.write_file(str(tmp_path / nombre), "allow_commands = true")
        ok = tmp_path / "nota.txt"
        assert "protegida" not in agent.fs.write_file(str(ok), "contenido")
    finally:
        agent.close_resources()
