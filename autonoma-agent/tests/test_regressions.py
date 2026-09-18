import json
import os
import sys
from types import SimpleNamespace

import httpx
import pytest

from autonoma.agent import Agent
from autonoma.cli import Session, parse_args
from autonoma.config import Settings, load_settings, save_api_keys
from autonoma.errors import (
    ConfigurationError,
    ErrorCode,
    ExitCode,
    FileSystemError,
    NetworkPolicyError,
    ProcessTimeoutError,
    SearchBackendError,
)
from autonoma.filesystem import FileSystemManager
from autonoma.key_handler import PanicController
from autonoma.network import validate_public_url
from autonoma.search_engine import SearchEngine


def test_large_process_output():
    fs = FileSystemManager(PanicController())
    command = [sys.executable, "-c", 'import sys; print("x"*200000); sys.stderr.write("y"*200000)']
    result = fs.run_command(command, shell=False, timeout=10)
    assert result.returncode == 0
    assert len(result.stdout) == 12000
    assert len(result.stderr) == 8000
    assert result.truncated
    assert result.succeeded
    assert fs.active_processes == 0


def test_process_timeout():
    fs = FileSystemManager(PanicController())
    with pytest.raises(ProcessTimeoutError, match="Timeout"):
        fs.run_command([sys.executable, "-c", "import time; time.sleep(10)"], shell=False, timeout=.1)
    assert fs.active_processes == 0


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf"), 301, "x"])
def test_invalid_timeout(value):
    with pytest.raises(ConfigurationError, match="timeout"):
        FileSystemManager(PanicController()).run_command("echo test", timeout=value)


def test_force_cannot_override(tmp_path):
    fs = FileSystemManager(PanicController(), extra_protected=[str(tmp_path)])
    with pytest.raises(FileSystemError, match="protegida"):
        fs.write_file(str(tmp_path/"x"), "no", force=True)


@pytest.mark.parametrize("tool,args", [
    ("run_command", {"command": "echo x"}),
    ("read_file", {"path": "x"}),
    ("write_file", {"path": "x", "content": "y"}),
    ("delete_path", {"path": "x"}),
    ("copy_path", {"src": "a", "dst": "b"}),
    ("move_path", {"src": "a", "dst": "b"}),
    ("mkdir", {"path": "x"}),
    ("list_dir", {"path": "x"}),
])
def test_local_tools_deny_without_approval(tool, args):
    """Sin callback de aprobación ninguna herramienta local se ejecuta, con argumentos válidos."""
    agent = Agent(Settings(), PanicController(), None, None, None)
    outcome = agent.run_tool(tool, args, user_prompt="yes")
    assert not outcome.ok
    assert "denegada" in outcome.output
    assert outcome.error_code is ErrorCode.APPROVAL_REQUIRED
    assert outcome.requires_approval


def test_approved_dispatch(tmp_path):
    panic = PanicController()
    agent = Agent(Settings(), panic, None, None, FileSystemManager(panic), approve=lambda *_: True)
    target = tmp_path/"hello"
    agent._dispatch("write_file", {"path": str(target), "content": "ok"}, user_prompt="")
    assert target.read_text() == "ok"


def test_knowledge_confined(tmp_path):
    engine = SearchEngine(PanicController(), tmp_path/"kb")
    outside = tmp_path/"secret.txt"
    outside.write_text("secret")
    with pytest.raises(ValueError):
        engine.read_note(str(outside))
    with pytest.raises(ValueError):
        engine.read_note("../secret.txt")
    (tmp_path/"kb"/"link.md").symlink_to(outside)
    assert engine.list_notes() == []
    with pytest.raises(ValueError):
        engine.read_note("link.md")


def test_notes_do_not_overwrite(tmp_path):
    engine = SearchEngine(PanicController(), tmp_path)
    assert engine.save_note("same", "one") != engine.save_note("same", "two")


@pytest.mark.parametrize("url", ["file:///etc/passwd", "http://127.0.0.1", "http://[::1]", "http://169.254.169.254", "https://user:pass@example.com", "https://example.com:22"])
def test_unsafe_urls(url):
    with pytest.raises(ValueError):
        validate_public_url(url)


def test_fetch_http_error(tmp_path, monkeypatch):
    import autonoma.search_engine as module
    monkeypatch.setattr(module, "validate_public_url", lambda _: None)
    engine = SearchEngine(PanicController(), tmp_path)
    engine._http = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(404)))
    with pytest.raises(SearchBackendError, match="HTTP 404") as excinfo:
        engine.fetch_url("https://example.com")
    assert excinfo.value.context["host"] == "example.com"
    engine.close()
    panic = PanicController()
    engine2 = SearchEngine(panic, tmp_path)
    def refuse(request):
        raise httpx.ConnectError("TLS/SSL se cerró al enviar la cabecera Authorization", request=request)
    engine2._http = httpx.Client(transport=httpx.MockTransport(refuse))
    with pytest.raises(SearchBackendError) as transport:
        engine2.fetch_url("https://example.com/x")
    # El detalle crudo del transporte nunca sube al modelo: sólo el tipo, en el log.
    assert "Authorization" not in transport.value.user_message()
    assert isinstance(transport.value.__cause__, httpx.ConnectError)


def test_fetch_size_limit(tmp_path, monkeypatch):
    import autonoma.search_engine as module
    monkeypatch.setattr(module, "validate_public_url", lambda _: None)
    engine = SearchEngine(PanicController(), tmp_path)
    engine._http = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, headers={"content-type": "text/plain"}, content=b"x"*2_000_001)))
    with pytest.raises(ValueError, match="grande"):
        engine.fetch_url("https://example.com")
    engine.close()


def test_config_and_secret_persistence(tmp_path, monkeypatch):
    monkeypatch.delenv("NOTRACK_API_KEY", raising=False)
    env = tmp_path/".env"
    env.write_text("CUSTOM=keep\n")
    saved = save_api_keys("secret", env_path=env)
    assert saved == {"NOTRACK_API_KEY": "secret"}
    assert "CUSTOM=keep" in env.read_text()
    if os.name != "nt":
        assert env.stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError):
        save_api_keys("bad\nINJECT=yes", env_path=env)
    # Persistir no muta el entorno del proceso: el canal global quedó eliminado.
    assert "NOTRACK_API_KEY" not in os.environ
    assert load_settings(env, tmp_path/"absent.json", ensure_dirs=False).notrack_api_key == "secret"
    cfg = tmp_path/"config.json"
    cfg.write_text(json.dumps({"http_timeout": 5, "fetch_pages": 0, "knowledge_dir": str(tmp_path/"kb"), "log_dir": str(tmp_path/"logs")}))
    settings = load_settings(env, cfg)
    assert settings.http_timeout == 5 and settings.fetch_pages == 0
    monkeypatch.setenv("MAX_TOOL_ITERATIONS", "-1")
    with pytest.raises(ValueError, match="max_tool_iterations"):
        load_settings(env, cfg)


def test_preferences():
    args = parse_args(["--plain", "--quiet", "--reduced-motion", "hola"])
    assert args.plain and args.quiet and args.reduced_motion


def test_missing_key_exit_code():
    session = Session.__new__(Session)
    session.agent = SimpleNamespace(notrack=SimpleNamespace(configured=False))
    session.console = SimpleNamespace(print=lambda *a, **k: None)
    assert session.run_prompt("hello") == int(ExitCode.CONFIGURATION)


def test_frozen_config_location(monkeypatch, tmp_path):
    from autonoma.config import project_root
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path/"Autonoma.exe"))
    assert project_root() == tmp_path


def test_bad_config_is_actionable(tmp_path):
    cfg = tmp_path/"config.json"
    cfg.write_text("{bad")
    with pytest.raises(ValueError, match=r"config\.json"):
        load_settings(tmp_path/".env", cfg)


def test_history_is_bounded(tmp_path):
    panic = PanicController()
    client = SimpleNamespace(chat=lambda *a, **k: {}, extract_message=lambda _: {"content": "ok"})
    engine = SearchEngine(panic, tmp_path)
    agent = Agent(Settings(), panic, client, engine, None)
    for _ in range(20):
        assert agent.run("hello") == "ok"
    assert len(agent.history) == 16
    agent.reset_history()
    assert agent.history == []


def test_redirect_not_followed(tmp_path, monkeypatch):
    import autonoma.search_engine as module
    monkeypatch.setattr(module, "validate_public_url", lambda _: None)
    engine = SearchEngine(PanicController(), tmp_path)
    requests = []
    def handle(request):
        requests.append(request)
        return httpx.Response(302, headers={"location": "http://127.0.0.1"})
    engine._http = httpx.Client(transport=httpx.MockTransport(handle), follow_redirects=True)
    with pytest.raises(NetworkPolicyError, match="no se siguen redirecciones") as excinfo:
        engine.fetch_url("https://example.com")
    assert len(requests) == 1  # ni un solo intento de alcanzar 127.0.0.1
    assert excinfo.value.context["redirect_host"] == "127.0.0.1"
    engine.close()
