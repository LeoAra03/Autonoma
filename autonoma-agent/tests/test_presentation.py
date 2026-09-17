from types import SimpleNamespace
import pytest
from autonoma.cli import Session, _PlainConsole, parse_args
from autonoma.key_handler import PanicController
from autonoma.presentation import safe_text, operation_preview


def test_controls_are_visible_not_executed():
    assert safe_text('hello\x1b[31m\r\u202esecret\x07') == 'hello\\u001b[31m\\u000d\\u202esecret\\u0007'
    assert safe_text('áé\n\t中文') == 'áé\n\t中文'


def test_diff_preview(tmp_path):
    path = tmp_path/'note'
    path.write_text('old\n', encoding='utf-8')
    preview = operation_preview('write_file', {'path': str(path), 'content': 'new\n'})
    assert '-old' in preview and '+new' in preview
    assert path.read_text() == 'old\n'
    assert 'parcial' in operation_preview('write_file', {'path': str(path), 'content': '\n'.join(str(i) for i in range(200))})
    assert 'PRIVACIDAD' in operation_preview('read_file', {})
    assert 'DESTRUCTIVO' in operation_preview('delete_path', {})
    assert 'ALTO RIESGO' in operation_preview('run_command', {})


@pytest.mark.parametrize('reply,expected', [('SI', True), ('si', False), ('', False), ('NO', False)])
def test_confirmation_explicit(monkeypatch, reply, expected):
    monkeypatch.setattr('sys.stdin.isatty', lambda: True)
    session = Session.__new__(Session)
    session.panic = PanicController()
    session.panic.mark_busy()
    seen = []
    session.console = SimpleNamespace(print=lambda *a, **k: seen.append(a), input=lambda _: reply)
    assert session.approve('read_file', {'path': 'x'}) is expected
    assert session.panic.busy
    assert any('PRIVACIDAD' in str(x) for x in seen)


def test_noninteractive_denial(monkeypatch):
    monkeypatch.setattr('sys.stdin.isatty', lambda: False)
    session = Session.__new__(Session)
    assert session.approve('run_command', {}) is False


def test_cli_success_and_error_codes(monkeypatch):
    monkeypatch.setattr('autonoma.cli.RICH', False)
    session = Session.__new__(Session)
    session.panic = PanicController()
    session.console = _PlainConsole()
    session.agent = SimpleNamespace(notrack=SimpleNamespace(configured=True), run=lambda *a, **k: 'hola')
    assert session.run_prompt('hola') == 0
    def fail(*a, **k):
        raise KeyboardInterrupt()
    session.agent.run = fail
    assert session.run_prompt('hola') != 0
    assert session.panic.is_set
    assert not parse_args([]).allow_commands
    assert parse_args(['--allow-commands']).allow_commands
