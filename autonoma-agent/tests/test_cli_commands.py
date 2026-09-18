"""La CLI: estilo y ciclo de vida separados de la lógica, códigos de salida y enrutado de comandos."""

from __future__ import annotations

import io
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from autonoma import __version__
from autonoma.cli import (
    _COMMAND_TABLE,
    HELP_TEXT,
    PlainConsole,
    Session,
    _configure_stdio,
    _ProgressReporter,
    _read_secret,
    banner_body,
    build_context,
    build_parser,
    main,
    parse_args,
    print_banner,
    repl,
)
from autonoma.config import Settings
from autonoma.errors import ConfigurationError, ExitCode
from autonoma.key_handler import ListenerState, ListenerStatus
from autonoma.observability import MetricsRegistry
from autonoma.runtime import DataOrigin, DataRoot, RenderPreferences, RuntimeContext


class RecordingConsole:
    """Doble de `ConsolePort`: captura lo impreso y responde lo programado."""

    def __init__(self, responses: list[str] | None = None) -> None:
        self.lines: list[str] = []
        self._responses = list(responses or [])

    def print(self, *objects: object, **kwargs: object) -> None:
        self.lines.append(" ".join(str(item) for item in objects))

    def input(self, prompt: str = "") -> str:
        if not self._responses:
            raise EOFError
        return self._responses.pop(0)

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


class FakeRuntime:
    def __init__(self) -> None:
        self.closed = 0
        self.handlers: tuple[object, ...] = ()
        self.log_file: Path | None = None

    def close(self) -> None:
        self.closed += 1


class FakeAgent:
    def __init__(self, *, answer: str = "respuesta", fail: Exception | None = None) -> None:
        self.notrack = SimpleNamespace(configured=True)
        self.notes = SimpleNamespace(list_notes=lambda limit=40: [Path("nota.md")])
        self.reset_calls = 0
        self.closed = 0
        self.prompts: list[str] = []
        self.approve = None
        self._answer = answer
        self._fail = fail

    def run(self, prompt: str, *, on_event=None) -> str:
        self.prompts.append(prompt)
        if on_event is not None:
            on_event("tool", "read_file(path=nota.md)")
        if self._fail is not None:
            raise self._fail
        return self._answer

    def reset_history(self) -> None:
        self.reset_calls += 1

    def close_resources(self) -> None:
        self.closed += 1


@pytest.fixture
def made_session(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Fábrica de `Session` con dependencias falsas: sin red, sin teclado, sin FS real."""

    build_calls: list[dict[str, object]] = []

    def make(*, agent: FakeAgent | None = None, allow_commands: bool = False, responses: list[str] | None = None):
        settings = Settings(notrack_api_key="sk-probe-123456", root_dir=tmp_path)
        runtime = FakeRuntime()
        fake = agent or FakeAgent()
        monkeypatch.setattr("autonoma.cli.load_settings", lambda **kwargs: settings)
        monkeypatch.setattr("autonoma.cli.configure_logging", lambda **kwargs: runtime)
        monkeypatch.setattr(
            "autonoma.cli.KeyHandler",
            lambda panic, *, enabled: SimpleNamespace(
                status=ListenerStatus(ListenerState.DISABLED, "simulado"),
                start=lambda: ListenerStatus(ListenerState.DISABLED, "simulado"),
                stop=lambda: None,
            ),
        )

        def fake_build(*args: object, **kwargs: object) -> FakeAgent:
            build_calls.append(kwargs)
            return fake

        monkeypatch.setattr("autonoma.cli.build_agent", fake_build)
        console = RecordingConsole(responses)
        context = RuntimeContext(
            data_root=DataRoot(tmp_path, DataOrigin.FLAG), render=RenderPreferences(plain=True)
        )
        session = Session(console, allow_commands=allow_commands, global_hotkey=False, context=context)
        return session, console, runtime, fake

    make.calls = build_calls  # type: ignore[attr-defined]
    return make


def test_session_wires_settings_console_and_logging(made_session) -> None:
    session, _console, runtime, agent = made_session()
    assert session.settings.has_notrack_key
    assert session.agent is agent
    assert session.handler.status.state is ListenerState.DISABLED
    session.close()
    assert runtime.closed == 1
    assert agent.closed == 1


def test_run_prompt_returns_answer_and_success_code(made_session) -> None:
    session, console, _, agent = made_session()
    assert session.run_prompt("hola") == int(ExitCode.SUCCESS)
    assert agent.prompts == ["hola"]
    assert "respuesta" in console.text


def test_run_prompt_requires_configuration(made_session) -> None:
    agent = FakeAgent()
    agent.notrack = SimpleNamespace(configured=False)
    session, console, _, _ = made_session(agent=agent)
    assert session.run_prompt("hola") == int(ExitCode.CONFIGURATION)
    assert agent.prompts == []
    assert "/key" in console.text


def test_run_prompt_maps_typed_errors_to_exit_codes(made_session) -> None:
    session, console, _, _ = made_session(agent=FakeAgent(fail=ConfigurationError("falta config")))
    assert session.run_prompt("hola") == int(ExitCode.CONFIGURATION)
    assert "falta config" in console.text


def test_unexpected_errors_never_dump_tracebacks_to_the_user(made_session) -> None:
    session, console, _, _ = made_session(agent=FakeAgent(fail=RuntimeError("trace interno")))
    assert session.run_prompt("hola") == int(ExitCode.INTERNAL)
    assert "Traceback" not in console.text
    assert "autonoma.log" in console.text


def test_cancellation_is_reported_and_panic_is_armed(made_session) -> None:
    session, console, _, _ = made_session(agent=FakeAgent(fail=KeyboardInterrupt()))
    assert session.run_prompt("hola") == int(ExitCode.CANCELLED)
    assert session.panic.is_set
    assert "Detenido por usuario" in console.text


def test_status_never_prints_the_api_key(made_session) -> None:
    session, console, _, _ = made_session()
    session.status()
    assert "sk-probe-123456" not in console.text
    assert "NoTrack key : sí" in console.text
    assert "nota.md" in console.text


@pytest.mark.parametrize("raw", ["/exit", "/quit", "/q"])
def test_exit_commands_end_the_repl(made_session, raw: str) -> None:
    session, _, _, _ = made_session()
    assert session.handle_command(raw) is False


@pytest.mark.parametrize("raw", ["/help", "/h", "/?"])
def test_help_commands_print_the_catalog(made_session, raw: str) -> None:
    session, console, _, _ = made_session()
    assert session.handle_command(raw) is True
    assert HELP_TEXT.splitlines()[0] in console.text
    assert session.handle_command("ayuda") is True  # sin barra no es comando: sigue vivo
    assert "Comando desconocido" in console.text


def test_unknown_command_keeps_the_repl_alive(made_session) -> None:
    session, console, _, _ = made_session()
    assert session.handle_command("/noexiste") is True
    assert "Comando desconocido" in console.text


def test_command_routing_is_case_insensitive_and_ignores_extra_args(made_session) -> None:
    session, _, _, agent = made_session()
    assert session.handle_command("/CLEAR    ") is True
    assert agent.reset_calls == 1


def test_every_table_entry_is_a_callable_handler() -> None:
    assert set(_COMMAND_TABLE) >= {"/exit", "/help", "/status", "/clear", "/kb", "/key", "/brave"}
    assert all(callable(handler) for handler in _COMMAND_TABLE.values())


def test_panic_command_trips_the_controller(made_session) -> None:
    session, console, _, _ = made_session()
    assert session.handle_command("/panic") is True
    assert session.panic.is_set
    assert "Detenido" in console.text


def test_approval_without_a_tty_is_denied_without_prompting(made_session, monkeypatch: pytest.MonkeyPatch) -> None:
    """Un entorno sin terminal (CI, `--prompt` piping) jamás autoriza por sí solo."""
    session, console, _, _ = made_session()
    monkeypatch.setattr("autonoma.cli.sys.stdin.isatty", lambda: False, raising=True)
    assert session.approve("delete_path", {"path": "/x"}, "borrar?") is False
    assert console.lines == []
    assert not session.panic.is_set


def test_approval_requires_explicit_yes_and_restores_busy(made_session, monkeypatch: pytest.MonkeyPatch) -> None:
    session, _, _, _ = made_session(responses=["SI"])
    monkeypatch.setattr("autonoma.cli.sys.stdin.isatty", lambda: True, raising=True)
    session.panic.mark_busy()
    assert session.approve("run_command", {"command": "ls"}, "borrar?") is True
    assert session.panic.busy  # el estado de trabajo se restaura tras preguntar


def test_cancelling_the_prompt_trips_panic(made_session, monkeypatch: pytest.MonkeyPatch) -> None:
    session, _, _, _ = made_session(responses=[])  # RecordingConsole lanza EOFError al agotarse
    monkeypatch.setattr("autonoma.cli.sys.stdin.isatty", lambda: True, raising=True)
    assert session.approve("write_file", {"path": "/x"}, "") is False
    assert session.panic.is_set


def test_replies_that_are_not_the_confirmation_word_deny(made_session, monkeypatch: pytest.MonkeyPatch) -> None:
    session, _, _, _ = made_session(responses=["sí", "yes", "SI por favor"])
    monkeypatch.setattr("autonoma.cli.sys.stdin.isatty", lambda: True, raising=True)
    assert session.approve("mkdir", {"path": "/x"}, "") is False
    assert session.approve("mkdir", {"path": "/x"}, "") is False
    assert session.approve("mkdir", {"path": "/x"}, "") is False


def test_progress_reporter_in_plain_mode_stays_quiet(made_session) -> None:
    session, console, _, _ = made_session()
    reporter = _ProgressReporter(console, session.render)
    reporter.start()
    reporter.on_event("tool", "read_file(path=x)")
    reporter.stop()
    assert console.lines  # cada evento se imprime una vez, sin hilos de spinner


def test_quiet_mode_suppresses_tool_previews_only(made_session) -> None:
    """`--quiet` oculta las previsualizaciones, nunca el progreso ni los avisos."""
    _, console, _, _ = made_session()
    loud = _ProgressReporter(console, RenderPreferences(plain=True))
    loud.on_event("tool_result", "contenido de nota.md")
    quiet = _ProgressReporter(console, RenderPreferences(plain=True, quiet=True))
    quiet.on_event("tool_result", "contenido de nota.md")
    quiet.on_event("timing", "42 ms")
    assert len(console.lines) == 2
    assert "contenido" in console.lines[0] and "42 ms" in console.lines[1]


def test_empty_brave_key_does_not_wipe_the_stored_one(made_session, monkeypatch: pytest.MonkeyPatch) -> None:
    """Un Enter en `/brave` es "más tarde", nunca un borrado destructivo de la clave."""
    written: list[dict[str, object]] = []
    monkeypatch.setattr("autonoma.cli._read_secret", lambda prompt: "")
    monkeypatch.setattr(
        "autonoma.cli.save_api_keys",
        lambda **kwargs: written.append(kwargs) or {},
    )
    session, console, _, _ = made_session()
    assert session.handle_command("/brave") is True
    assert written == []
    assert "sin cambios" in console.text


def test_brave_key_is_persisted_and_the_agent_rebuilt(made_session, monkeypatch: pytest.MonkeyPatch) -> None:
    rebuilt: list[int] = []
    monkeypatch.setattr("autonoma.cli._read_secret", lambda prompt: "brave-abc-123")
    monkeypatch.setattr("autonoma.cli.save_api_keys", lambda **kwargs: {"BRAVE_API_KEY": "brave-abc-123"})
    monkeypatch.setattr(Session, "rebuild_agent", lambda self: rebuilt.append(1))
    session, console, _, _ = made_session()
    assert session.handle_command("/brave") is True
    assert rebuilt == [1]
    assert session.settings.has_brave_key
    assert "actualizada" in console.text


def test_kb_command_lists_notes_or_says_it_is_empty(made_session) -> None:
    session, console, _, _ = made_session()
    assert session.handle_command("/kb") is True
    assert "nota.md" in console.text
    session.agent.notes = SimpleNamespace(list_notes=lambda limit=40: [])
    console.lines.clear()
    session.handle_command("/kb")
    assert "vacía" in console.text


def test_status_command_routes_through_the_table(made_session) -> None:
    session, console, _, _ = made_session()
    assert session.handle_command("/status") is True
    assert "Modelo" in console.text and "shell local" in console.text


def test_configure_stdio_tolerates_streams_without_reconfigure(monkeypatch: pytest.MonkeyPatch) -> None:
    """En un entorno con flujos sustituidos (captura de pytest) no se puede romper el arranque."""
    monkeypatch.setattr("autonoma.cli.sys.stdout", object())
    monkeypatch.setattr("autonoma.cli.sys.stderr", object())
    _configure_stdio()


def test_configure_stdio_applies_utf8_when_available(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict[str, str]] = []

    class Stream:
        def reconfigure(self, **kwargs: str) -> None:
            seen.append(kwargs)

    class Refusing:
        def reconfigure(self, **kwargs: str) -> None:
            raise OSError("flujo cerrado")

    monkeypatch.setattr("autonoma.cli.sys.stdout", Stream())
    monkeypatch.setattr("autonoma.cli.sys.stderr", Refusing())
    _configure_stdio()  # el segundo fallo no puede impedir que el primero se aplique
    assert seen == [{"encoding": "utf-8", "errors": "replace"}]


def test_read_secret_treats_interruption_as_defer(monkeypatch: pytest.MonkeyPatch) -> None:
    def eof(prompt: str = "") -> str:
        raise EOFError

    monkeypatch.setattr("autonoma.cli.getpass", eof)
    assert _read_secret("clave: ") == ""

    def interrupted(prompt: str = "") -> str:
        raise KeyboardInterrupt

    monkeypatch.setattr("autonoma.cli.getpass", interrupted)
    assert _read_secret("clave: ") == ""


def test_banner_shows_key_and_hotkey_state(made_session) -> None:
    session, console, _, _ = made_session()
    body = banner_body(session.settings, session.listener_status)
    assert "clave configurada" in body
    assert "simulado" in body
    print_banner(console, session.context, session.settings, session.listener_status)
    assert any("====" in line for line in console.lines)  # modo plano: marco de texto


def test_repl_single_prompt_runs_and_closes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """`autonoma "prompt"`: un turno, código de salida y cierre garantizado del recurso."""
    closed: list[str] = []

    class OnceSession:
        def __init__(self, *args: object, **kwargs: object) -> None:
            self.console = RecordingConsole()
            self.context = RuntimeContext(
                data_root=DataRoot(tmp_path, DataOrigin.FLAG), render=RenderPreferences(plain=True)
            )
            self.settings = Settings(notrack_api_key="k", root_dir=tmp_path)

        @property
        def listener_status(self) -> object:
            return ListenerStatus(ListenerState.DISABLED, "simulado")

        def run_prompt(self, prompt: str) -> int:
            closed.append(f"turno:{prompt}")
            return int(ExitCode.SUCCESS)

        def close(self) -> None:
            closed.append("close")

    monkeypatch.setattr("autonoma.cli.Session", OnceSession)
    code = repl("investiga x", context=RuntimeContext(data_root=DataRoot(tmp_path, DataOrigin.FLAG), render=RenderPreferences(plain=True)))
    assert code == int(ExitCode.SUCCESS)
    assert closed == ["turno:investiga x", "close"]


def test_repl_loop_handles_commands_and_exits(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    lines: list[str] = []

    class LoopSession:
        def __init__(self, *args: object, **kwargs: object) -> None:
            self.console = RecordingConsole(responses=["/help", "   ", "hola", "/exit"])
            self.context = RuntimeContext(
                data_root=DataRoot(tmp_path, DataOrigin.FLAG), render=RenderPreferences(plain=True)
            )
            self.settings = Settings(notrack_api_key="k", root_dir=tmp_path)
            self.closed = False

        @property
        def listener_status(self) -> object:
            return ListenerStatus(ListenerState.DISABLED, "simulado")

        def handle_command(self, raw: str) -> bool:
            lines.append(f"cmd:{raw}")
            return raw != "/exit"

        def run_prompt(self, prompt: str) -> int:
            lines.append(f"turno:{prompt}")
            return int(ExitCode.SUCCESS)

        def close(self) -> None:
            self.closed = True

    session_holder: dict[str, LoopSession] = {}

    def factory(*args: object, **kwargs: object) -> LoopSession:
        session_holder["s"] = LoopSession()
        return session_holder["s"]

    monkeypatch.setattr("autonoma.cli.Session", factory)
    assert repl(context=RuntimeContext(data_root=DataRoot(tmp_path, DataOrigin.FLAG), render=RenderPreferences(plain=True))) == int(ExitCode.SUCCESS)
    assert lines == ["cmd:/help", "turno:hola", "cmd:/exit"]
    assert session_holder["s"].closed is True


def test_repl_exits_cleanly_on_end_of_input(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Ctrl+D / fin de tubería: el REPL se despide y cierra, sin código de error."""
    monkeypatch.setattr("autonoma.cli.Session", lambda *a, **k: LoopSessionEof(tmp_path))
    assert repl(context=RuntimeContext(data_root=DataRoot(tmp_path, DataOrigin.FLAG), render=RenderPreferences(plain=True))) == int(ExitCode.SUCCESS)


class LoopSessionEof:
    def __init__(self, tmp_path: Path) -> None:
        self.console = RecordingConsole(responses=[])  # EOF inmediato
        self.context = RuntimeContext(data_root=DataRoot(tmp_path, DataOrigin.FLAG), render=RenderPreferences(plain=True))
        self.settings = Settings(notrack_api_key="k", root_dir=tmp_path)
        self.closed = False

    @property
    def listener_status(self) -> object:
        return ListenerStatus(ListenerState.DISABLED, "simulado")

    def close(self) -> None:
        self.closed = True


def test_main_module_entrypoint_is_callable() -> None:
    """`python -m autonoma` debe existir y delegar en `cli.main` (el `__main__` del paquete)."""
    import runpy

    called: list[list[str]] = []

    def fake_main(argv: list[str] | None = None) -> None:
        called.append(list(argv or []))

    import autonoma.cli as cli_module

    original = cli_module.main
    cli_module.main = fake_main  # type: ignore[assignment]
    try:
        runpy.run_module("autonoma", run_name="__main__", alter_sys=True)
    finally:
        cli_module.main = original  # type: ignore[assignment]
    assert called == [[]]


def test_plain_console_forwards_text_and_swallows_style_kwargs() -> None:
    buffer = io.StringIO()
    original = sys.stdout
    sys.stdout = buffer
    try:
        PlainConsole().print("hola", style="ok", highlight=False)
    finally:
        sys.stdout = original
    assert buffer.getvalue() == "hola\n"


def test_plain_console_reads_input_through_the_console_port(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []
    console = PlainConsole()

    def fake_input(prompt: str = "") -> str:
        seen.append(prompt)
        return "SI"

    monkeypatch.setattr("builtins.input", fake_input)
    assert console.input("? ") == "SI"
    assert seen == ["? "]


def test_the_session_registry_is_injected_into_the_agent(made_session) -> None:
    """El orquestador y la UI comparten un único registro: `/status` no reimplementa contadores."""
    session, _, _, _ = made_session()
    captured = made_session.calls[0] if made_session.calls else {}
    session.metrics.increment("turn.ok")
    assert captured["metrics"] is session.metrics
    assert isinstance(session.metrics, MetricsRegistry)
    assert session.metrics_snapshot()["counters"]["turn.ok"] == 1


# ------------------------------------------------------------------ parser
def test_parser_accepts_single_prompt_and_flags() -> None:
    args = parse_args(["investiga", "x", "--plain", "--quiet"])
    assert args.prompt == ["investiga", "x"]
    assert args.plain and args.quiet


def test_json_requires_a_diagnostic_mode() -> None:
    with pytest.raises(SystemExit):
        parse_args(["--json"])
    assert parse_args(["--doctor", "--json"]).json is True


def test_doctor_refuses_prompts_and_risky_switches() -> None:
    with pytest.raises(SystemExit):
        parse_args(["--doctor", "hola"])
    with pytest.raises(SystemExit):
        parse_args(["--doctor", "--allow-commands"])


def test_selftest_refuses_prompts() -> None:
    with pytest.raises(SystemExit):
        parse_args(["--selftest", "hola"])
    assert parse_args(["--selftest"]).selftest is True


def test_build_context_maps_flags_without_touching_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("AUTONOMA_HOME", raising=False)
    monkeypatch.delenv("NO_COLOR", raising=False)
    before = dict(os.environ)
    context = build_context(parse_args(["--data-dir", str(tmp_path), "--plain", "--allow-commands"]))
    assert context.root == tmp_path
    assert context.use_rich is False
    assert context.allow_commands is True
    assert dict(os.environ) == before


def test_main_routes_doctor_and_selftest_to_diagnostics(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[str] = []
    monkeypatch.setattr("autonoma.cli.run_diagnostics", lambda **kwargs: called.append("doctor") or 0)
    monkeypatch.setattr("autonoma.cli.run_selftest", lambda **kwargs: called.append("selftest") or 0)
    with pytest.raises(SystemExit) as first:
        main(["--doctor", "--json"])
    assert first.value.code == 0
    with pytest.raises(SystemExit):
        main(["--selftest"])
    assert called == ["doctor", "selftest"]


def test_main_returns_typed_exit_code_on_startup_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(**kwargs: object) -> int:
        raise ConfigurationError("config.json inválido")

    monkeypatch.setattr("autonoma.cli.repl", boom)
    with pytest.raises(SystemExit) as excinfo:
        main(["hola"])
    assert excinfo.value.code == int(ExitCode.CONFIGURATION)


def test_version_is_the_single_source_of_truth(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--version"])
    assert __version__ in capsys.readouterr().out
