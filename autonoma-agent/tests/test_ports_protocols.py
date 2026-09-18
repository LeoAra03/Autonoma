"""Los puertos estructurales: la UI y el orquestador se acoplan sólo por contratos."""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from autonoma.agent import Agent, build_agent
from autonoma.cli import PlainConsole, RichConsoleFactory, build_console
from autonoma.config import Settings
from autonoma.filesystem import FileSystemManager
from autonoma.key_handler import PanicController
from autonoma.knowledge import KnowledgeStore
from autonoma.ports import ConsolePort, FileSystemPort, NotesPort, ResourcePort, SearchPort
from autonoma.runtime import DataOrigin, DataRoot, RenderPreferences, RuntimeContext
from autonoma.search_engine import SearchEngine


def protocol_members(protocol: type[object]) -> list[str]:
    return sorted(
        name
        for name, member in vars(protocol).items()
        if not name.startswith("__") and callable(member)
    )


def test_search_engine_satisfies_search_port(tmp_path: Path) -> None:
    engine = SearchEngine(PanicController(), knowledge_dir=tmp_path)
    assert isinstance(engine, SearchPort)
    assert protocol_members(SearchPort) == ["fetch_url", "format_bundle", "research", "shutdown"]


def test_knowledge_store_satisfies_notes_port(tmp_path: Path) -> None:
    assert isinstance(KnowledgeStore(PanicController(), tmp_path), NotesPort)


def test_filesystem_manager_satisfies_filesystem_port() -> None:
    assert isinstance(FileSystemManager(PanicController()), FileSystemPort)


def test_plain_console_satisfies_console_port() -> None:
    assert isinstance(PlainConsole(), ConsolePort)


def test_rich_factory_output_satisfies_console_port() -> None:
    pytest.importorskip("rich")
    console = RichConsoleFactory(RenderPreferences()).build()
    assert isinstance(console, ConsolePort)


def test_every_closeable_resource_satisfies_resource_port(tmp_path: Path) -> None:
    """Todo recurso de la sesión puede cerrarse desde la limpieza de pánico."""
    agent = Agent(Settings(), PanicController(), None, None, None)
    engine = SearchEngine(PanicController(), knowledge_dir=tmp_path)
    for resource in (agent, engine):
        assert isinstance(resource, ResourcePort)
    agent.close()  # idempotente y sin recursos: no debe lanzar
    agent.close()

    assembled = build_agent(Settings(notrack_api_key="sk-prueba-123456", root_dir=tmp_path), PanicController())
    for resource in (assembled.notrack, assembled.search, assembled.fs):
        assert isinstance(resource, ResourcePort)
    assembled.close()
    assembled.close()  # cerrar dos veces no reintenta cierres ya hechos


def test_port_methods_exist_with_compatible_arity(tmp_path: Path) -> None:
    """`isinstance` con Protocol no valida firmas: aquí se comprueban nombre y parámetros."""
    pairs = [
        (SearchPort, SearchEngine(PanicController(), knowledge_dir=tmp_path)),
        (FileSystemPort, FileSystemManager(PanicController())),
        (NotesPort, KnowledgeStore(PanicController(), tmp_path)),
    ]
    for protocol, implementation in pairs:
        for name in protocol_members(protocol):
            # El protocolo entrega la función sin enlazar (con `self`); la instancia, enlazada.
            expected = _required_parameters(inspect.signature(getattr(protocol, name)), skip_self=True)
            actual = _required_parameters(inspect.signature(getattr(implementation, name)), skip_self=False)
            assert expected == actual, f"{protocol.__name__}.{name}"


def _required_parameters(signature: inspect.Signature, *, skip_self: bool) -> tuple[str, ...]:
    names = list(signature.parameters)
    if skip_self:
        names = names[1:]
    return tuple(
        name
        for name in names
        if signature.parameters[name].default is inspect.Parameter.empty
        and signature.parameters[name].kind is not inspect.Parameter.VAR_KEYWORD
    )


def test_approver_contract_is_satisfied_by_the_session_callback() -> None:
    """`Agent.approve` recibe (herramienta, argumentos, prompt del usuario) y devuelve bool."""

    def approver(name: str, args: object, user_prompt: str = "") -> bool:
        return bool(name) and isinstance(args, dict) and user_prompt == "sí"

    agent = Agent(Settings(), PanicController(), None, None, None, approve=approver)
    outcome = agent.run_tool("list_dir", {"path": str(Path.cwd())}, user_prompt="sí")
    assert outcome.ok or "denegada" not in outcome.output


def test_console_selection_depends_only_on_the_render_context() -> None:
    plain = RuntimeContext(data_root=DataRoot(Path("/x"), DataOrigin.FLAG), render=RenderPreferences(plain=True))
    assert isinstance(build_console(plain), PlainConsole)
