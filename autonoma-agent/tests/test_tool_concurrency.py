"""El bucle del modelo en paralelo: sólo lo que no escribe, en el orden del proveedor.

Se prueba el mecanismo completo (concurrencia real, orden del payload, propagación del
pánico y del fallo de contrato) porque un lote mal ensamblado no se nota en una demo: se
nota cuando el proveedor rechaza el historial por `tool_call_id` desparejado.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from autonoma.agent import Agent
from autonoma.config import Settings
from autonoma.filesystem import FileSystemManager
from autonoma.key_handler import PanicController, PanicError
from autonoma.knowledge import KnowledgeStore


@dataclass(frozen=True, slots=True)
class Bundle:
    """Lo mínimo que `format_bundle` necesita ver: el resto del informe no interesa aquí."""

    query: str
    backend: str
    hit_count: int
    text: str


class Clock:
    """Contador de concurrencia observada dentro de las herramientas."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.now = 0
        self.peak = 0
        self.calls: list[str] = []

    def enter(self, name: str, delay: float = 0.05) -> str:
        with self._lock:
            self.now += 1
            self.peak = max(self.peak, self.now)
            self.calls.append(name)
        time.sleep(delay)
        with self._lock:
            self.now -= 1
        return f"resultado::{name}"


class SlowSearch:
    def __init__(self, clock: Clock, store: KnowledgeStore) -> None:
        self.clock = clock
        self.store = store
        self.knowledge_dir = Path(store.knowledge_dir)

    def search(self, *args: object, **kwargs: object) -> list[object]:
        self.clock.enter("web_search", delay=0.0)
        return []

    def research(self, query: str, fetch_pages: int | None = None, *, save: bool = True) -> Bundle:
        text = self.clock.enter("web_search")
        return Bundle(query=str(query), backend="fake", hit_count=1, text=text)

    def format_bundle(self, bundle: Bundle, preview: int = 900) -> str:
        return bundle.text

    def fetch_url(self, *args: object, **kwargs: object) -> str:
        return self.clock.enter("fetch_url")

    def save_note(self, *args: object, **kwargs: object) -> str:
        return self.clock.enter("save_knowledge")

    def read_note(self, *args: object, **kwargs: object) -> str:
        return self.clock.enter("read_knowledge")

    def search_notes(self, *args: object, **kwargs: object) -> str:
        return self.clock.enter("read_knowledge")

    def list_notes(self, *args: object, **kwargs: object) -> list[Path]:
        return []

    def context_digest(self, *args: object, **kwargs: object) -> str:
        return ""


class ScriptedClient:
    """Cliente de juguete: primero devuelve el lote de herramientas y luego la respuesta."""

    def __init__(self, calls: list[dict[str, object]]) -> None:
        self._script = [
            {"choices": [{"message": {"content": None, "tool_calls": calls}}]},
            {"choices": [{"message": {"content": "listo"}}]},
        ]
        self.configured = True
        self.payloads: list[list[dict[str, object]]] = []

    def chat(self, payload, **kwargs: object) -> dict[str, object]:
        self.payloads.append(list(payload))
        return self._script.pop(0)

    def extract_message(self, completion: dict[str, object]) -> dict[str, object]:
        return dict(completion["choices"][0]["message"])  # type: ignore[index]


def _tool_call(name: str, call_id: str, arguments: str = '{"query":"x"}') -> dict[str, object]:
    return {"type": "function", "id": call_id, "function": {"name": name, "arguments": arguments}}


def _agent(
    tmp_path: Path, calls: list[dict[str, object]], *, approve=None, max_parallel: int = 4
) -> tuple[Agent, Clock, ScriptedClient]:
    panic = PanicController()
    clock = Clock()
    store = KnowledgeStore(panic, tmp_path / "kb")
    client = ScriptedClient(calls)
    agent = Agent(
        Settings(notrack_api_key="sk-prueba-123456", max_parallel_tool_calls=max_parallel),
        panic,
        client,  # type: ignore[arg-type]
        SlowSearch(clock, store),  # type: ignore[arg-type]
        FileSystemManager(panic),
        notes=store,
        approve=approve,
    )
    return agent, clock, client


def test_read_only_calls_run_concurrently_and_keep_their_order(tmp_path: Path) -> None:
    calls = [
        _tool_call("web_search", "c1"),
        _tool_call("fetch_url", "c2", '{"url":"https://x.example"}'),
        _tool_call("web_search", "c3"),
    ]
    agent, clock, client = _agent(tmp_path, calls)
    assert agent.run("investiga") == "listo"
    assert clock.peak > 1, "sin concurrencia el pico sería 1"
    assert len(clock.calls) == 3
    # El segundo payload enviado al proveedor es el que lleva las respuestas de las herramientas.
    tool_messages = [message for message in client.payloads[-1] if message["role"] == "tool"]
    assert [message["tool_call_id"] for message in tool_messages] == ["c1", "c2", "c3"]
    assert [message["content"] for message in tool_messages] == [
        "resultado::web_search",
        "resultado::fetch_url",
        "resultado::web_search",
    ]


def test_a_local_tool_in_the_batch_forces_serial_execution(tmp_path: Path) -> None:
    """Una sola herramienta que escribe (o lee rutas) convierte el lote en serie."""
    target = tmp_path / "aparece.txt"
    calls = [_tool_call("web_search", "c1"), _tool_call("write_file", "c2", f'{{"path":"{target}","content":"v"}}')]
    asked: list[str] = []

    def approve(name: str, args: object, prompt: str = "") -> bool:
        asked.append(name)
        return True

    agent, clock, _client = _agent(tmp_path, calls, approve=approve)
    assert agent.run("guarda") == "listo"
    assert clock.peak == 1  # nada se solapa: el lote lleva una herramienta local
    assert asked == ["write_file"]
    assert target.read_text(encoding="utf-8") == "v"


def test_parallelism_is_bounded_by_settings(tmp_path: Path) -> None:
    calls = [_tool_call("web_search", f"c{index}") for index in range(6)]
    agent, clock, _client = _agent(tmp_path, calls, max_parallel=2)
    assert agent.run("investiga") == "listo"
    assert clock.peak <= 2
    assert len(clock.calls) == 6


def test_events_are_emitted_from_the_main_thread_in_order(tmp_path: Path) -> None:
    calls = [_tool_call("web_search", "c1"), _tool_call("web_search", "c2")]
    agent, _clock, _client = _agent(tmp_path, calls)
    events: list[tuple[str, str]] = []
    assert agent.run("investiga", lambda kind, text: events.append((kind, text))) == "listo"
    tool_lines = [text for kind, text in events if kind == "tool"]
    assert any("en paralelo" in line for line in tool_lines)
    results = [text for kind, text in events if kind == "tool_result"]
    assert results == ["resultado::web_search", "resultado::web_search"]


def test_unknown_tool_in_a_batch_is_reported_not_raised(tmp_path: Path) -> None:
    calls = [_tool_call("no_existe", "c1"), _tool_call("web_search", "c2")]
    agent, clock, client = _agent(tmp_path, calls)
    assert agent.run("prueba") == "listo"
    assert clock.peak == 1  # un nombre desconocido manda el lote a la vía serie
    payloads = client.payloads[-1]
    broken = [message for message in payloads if message["role"] == "tool"]
    assert any("no_existe" in str(message["content"]) or "ERROR" in str(message["content"]) for message in broken)


def test_panic_during_a_batch_cancels_the_turn_from_the_worker(tmp_path: Path) -> None:
    """Un `PanicError` dentro del hilo de trabajo no se convierte en un `ToolOutcome` benigno."""
    panic = PanicController()
    store = KnowledgeStore(panic, tmp_path / "kb")
    search = SlowSearch(Clock(), store)
    hits: list[int] = []

    def armed_research(query: str, fetch_pages: int | None = None, *, save: bool = True) -> Bundle:
        hits.append(1)
        if len(hits) == 1:  # el primer worker cancela la tarea
            panic.panic()
            return Bundle(query=str(query), backend="fake", hit_count=1, text="primera")
        panic.check()  # el segundo lo nota al entrar: sale disparado, no se vuelve texto benigno
        return Bundle(query=str(query), backend="fake", hit_count=1, text="segunda")

    search.research = armed_research  # type: ignore[method-assign]
    agent = Agent(
        Settings(notrack_api_key="sk-prueba-123456", max_parallel_tool_calls=1),
        panic,
        ScriptedClient([_tool_call("web_search", "c1"), _tool_call("web_search", "c2")]),  # type: ignore[arg-type]
        search,  # type: ignore[arg-type]
        FileSystemManager(panic),
        notes=store,
    )
    with pytest.raises(PanicError):
        agent.run("investiga")
    assert not panic.busy


def test_serial_path_still_works_with_one_single_call(tmp_path: Path) -> None:
    agent, clock, _client = _agent(tmp_path, [_tool_call("web_search", "solo")])
    assert agent.run("una sola") == "listo"
    assert clock.calls == ["web_search"]
