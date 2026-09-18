from pathlib import Path
from types import SimpleNamespace

import pytest

from autonoma.cli import PlainConsole, Session, parse_args
from autonoma.errors import ExitCode, ProviderUnavailableError
from autonoma.key_handler import PanicController
from autonoma.observability import MetricsRegistry
from autonoma.presentation import operation_preview, safe_text
from autonoma.runtime import DataOrigin, DataRoot, RenderPreferences, RuntimeContext


def test_controls_are_visible_not_executed():
    assert safe_text("hello\x1b[31m\r\u202esecret\x07") == "hello\\u001b[31m\\u000d\\u202esecret\\u0007"
    assert safe_text("áé\n\t中文") == "áé\n\t中文"


def test_diff_preview(tmp_path):
    path = tmp_path/"note"
    path.write_text("old\n", encoding="utf-8")
    preview = operation_preview("write_file", {"path": str(path), "content": "new\n"})
    assert "-old" in preview and "+new" in preview
    assert path.read_text() == "old\n"
    assert "parcial" in operation_preview("write_file", {"path": str(path), "content": "\n".join(str(i) for i in range(200))})
    assert "PRIVACIDAD" in operation_preview("read_file", {})
    assert "DESTRUCTIVO" in operation_preview("delete_path", {})
    assert "ALTO RIESGO" in operation_preview("run_command", {})


@pytest.mark.parametrize("reply,expected", [("SI", True), ("si", False), ("", False), ("NO", False)])
def test_confirmation_explicit(monkeypatch, reply, expected):
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    session = Session.__new__(Session)
    session.panic = PanicController()
    session.panic.mark_busy()
    seen = []
    session.console = SimpleNamespace(print=lambda *a, **k: seen.append(a), input=lambda _: reply)
    assert session.approve("read_file", {"path": "x"}) is expected
    assert session.panic.busy
    assert any("PRIVACIDAD" in str(x) for x in seen)


def test_noninteractive_denial(monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    session = Session.__new__(Session)
    assert session.approve("run_command", {}) is False


def _bare_session():
    session = Session.__new__(Session)
    session.panic = PanicController()
    session.console = PlainConsole()
    session.context = RuntimeContext(
        data_root=DataRoot(Path.cwd(), DataOrigin.CHECKOUT),
        render=RenderPreferences(plain=True),
    )
    session.metrics = MetricsRegistry()
    return session


def test_cli_success_and_error_codes():
    session = _bare_session()
    session.agent = SimpleNamespace(notrack=SimpleNamespace(configured=True), run=lambda *a, **k: "hola")
    assert session.run_prompt("hola") == int(ExitCode.SUCCESS)

    def fail(*a, **k):
        raise KeyboardInterrupt

    session.agent.run = fail
    assert session.run_prompt("hola") == int(ExitCode.CANCELLED)
    assert session.panic.is_set


def test_cli_maps_typed_errors_to_exit_codes():
    session = _bare_session()
    session.agent = SimpleNamespace(notrack=SimpleNamespace(configured=True))
    def provider_fail(*a, **k):
        raise ProviderUnavailableError("No se pudo conectar con NoTrack")
    session.agent.run = provider_fail
    assert session.run_prompt("hola") == int(ExitCode.PROVIDER)
    session.agent.run = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("secreto-privado"))
    assert session.run_prompt("hola") == int(ExitCode.INTERNAL)
    assert not parse_args([]).allow_commands
    assert parse_args(["--allow-commands"]).allow_commands
