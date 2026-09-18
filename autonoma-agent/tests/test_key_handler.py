"""Listener de pánico: estados observables, tecla exacta y ausencia de dependencias."""

from __future__ import annotations

import pytest

from autonoma.key_handler import (
    KeyHandler,
    ListenerState,
    ListenerStatus,
    PanicController,
    PanicError,
)


class FakeListener:
    """Doble de `pynput.keyboard.Listener`: registra ciclos de vida y callbacks."""

    def __init__(self, *, on_press=None, joined: bool = True) -> None:
        self.started = 0
        self.stopped = 0
        self.joined = 0
        self.on_press = on_press
        self._joined = joined

    def start(self) -> None:
        self.started += 1

    def stop(self) -> None:
        self.stopped += 1

    def join(self, timeout: float | None = None) -> bool:
        self.joined += 1
        return self._joined

    def is_alive(self) -> bool:
        return self.started > self.stopped


def test_listener_status_describe_is_actionable() -> None:
    running = ListenerStatus(ListenerState.RUNNING)
    assert running.running is True
    assert "activo" in running.describe()
    down = ListenerStatus(ListenerState.UNAVAILABLE, "pynput no instalado")
    assert down.running is False
    assert "inactivo" in down.describe() and "pynput no instalado" in down.describe()
    assert ListenerStatus(ListenerState.DISABLED).describe().startswith("Listener P: inactivo")


def test_disabled_handler_never_touches_pynput() -> None:
    panic = PanicController()
    handler = KeyHandler(panic, enabled=False)
    status = handler.start()
    assert status.state is ListenerState.DISABLED
    assert handler.stop() is None  # detener lo que no arrancó no es un error
    assert panic.is_set is False


def test_missing_pynput_is_reported_not_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sin el extra [keyboard] el listener se declara no disponible; la sesión sigue válida."""
    panic = PanicController()
    handler = KeyHandler(panic, enabled=True)

    def boom(self: KeyHandler) -> None:
        raise ImportError("pynput no instalado")

    monkeypatch.setattr(KeyHandler, "_build_listener", boom)
    status = handler.start()
    assert status.state is ListenerState.UNAVAILABLE
    assert "pynput no instalado" in status.reason
    assert panic.is_set is False


def test_press_only_cancels_a_running_task_on_the_panic_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """La tecla solo significa "corta esto" si hay tarea; no apaga nada en reposo."""
    panic = PanicController()
    handler = KeyHandler(panic, "p", enabled=True)
    listener = FakeListener()
    monkeypatch.setattr(KeyHandler, "_build_listener", lambda self: listener)
    status = handler.start()
    assert status.state is ListenerState.RUNNING
    assert listener.started == 1

    handler._on_press(SimpleKey("p"))  # en reposo: no hay nada que cancelar
    assert panic.is_set is False
    panic.mark_busy()
    handler._on_press(SimpleKey("x"))  # otra tecla: ignorable
    assert panic.is_set is False
    handler._on_press(SimpleKey("P"))  # el comparador ignora mayúsculas
    assert panic.is_set is True
    handler.stop()
    assert listener.stopped == 1
    assert handler.status.state is ListenerState.STOPPED


def test_a_listener_that_refuses_to_stop_does_not_break_shutdown(monkeypatch: pytest.MonkeyPatch) -> None:
    """`stop()` debe dejar la sesión cerrable aunque el hilo gráfico responda mal."""
    panic = PanicController()
    handler = KeyHandler(panic, enabled=True)

    class RudeListener:
        def __init__(self) -> None:
            self.stop_calls = 0

        def start(self) -> None:
            return None

        def stop(self) -> None:
            self.stop_calls += 1
            raise RuntimeError("el backend de X11 no responde")

    rude = RudeListener()
    monkeypatch.setattr(KeyHandler, "_build_listener", lambda self: rude)
    assert handler.start().state is ListenerState.RUNNING
    handler.stop()  # no propaga
    assert rude.stop_calls == 1
    assert handler.status.state is ListenerState.STOPPED


def test_press_callback_never_propagates_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """El callback corre en el hilo del listener: un fallo ahí no puede tumbar la app."""
    panic = PanicController()
    handler = KeyHandler(panic, enabled=True)

    class ExplodingKey:
        @property
        def char(self) -> str:
            raise RuntimeError("el objeto de pynput explotó")

    handler._on_press(ExplodingKey())  # no lanza
    assert panic.is_set is False


class SimpleKey:
    def __init__(self, char: str) -> None:
        self.char = char


def test_panic_controller_cancels_only_when_a_task_is_running() -> None:
    """`check()` es la única señal de cancelación; sin turno activo no hay nada que cortar."""
    panic = PanicController()
    panic.check()  # idle: no lanza
    panic.mark_busy()
    panic.reset()
    panic.check()  # reset limpia la bandera
    panic.panic()
    with pytest.raises(PanicError):
        panic.check()
    panic.reset()
    panic.check()
