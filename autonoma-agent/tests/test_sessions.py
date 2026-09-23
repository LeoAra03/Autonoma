"""Sesiones persistentes: un turno por línea, permisos privados y recuperación honesta.

La memoria en disco es la única que sobrevive al proceso, así que se prueba el contrato
completo: dónde se escribe, con qué modo, qué se ignora cuando está corrupto y qué se
rehúsa en seco.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

import pytest

from autonoma.agent import Agent
from autonoma.config import Settings
from autonoma.errors import FileSystemError
from autonoma.key_handler import PanicController
from autonoma.sessions import SessionInfo, SessionStore, session_id_for


@pytest.fixture
def store(tmp_path: Path) -> SessionStore:
    return SessionStore(tmp_path / "sessions")


# ─────────────────────────────────────────────────────────────── identificación
def test_session_ids_sort_chronologically() -> None:
    earlier, later = session_id_for(time.time() - 3_600), session_id_for(time.time())
    assert earlier < later
    assert len(earlier.split("-")) == 5  # año-mes-día-hora-proceso: legible y único en la máquina


def test_path_for_cannot_escape_the_directory(store: SessionStore) -> None:
    """La sanitización mata los separadores: una ruta traversal termina dentro de `sessions/`."""
    for hostile in ("../../etc/passwd", "/etc/shadow", r"..\..\windows\system32", "a/b/../c"):
        path = store.path_for(hostile)
        assert path.parent == store.directory
        assert os.sep not in path.name and "/" not in path.name and "\\" not in path.name
        assert path.resolve().is_relative_to(store.directory.resolve())


def test_empty_identifier_is_refused(store: SessionStore) -> None:
    with pytest.raises(FileSystemError, match="vacío"):
        store.path_for("   ")


# ─────────────────────────────────────────────────────────────── escritura
def test_append_writes_one_json_line_per_message(store: SessionStore) -> None:
    store.session_id = store.start()
    store.append("user", "pregunta")
    store.append("assistant", "respuesta")
    lines = store.path_for(store.session_id).read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["role"] for line in lines] == ["user", "assistant"]
    assert all(json.loads(line)["at"] > 0 for line in lines)


def test_blank_content_is_not_stored(store: SessionStore) -> None:
    store.session_id = store.start()
    store.append("user", "   ")
    # Ni archivo: una sesión sin turnos no debe ensuciar `sessions/`.
    assert not store.path_for(store.session_id).exists()
    with pytest.raises(FileSystemError, match="No hay sesión guardada"):
        store.turns()


def test_files_are_private_to_the_user(store: SessionStore) -> None:
    if os.name == "nt":  # pragma: no cover - la permisos de NTFS no son POSIX
        pytest.skip("el modo POSIX no aplica en Windows")
    store.session_id = store.start()
    store.append("user", "datos sensibles")
    path = store.path_for(store.session_id)
    assert path.stat().st_mode & 0o077 == 0
    assert store.directory.stat().st_mode & 0o077 == 0


def test_disabled_store_writes_nothing(tmp_path: Path) -> None:
    quiet = SessionStore(tmp_path / "sessions", enabled=False)
    quiet.start()
    quiet.append("user", "no se guarda")
    quiet.append("assistant", "tampoco esto")
    assert not (tmp_path / "sessions").exists()
    assert quiet.history() == []


def test_max_turns_bounds_the_file_in_disk(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "s", max_turns=4)
    store.session_id = store.start()
    for index in range(10):
        store.append("user", f"turno {index}")
    assert len(store.turns()) == 4


# ─────────────────────────────────────────────────────────────── lectura
def test_a_half_written_last_line_is_dropped_not_fatal(store: SessionStore) -> None:
    store.session_id = store.start()
    store.append("user", "primera")
    with store.path_for(store.session_id).open("a", encoding="utf-8") as handle:
        handle.write('{"role": "assistant", "conte')  # corte a medias: el proceso murió aquí
    turns = store.turns()
    assert [turn.content for turn in turns] == ["primera"]


def test_corruption_in_the_middle_is_reported(store: SessionStore) -> None:
    store.session_id = store.start()
    store.append("user", "primera")
    store.append("assistant", "segunda")
    path = store.path_for(store.session_id)
    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text("\n".join([lines[0], "basura", *lines[1:]]) + "\n", encoding="utf-8")
    with pytest.raises(FileSystemError, match="Línea 2"):
        store.turns()


def test_unknown_session_raises_instead_of_returning_empty(store: SessionStore) -> None:
    with pytest.raises(FileSystemError, match="No hay sesión guardada"):
        store.turns("2020-01-01-000000-ffff")


def test_turns_of_no_session_selected_is_empty(store: SessionStore) -> None:
    assert store.turns() == ()


# ─────────────────────────────────────────────────────────────── índice y borrado
def test_list_is_newest_first_with_summaries(store: SessionStore) -> None:
    for offset in range(3):
        session_id = store.start(session_id=f"s{offset}")
        store.append("user", f"pregunta número {offset}")
        _touch(store.path_for(session_id), 1_000_000 + offset)
    infos = store.list()
    assert [info.session_id for info in infos] == ["s2", "s1", "s0"]
    assert isinstance(infos[0], SessionInfo)
    assert infos[0].turns == 1
    assert "pregunta número 2" in infos[0].preview
    assert "s2" in infos[0].summary()


def test_limit_bounds_the_listing(store: SessionStore) -> None:
    for offset in range(5):
        store.start(session_id=f"x{offset}")
        store.append("user", "cuerpo")
    assert len(store.list(limit=2)) == 2


def test_latest_id_skips_the_current_session(store: SessionStore) -> None:
    store.start(session_id="vieja")
    store.append("user", "a")
    _touch(store.path_for("vieja"), 1_000_000)
    store.start(session_id="nueva")
    store.append("user", "b")
    _touch(store.path_for("nueva"), 2_000_000)
    assert store.latest_id() == "nueva"
    assert store.latest_id(exclude_current=True) == "vieja"


def test_latest_id_without_sessions_is_none(tmp_path: Path) -> None:
    assert SessionStore(tmp_path / "vacío").latest_id() is None


def test_delete_clears_the_current_session_and_reports_the_id(store: SessionStore) -> None:
    store.session_id = store.start()
    store.append("user", "adiós")
    path = store.path_for(store.session_id)
    deleted = store.session_id
    assert store.delete(deleted) == deleted
    assert not path.exists()
    # Borrar la sesión activa deja el store sin destino: el siguiente turno no puede
    # escribir en un archivo que ya no existe.
    assert store.session_id is None
    assert store.delete("nunca-existió") is None


def _touch(path: Path, stamp: float) -> None:
    """Fuerza la antigüedad: algunos sistemas de archivos sólo tienen resolución de un segundo."""
    os.utime(path, (stamp, stamp))


# ─────────────────────────────────────────────────────────────── con el agente
def test_history_round_trips_through_disk(store: SessionStore) -> None:
    store.session_id = store.start()
    store.append("user", "primera pregunta")
    store.append("assistant", "primera respuesta")
    store.append("user", "segunda pregunta")
    store.append("assistant", "segunda respuesta")
    agent = _bare_agent()
    assert agent.load_history(store.history(store.session_id)) == 2
    window = [message["content"] for message in agent.history]
    assert window == ["primera pregunta", "primera respuesta", "segunda pregunta", "segunda respuesta"]


def test_an_unpaired_turn_is_not_invented(store: SessionStore) -> None:
    """La última pregunta sin respuesta no puede fabricar un turno del modelo."""
    store.session_id = store.start()
    store.append("user", "sin responder")
    agent = _bare_agent()
    assert agent.load_history(store.history(store.session_id)) == 0


def _bare_agent() -> Agent:
    """Agente real con un cliente falso: la ventana de historia es lo que se prueba aquí."""
    from autonoma.filesystem import FileSystemManager
    from autonoma.search_engine import SearchEngine

    panic = PanicController()
    engine = SearchEngine(panic, Path(tempfile.mkdtemp()))
    return Agent(Settings(notrack_api_key="sk-prueba-123"), panic, object(), engine, FileSystemManager(panic))
