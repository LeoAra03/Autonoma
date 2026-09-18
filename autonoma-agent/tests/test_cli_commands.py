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
    _ProgressReporter,
    build_context,
    build_parser,
    main,
    parse_args,
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
