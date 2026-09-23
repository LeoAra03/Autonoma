"""Contexto de ejecución: raíz de datos, preferencias de render y limpieza de pánico."""

from __future__ import annotations

import dataclasses
import os
import time
from pathlib import Path

import pytest

from autonoma.key_handler import PanicController
from autonoma.runtime import (
    DataOrigin,
    DataRoot,
    RenderPreferences,
    RuntimeContext,
    resolve_data_root,
)


def test_explicit_flag_wins_over_environment(tmp_path: Path) -> None:
    root = resolve_data_root({"AUTONOMA_HOME": str(tmp_path / "env")}, override=str(tmp_path / "flag"))
    assert root.origin is DataOrigin.FLAG
    assert root.path == (tmp_path / "flag").resolve()


def test_environment_home_wins_over_checkout(tmp_path: Path) -> None:
    root = resolve_data_root({"AUTONOMA_HOME": str(tmp_path)})
    assert root.origin is DataOrigin.ENV
    assert root.path == tmp_path


def test_blank_home_is_ignored_not_treated_as_root(tmp_path: Path) -> None:
    (tmp_path / "run_autonoma.py").write_text("", encoding="utf-8")
    root = resolve_data_root({"AUTONOMA_HOME": "   "}, source_root=tmp_path)
    assert root.origin is DataOrigin.CHECKOUT


def test_checkout_is_preferred_when_launcher_exists(tmp_path: Path) -> None:
    (tmp_path / "run_autonoma.py").write_text("", encoding="utf-8")
    root = resolve_data_root({}, source_root=tmp_path)
    assert root.origin is DataOrigin.CHECKOUT
    assert root.path == tmp_path


def test_packaged_install_falls_back_to_user_config(tmp_path: Path) -> None:
    """Sin XDG_CONFIG_HOME/APPDATA se usa `~/.config/autonoma` (o `%APPDATA%` en Windows)."""
    root = resolve_data_root({"XDG_CONFIG_HOME": str(tmp_path)}, source_root=tmp_path / "src")
    assert root.origin is DataOrigin.USER_CONFIG
    assert root.path == tmp_path / "autonoma"
    fallback = resolve_data_root({}, source_root=tmp_path / "src")
    assert fallback.path.name == "autonoma"
    assert fallback.path.is_absolute()  # la raíz se resuelve siempre en absoluto


def test_frozen_executable_uses_its_own_directory(tmp_path: Path) -> None:
    root = resolve_data_root({}, frozen=True, executable=str(tmp_path / "autonoma"))
    assert root.origin is DataOrigin.FROZEN_EXECUTABLE
    assert root.path == tmp_path


def test_resolution_never_touches_the_process_environment(tmp_path: Path) -> None:
    before = dict(os.environ)
    resolve_data_root(None, override=str(tmp_path))
    assert dict(os.environ) == before


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1", True),
        ("true", True),
        ("YES", True),
        ("on", True),
        ("0", False),
        ("false", False),
        ("no", False),
        ("off", False),
    ],
)
def test_env_flags_use_a_whitelist_of_falsy_values(raw: str, expected: bool) -> None:
    """Cualquier texto distinto de los falsy explícitos enciende la preferencia."""
    preferences = RenderPreferences.from_environment({"AUTONOMA_QUIET": raw})
    assert preferences.quiet is expected


def test_unset_flags_are_off() -> None:
    preferences = RenderPreferences.from_environment({})
    assert preferences == RenderPreferences()
    assert preferences.use_rich is True and preferences.animate is True


def test_plain_flag_disables_rich_and_animation() -> None:
    preferences = RenderPreferences.from_environment({}, plain=True, rich_available=True)
    assert preferences.use_rich is False and preferences.animate is False


def test_reduced_motion_kills_spinners_but_keeps_markup() -> None:
    preferences = RenderPreferences.from_environment({"AUTONOMA_REDUCED_MOTION": "1"})
    assert preferences.animate is False and preferences.use_rich is True


def test_missing_rich_degrades_to_plain() -> None:
    preferences = RenderPreferences.from_environment({}, rich_available=False)
    assert preferences.use_rich is False


def test_no_color_only_depends_on_presence_of_the_variable() -> None:
    assert RenderPreferences.from_environment({"NO_COLOR": ""}).no_color is True
    assert RenderPreferences.from_environment({"NO_COLOR": "0"}).no_color is True
    assert RenderPreferences.from_environment({}).no_color is False


def test_context_is_immutable_and_copies_instead_of_mutating() -> None:
    context = RuntimeContext(data_root=DataRoot(Path("/srv"), DataOrigin.FLAG), render=RenderPreferences(plain=True))
    allowed = context.with_allow_commands(allow=True)
    assert context.allow_commands is False and allowed.allow_commands is True
    quiet = allowed.with_render(quiet=True)
    assert allowed.render.quiet is False and quiet.render.quiet is True
    with pytest.raises(dataclasses.FrozenInstanceError):
        context.allow_commands = True  # type: ignore[misc]


def test_detect_reads_flags_without_side_effects(tmp_path: Path) -> None:
    context = RuntimeContext.detect(env={"AUTONOMA_PLAIN": "1"}, data_dir=str(tmp_path), allow_commands=True)
    assert context.root == tmp_path
    assert context.use_rich is False
    assert context.allow_commands is True
    assert context.as_dict()["data_root"]["origin"] == "flag"
    assert context.as_dict()["render"]["plain"] is True


# ------------------------------------------------------------------ limpiezas
def test_registering_the_same_callable_twice_keeps_one_cleanup() -> None:
    panic = PanicController()
    log: list[str] = []

    def close_resource() -> None:
        log.append("cerrado")

    panic.register_cleanup(close_resource)
    panic.register_cleanup(close_resource)
    assert panic.registered_cleanups == 1
    panic.panic()
    assert log == ["cerrado"]


def test_cleanups_run_before_panic_hooks_and_failures_are_isolated() -> None:
    panic = PanicController()
    order: list[str] = []

    def broken() -> None:
        order.append("rota")
        raise RuntimeError("el recurso ya estaba cerrado")

    panic.register_cleanup(broken)
    panic.register_cleanup(lambda: order.append("ok"))
    panic.on_panic(lambda: order.append("hook"))
    assert panic.panic() is True
    assert order == ["rota", "ok", "hook"]
    assert panic.panic() is False  # segundo aviso no repite el trabajo de limpieza


def test_unregister_cleanup_detaches_only_the_given_callable() -> None:
    panic = PanicController()
    hits: list[str] = []
    keep = lambda: hits.append("keep")  # noqa: E731
    drop = lambda: hits.append("drop")  # noqa: E731
    panic.register_cleanup(keep)
    panic.register_cleanup(drop)
    panic.unregister_cleanup(drop)
    panic.panic()
    assert hits == ["keep"]


def test_wait_wakes_up_immediately_when_panicked() -> None:
    panic = PanicController()
    panic.panic()
    started = time.perf_counter()
    assert panic.wait(30) is True
    assert time.perf_counter() - started < 1.0


def test_busy_flag_tracks_work_in_progress() -> None:
    panic = PanicController()
    assert panic.busy is False
    panic.mark_busy()
    assert panic.busy is True
    panic.mark_idle()
    assert panic.busy is False


def test_session_and_job_directories_follow_the_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Poder mover la memoria de sitio es tan importante como poder apagarla.

    El patrón es el de `LOG_DIR`/`KNOWLEDGE_DIR`: una variable de entorno y su clave
    equivalente en `config.json`, resueltas siempre bajo la raíz de datos si son relativas.
    """
    from autonoma.config import load_settings

    monkeypatch.setenv("SESSIONS_DIR", "mi-historial")
    monkeypatch.setenv("JOBS_DIR", str(tmp_path / "trabajos"))
    monkeypatch.setenv("KNOWLEDGE_DIR", "notas")
    settings = load_settings(overrides={}, data_root=DataRoot(tmp_path, DataOrigin.FLAG))
    assert settings.sessions_path() == tmp_path / "mi-historial"
    assert settings.jobs_path() == tmp_path / "trabajos"
    assert settings.knowledge_path() == tmp_path / "notas"
