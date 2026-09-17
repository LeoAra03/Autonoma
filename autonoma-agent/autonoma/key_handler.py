"""Botón de pánico (tecla P) y controlador de cancelación cooperativa."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)


class PanicError(Exception):
    """Se lanza cuando el usuario detiene una tarea en curso."""


class PanicController:
    """Estado compartido: bandera de pánico, limpiezas y registro de recursos."""

    def __init__(self) -> None:
        self._event = threading.Event()
        self._busy = threading.Event()
        self._lock = threading.Lock()
        self._cleanups: list[Callable[[], None]] = []
        self._on_panic: list[Callable[[], None]] = []

    @property
    def is_set(self) -> bool:
        return self._event.is_set()

    @property
    def busy(self) -> bool:
        return self._busy.is_set()

    def mark_busy(self) -> None:
        self._busy.set()

    def mark_idle(self) -> None:
        self._busy.clear()

    def register_cleanup(self, fn: Callable[[], None]) -> None:
        with self._lock:
            self._cleanups.append(fn)

    def unregister_cleanup(self, fn: Callable[[], None]) -> None:
        with self._lock:
            try:
                self._cleanups.remove(fn)
            except ValueError:
                pass

    def on_panic(self, fn: Callable[[], None]) -> None:
        with self._lock:
            self._on_panic.append(fn)

    def check(self) -> None:
        """Abortar el hilo de trabajo si el usuario pulsó P."""
        if self._event.is_set():
            raise PanicError("Detenido por usuario")

    def panic(self) -> bool:
        """Activa el pánico y ejecuta limpiezas. Devuelve False si no había tarea."""
        if not self._busy.is_set() and not self._event.is_set():
            # Permitimos pánico aunque no esté busy por si hay una carrera.
            pass
        already = self._event.is_set()
        self._event.set()
        with self._lock:
            cleanups = list(self._cleanups)
            hooks = list(self._on_panic)
        for fn in cleanups:
            try:
                fn()
            except Exception as exc:  # noqa: BLE001 — la limpieza no debe tumbar el pánico
                logger.debug("Cleanup de pánico falló: %s", exc)
        for fn in hooks:
            try:
                fn()
            except Exception as exc:  # noqa: BLE001
                logger.debug("Hook de pánico falló: %s", exc)
        return not already

    def reset(self) -> None:
        self._event.clear()

    def wait(self, timeout: float | None = None) -> bool:
        return self._event.wait(timeout)


class KeyHandler:
    """Listener no bloqueante de teclado. La tecla P cancela la tarea en curso."""

    def __init__(self, panic: PanicController, panic_key: str = "p") -> None:
        self.panic = panic
        self.panic_key = panic_key.lower()
        self._listener: Any = None
        self._started = False
        self._lock = threading.Lock()
        self.last_error: str | None = None

    def _on_press(self, key: Any) -> None:
        try:
            char = getattr(key, "char", None)
            if char is None:
                return
            if str(char).lower() != self.panic_key:
                return
        except Exception:  # noqa: BLE001
            return
        if self.panic.busy or self.panic.is_set:
            logger.info("Tecla de pánico detectada")
            self.panic.panic()

    def start(self) -> bool:
        """Arranca el listener en un hilo daemon. Nunca bloquea el hilo llamante."""
        with self._lock:
            if self._started:
                return True
            try:
                from pynput import keyboard
            except Exception as exc:  # noqa: BLE001
                self.last_error = f"pynput no disponible: {exc}"
                logger.warning(self.last_error)
                return False
            try:
                self._listener = keyboard.Listener(
                    on_press=self._on_press,
                    daemon=True,
                )
                self._listener.start()
                self._started = True
                logger.info("KeyHandler activo — tecla de pánico: %s", self.panic_key.upper())
                return True
            except Exception as exc:  # noqa: BLE001
                self.last_error = (
                    f"No se pudo iniciar el listener de teclado ({exc}). "
                    "En Linux suele hacer falta un servidor X/Wayland. Usa Ctrl+C en la CLI."
                )
                logger.warning(self.last_error)
                self._listener = None
                return False

    def stop(self) -> None:
        with self._lock:
            listener = self._listener
            self._listener = None
            self._started = False
        if listener is not None:
            try:
                listener.stop()
            except Exception as exc:  # noqa: BLE001
                logger.debug("Error al detener KeyHandler: %s", exc)
