"""Botón de pánico (tecla P) y controlador de cancelación cooperativa.

`PanicController` es el único estado compartido entre el hilo de UI, el hilo de
trabajo y el listener global: todo acceso pasa por un lock o por primitivas de
`threading`. El `KeyHandler` ya no expone `last_error` mutable desde fuera; reporta
un `ListenerStatus` inmutable, incluido el motivo "deshabilitado por configuración".
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any, Final

from autonoma.errors import CancelledByUserError

__all__ = [
    "CancelledByUserError",
    "KeyHandler",
    "ListenerState",
    "ListenerStatus",
    "PanicController",
    "PanicError",
]

logger = logging.getLogger(__name__)

_PANIC_MESSAGE: Final[str] = "Detenido por usuario"


class PanicError(CancelledByUserError):
    """Se lanza cuando el usuario detiene la tarea en curso."""

    def __init__(self, message: str = _PANIC_MESSAGE) -> None:
        super().__init__(message)


class ListenerState(str, Enum):
    """Estado observable del listener global de pánico."""

    DISABLED = "disabled"
    RUNNING = "running"
    UNAVAILABLE = "unavailable"
    STOPPED = "stopped"


@dataclass(frozen=True, slots=True)
class ListenerStatus:
    """Resultado de intentar arrancar el listener: nunca se infiere de cadenas."""

    state: ListenerState
    reason: str | None = None

    @property
    def running(self) -> bool:
        return self.state is ListenerState.RUNNING

    def describe(self) -> str:
        """Una línea para la consola: estado, por qué si no arrancó, y el plan B (Ctrl+C)."""
        if self.running:
            return "Listener P: activo"
        reason = (self.reason or "").strip().rstrip(".")
        if not reason:
            return "Listener P: inactivo. Usa Ctrl+C en la terminal."
        return f"Listener P: inactivo — {reason}."


Cleanups = Callable[[], None]


class PanicController:
    """Estado compartido: bandera de pánico, ocupación y limpieza de recursos."""

    __slots__ = ("_busy", "_cleanups", "_event", "_lock", "_on_panic")

    def __init__(self) -> None:
        self._event = threading.Event()
        self._busy = threading.Event()
        self._lock = threading.Lock()
        self._cleanups: list[Cleanups] = []
        self._on_panic: list[Cleanups] = []

    # ------------------------------------------------------------- propiedades
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

    # --------------------------------------------------------------- registros
    def register_cleanup(self, cleanup: Cleanups) -> None:
        """Idempotente por callable: reconstruir el agente no duplica limpiezas."""
        with self._lock:
            if cleanup not in self._cleanups:
                self._cleanups.append(cleanup)

    def unregister_cleanup(self, cleanup: Cleanups) -> None:
        """Comparación por igualdad, no por identidad: `obj.close` crea un bound method nuevo
        en cada acceso, y con `is` la limpieza quedaría registrada para siempre."""
        with self._lock:
            self._cleanups = [item for item in self._cleanups if item != cleanup]

    def on_panic(self, hook: Cleanups) -> None:
        with self._lock:
            if hook not in self._on_panic:
                self._on_panic.append(hook)

    # ------------------------------------------------------------------- flujo
    def check(self) -> None:
        """Aborta el hilo de trabajo si el usuario pidió detener."""
        if self._event.is_set():
            raise PanicError

    def panic(self) -> bool:
        """Activa la cancelación y ejecuta limpiezas. False si ya estaba activa."""
        already_set = self._event.is_set()
        self._event.set()
        with self._lock:
            cleanups = list(self._cleanups)
            hooks = list(self._on_panic)
        for callback in (*cleanups, *hooks):
            try:
                callback()
            except Exception as exc:  # noqa: BLE001 — la limpieza nunca tapa el pánico
                logger.debug(
                    "cleanup falló",
                    extra={"event": "panic.cleanup_error", "fields": {"error": type(exc).__name__}},
                )
        return not already_set

    def reset(self) -> None:
        self._event.clear()

    def wait(self, timeout: float | None = None) -> bool:
        """Espera cancelable: despierta antes si el usuario pulsa P."""
        return self._event.wait(timeout)

    @property
    def registered_cleanups(self) -> int:
        with self._lock:
            return len(self._cleanups)


class KeyHandler:
    """Listener no bloqueante de teclado; la tecla P cancela la tarea en curso."""

    __slots__ = ("_enabled", "_listener", "_lock", "_started", "_status", "panic", "panic_key")

    def __init__(self, panic: PanicController, panic_key: str = "p", *, enabled: bool = False) -> None:
        self.panic = panic
        self.panic_key = panic_key.lower()
        self._listener: Any = None
        self._started = False
        self._enabled = enabled
        self._lock = threading.Lock()
        self._status = ListenerStatus(
            ListenerState.DISABLED, "Desactivado por defecto; --global-hotkey para habilitar."
        )

    @property
    def status(self) -> ListenerStatus:
        return self._status

    def _on_press(self, key: Any) -> None:
        try:
            char = getattr(key, "char", None)
            if char is None or str(char).lower() != self.panic_key:
                return
        except Exception:  # noqa: BLE001 — el callback del listener no debe propagar fallos
            return
        if self.panic.busy or self.panic.is_set:
            logger.info("Tecla de pánico detectada", extra={"event": "panic.key", "fields": {}})
            self.panic.panic()

    def _build_listener(self) -> Any:
        """Fábrica del listener: el import perezoso vive aquí y es el único punto sustituirle.

        Separarlo de `start()` permite probar la máquina de estados sin `pynput` ni
        servidor gráfico, y concentra el fallo de importación en un solo lugar.
        """
        from pynput import keyboard  # import perezoso: dependencia opcional

        return keyboard.Listener(on_press=self._on_press, daemon=True)

    def start(self) -> ListenerStatus:
        """Arranca el listener en un hilo daemon; nunca bloquea al llamante."""
        with self._lock:
            if self._started:
                return self._status
            if not self._enabled:
                self._status = ListenerStatus(
                    ListenerState.DISABLED, "Desactivado por defecto; --global-hotkey para habilitar."
                )
                return self._status
            try:
                listener = self._build_listener()
                listener.start()
            except Exception as exc:  # noqa: BLE001 — X11/Wayland/seguridad de entrada
                self._status = ListenerStatus(
                    ListenerState.UNAVAILABLE,
                    (
                        f"No se pudo iniciar el listener de teclado ({exc}). En Linux suele hacer falta "
                        "un servidor X/Wayland. Usa Ctrl+C en la CLI."
                    ),
                )
                logger.warning(self._status.reason)
                return self._status
            self._listener = listener
            self._started = True
            self._status = ListenerStatus(ListenerState.RUNNING)
            logger.info(
                "KeyHandler activo",
                extra={"event": "panic.listener_started", "fields": {"key": self.panic_key.upper()}},
            )
            return self._status

    def stop(self) -> None:
        with self._lock:
            listener, self._listener, self._started = self._listener, None, False
        if listener is None:
            self._status = ListenerStatus(ListenerState.STOPPED)
            return
        try:
            listener.stop()
        except Exception as exc:  # noqa: BLE001
            logger.debug("Error al detener KeyHandler: %s", type(exc).__name__)
        finally:
            self._status = ListenerStatus(ListenerState.STOPPED)
