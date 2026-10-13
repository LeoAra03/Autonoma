"""Conexión por CDP a un Brave que ya está corriendo.

No se lanza ningún navegador: nos adjuntamos al que abriste con
`--remote-debugging-port=9222`. Dos razones:

1. El perfil (y por tanto tu sesión) es el tuyo: inicias sesión una vez y listo.
2. La pestaña del bot es una pestaña más. No se mueve el ratón del sistema, no se
   roba el foco: los eventos se envían al DOM por CDP aunque la ventana esté detrás
   de una película.

Regla de oro de este archivo: **nunca** `bring_to_front()`, **nunca** foco.
"""

from __future__ import annotations

import time
from contextlib import suppress
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

__all__ = ["ErrorCDP", "Sesion", "abrir_pestana_fondo", "conectar", "localizar_pestana"]


class ErrorCDP(RuntimeError):
    """No se pudo conectar o localizar la pestaña. El mensaje dice qué hacer."""


@dataclass(slots=True)
class Sesion:
    """Brave conectado, con la pestaña del bot ya localizada."""

    gestor: Any
    navegador: Any
    contexto: Any
    pagina: Any
    nueva: bool = False  # ¿tuvimos que abrir la pestaña?

    def close(self) -> None:
        """Cierra sólo lo nuestro: el navegador se queda como estaba."""
        with suppress(Exception):
            self.gestor.stop()

    def __enter__(self) -> Sesion:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def _puerto(url_cdp: str) -> str:
    try:
        analizada = urlparse(url_cdp)
        return str(analizada.port or "9222")
    except ValueError:
        return "9222"


def conectar(url_cdp: str, *, timeout_ms: int = 10_000) -> tuple[Any, Any]:
    """Devuelve `(gestor, navegador)`. El gestor hay que pararlo al terminar."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover — depende del entorno
        raise ErrorCDP(
            "falta Playwright en este Python. Se instala con: pip install -r requirements.txt "
            "(no hace falta `playwright install`: nos adjuntamos a tu Brave, no lo bajamos)."
        ) from exc

    gestor = sync_playwright().start()
    try:
        navegador = gestor.chromium.connect_over_cdp(url_cdp, timeout=timeout_ms)
    except Exception as exc:
        gestor.stop()
        raise ErrorCDP(
            f"No pude hablar con {url_cdp}. ¿Arrancaste Brave con "
            f"--remote-debugging-port={_puerto(url_cdp)}? (Detalle: {type(exc).__name__}: {exc})"
        ) from exc
    if not navegador.contexts:
        gestor.stop()
        raise ErrorCDP("Brave respondió pero no tiene contextos: ¿se está cerrando?")
    return gestor, navegador


def localizar_pestana(contexto: Any, patron: str) -> Any | None:
    """La pestaña cuya URL contiene `patron`, o `None`. No abre nada."""
    for pagina in contexto.pages:
        try:
            if pagina.is_closed():
                continue
            if patron.lower() in (pagina.url or "").lower():
                return pagina
        except Exception:  # noqa: BLE001, S112 — una pestaña que muere no es un error: se salta
            continue
    return None


def abrir_pestana_fondo(navegador: Any, contexto: Any, url: str, *, espera_s: float = 5.0) -> Any:
    """Abre la pestaña en segundo plano: `new_page()` robaría el foco y eso no se hace.

    Si el truco CDP no está disponible, cae a `new_page()`, que al menos navega.
    """
    try:
        cdp = navegador.new_browser_cdp_session()
        cdp.send("Target.createTarget", {"url": url, "background": True})
    except Exception:  # noqa: BLE001 — sin CDP de navegador se hace como se puede
        pagina = contexto.new_page()
        pagina.goto(url, wait_until="domcontentloaded")
        return pagina
    limite = time.time() + espera_s
    while time.time() < limite:
        pagina = localizar_pestana(contexto, url.rsplit("//", maxsplit=1)[-1].split("/", maxsplit=1)[0])
        if pagina is not None:
            return pagina
        time.sleep(0.1)
    paginas = [p for p in contexto.pages if not p.is_closed()]
    return paginas[-1] if paginas else contexto.new_page()
