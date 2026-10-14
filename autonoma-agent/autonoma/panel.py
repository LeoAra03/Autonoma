"""Panel local: ver, dirigir y parar el lazo desde el navegador (incluido el del celular).

No es una app ni un servidor público: es una página mínima que sirve el propio Autonoma
para que puedas asomarte desde otra pantalla —mismo wifi— y ver por dónde va el lazo,
escribirle una instrucción nueva o pararlo sin volver al teclado del PC.

Las reglas que se respetan aquí no son adorno:

- **Token por arranque.** Se genera nuevo cada vez y se exige en todas las rutas. Sin él
  no hay página ni acción: cualquiera que llegue a la URL sin el token se va con un 401.
- **Sólo local por defecto.** Escucha en `127.0.0.1`. Para verlo desde el celular hay que
  decirlo (`--panel 0.0.0.0`), y entonces se avisa de que eso es visible en la red.
- **Sin registro HTTP.** El token viaja en la URL; un log con la URL sería un log con la
  llave. El servidor no escribe peticiones en ninguna parte.
- **Nada se ejecuta desde la web.** Lo único que se puede escribir es `steer.txt` y lo único
  que se puede crear es `STOP`: las mismas dos cosas que harías tú mismo en el disco.
- **Lo que viene del modelo se escapa** antes de imprimirlo, y los secretos conocidos se
  enmascaran: un turno puede contener una clave, y esta página se mira desde un celular.
"""

from __future__ import annotations

import hmac
import html
import ipaddress
import json
import logging
import secrets
import socket
import threading
from contextlib import suppress
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Final
from urllib.parse import parse_qs, urlparse

from autonoma.supervisor import STATE_FILENAME, STEER_FILENAME, STOP_FILENAME, TURNS_FILENAME

__all__ = [
    "DEFAULT_PORT",
    "Panel",
    "PanelConfig",
    "direcciones_lan",
    "iniciar_panel",
]

DEFAULT_PORT: Final[int] = 8787
#: Estados del lazo ya terminado (`run_supervisor`); mientras corre, el informe
#: guarda el estado del ÚLTIMO TURNO (ok/error), no el del lazo.
_FINALES: Final[frozenset[str]] = frozenset({"done", "blocked", "exhausted", "failed", "cancelled"})
_REFRESCO: Final[int] = 10
_PREVIEW: Final[int] = 400
_TOKEN_BYTES: Final[int] = 24
_MIN_SECRETO: Final[int] = 8
logger = logging.getLogger(__name__)


def direcciones_lan() -> tuple[str, ...]:
    """IPs de esta máquina en su red local, para poder abrirlas desde el celular."""
    encontradas: set[str] = set()
    # Un socket UDP "conectado" no envía nada: sólo sirve para que el SO elija interfaz.
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.0.2.1", 80))  # TEST-NET-1: documentación, nadie la usa
            salida = probe.getsockname()[0]
            if isinstance(salida, str):
                encontradas.add(salida)
    except OSError:
        logger.debug("panel.sin_interfaz_de_salida")
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            direccion = info[4][0]
            if isinstance(direccion, str):
                encontradas.add(direccion)
    except OSError:
        logger.debug("panel.sin_resolucion_local")
    return tuple(sorted(ip for ip in encontradas if not ip.startswith("127.")))


def _enmascarar(texto: str, secretos: tuple[str, ...]) -> str:
    salida = texto
    for secreto in secretos:
        if len(secreto) >= _MIN_SECRETO:
            salida = salida.replace(secreto, "••••••••")
    return salida


def _escapar(valor: object, secretos: tuple[str, ...]) -> str:
    """Listo para meter en HTML: enmascarado y escapado."""
    return html.escape(_enmascarar(str(valor), secretos))


def _leer_json(ruta: Path) -> dict[str, Any]:
    try:
        carga = json.loads(ruta.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return carga if isinstance(carga, dict) else {}


@dataclass(frozen=True, slots=True)
class PanelConfig:
    """Ajustes del panel. `secretos` son valores que nunca deben verse en la pantalla."""

    directorio: Path
    token: str
    direccion: str = "127.0.0.1"
    puerto: int = DEFAULT_PORT
    secretos: tuple[str, ...] = ()


class Panel:
    """Servidor de una sola página, en su propio hilo y con token obligatorio."""

    def __init__(self, config: PanelConfig) -> None:
        self._config = config
        self._server: ThreadingHTTPServer | None = None
        self._hilo: threading.Thread | None = None

    # ------------------------------------------------------------------ estado
    def estado(self) -> dict[str, Any]:
        """Lo que se ve en la página: objetivo, por dónde va y qué se le puede pedir."""
        directorio = self._config.directorio
        reporte = _leer_json(directorio / TURNS_FILENAME)
        guardado = _leer_json(directorio / STATE_FILENAME)
        crudos = reporte.get("turns")
        turnos: list[Any] = list(crudos) if isinstance(crudos, list) else []
        ultimo = turnos[-1] if turnos and isinstance(turnos[-1], dict) else {}
        instruccion = str(ultimo.get("instruction") or "")
        # Mientras el lazo corre, el informe guarda el estado del último turno (`ok`/`error`),
        # no el del lazo: sólo un estado final conocido significa que ya terminó.
        final = str(reporte.get("state") or "")
        parada = (directorio / STOP_FILENAME).exists()
        pendiente = (directorio / STEER_FILENAME).exists()
        if final in _FINALES:
            situacion = final
        elif parada:
            situacion = "parada pedida"
        elif turnos or guardado:
            situacion = "trabajando"
        else:
            situacion = "sin corridas"
        return {
            "goal": str(guardado.get("goal") or reporte.get("goal") or ""),
            "turno": int(guardado.get("turno") or len(turnos)),
            "state": situacion,
            "summary": str(reporte.get("summary") or ""),
            "stopped_by": str(reporte.get("stopped_by") or ""),
            "turnos": len(turnos),
            "ultimo": instruccion[:_PREVIEW],
            "parada_pedida": parada,
            "orden_pendiente": pendiente,
            "actualizado": str(guardado.get("actualizado") or ""),
        }

    # ------------------------------------------------------------------ arranque
    def start(self) -> None:
        config = self._config
        manejador = _FabricaManejadores(self)
        try:
            config.directorio.mkdir(parents=True, exist_ok=True)
        except OSError:
            logger.warning("panel.sin_directorio", extra={"path": str(config.directorio)})
        # S104: escuchar en 0.0.0.0 es justamente la opción para verlo desde el celular,
        # y sólo se llega aquí si el usuario lo pidió (`--panel 0.0.0.0`) tras leer el aviso.
        self._server = ThreadingHTTPServer((config.direccion, config.puerto), manejador)
        self._server.daemon_threads = True
        self._hilo = threading.Thread(target=self._server.serve_forever, name="autonoma-panel", daemon=True)
        self._hilo.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._hilo is not None:
            self._hilo.join(timeout=2.0)
            self._hilo = None

    def __enter__(self) -> Panel:
        self.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.stop()

    @property
    def puerto(self) -> int:
        if self._server is None:
            return self._config.puerto
        return int(self._server.server_address[1])

    def urls(self) -> tuple[str, ...]:
        """Dónde abrirlo: local siempre, y en la red local si está escuchando fuera."""
        token = self._config.token
        locales = ["127.0.0.1"]
        if self._config.direccion not in {"127.0.0.1", "localhost", "::1"}:
            locales = [*locales, *direcciones_lan()]
        vistos: list[str] = []
        for direccion in [*locales, self._config.direccion]:
            with suppress(ValueError):
                if ipaddress.ip_address(direccion).is_unspecified:
                    continue  # comodín (0.0.0.0): sirve para escuchar, no para abrir
            url = f"http://{direccion}:{self.puerto}/?token={token}"
            if url not in vistos:
                vistos.append(url)
        return tuple(vistos)

    # ------------------------------------------------------------------ acciones
    def escribir_orden(self, texto: str) -> bool:
        """Escribe `steer.txt`: una instrucción que el lazo consume sin pararse."""
        if not texto.strip():
            return False
        try:
            (self._config.directorio / STEER_FILENAME).write_text(texto.strip() + "\n", encoding="utf-8")
        except OSError:
            logger.warning("panel.orden_no_escrita")
            return False
        return True

    def pedir_parada(self) -> bool:
        """Crea `STOP`: el lazo termina limpio al empezar el turno siguiente."""
        try:
            (self._config.directorio / STOP_FILENAME).write_text("parada pedida desde el panel\n", encoding="utf-8")
        except OSError:
            logger.warning("panel.parada_no_escrita")
            return False
        return True

    def _autorizado(self, valores: list[str] | None) -> bool:
        esperado = self._config.token
        return any(hmac.compare_digest(valor, esperado) for valor in valores or [])

    def _pagina(self) -> str:
        estado = self.estado()
        secretos = self._config.secretos
        token = self._config.token
        def esc(valor: object) -> str:
            return _escapar(valor, secretos)
        parada = "ya está pedida" if estado["parada_pedida"] else ""
        orden = "hay una instrucción pendiente" if estado["orden_pendiente"] else ""
        return f"""<!doctype html>
<html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="{_REFRESCO}">
<title>Autonoma · {esc(estado['state'])}</title>
<style>
 body {{ font: 16px/1.5 system-ui, sans-serif; margin: 0 auto; padding: 1rem; max-width: 34rem; color: #e8e6e3; background: #16181d; }}
 h1 {{ font-size: 1.1rem; margin: 0 0 .5rem; }}
 .dato {{ background: #1f2229; border-radius: .5rem; padding: .6rem .8rem; margin: .5rem 0; }}
 .ultimo {{ white-space: pre-wrap; font-family: ui-monospace, monospace; font-size: 13px; max-height: 12rem; overflow: auto; }}
 button {{ font: inherit; padding: .6rem 1rem; border-radius: .5rem; border: 0; background: #d9534f; color: #fff; }}
 textarea {{ width: 100%; font: inherit; border-radius: .5rem; border: 1px solid #444; background: #11131a; color: #e8e6e3; padding: .5rem; }}
 input[type=submit] {{ font: inherit; margin-top: .5rem; padding: .5rem 1rem; border-radius: .5rem; border: 0; background: #3a7bd5; color: #fff; }}
 .nota {{ color: #9aa0a6; font-size: .85rem; }}
</style></head><body>
<h1>Autonoma · {esc(estado['state'])}</h1>
<div class="dato"><b>Objetivo:</b> {esc(estado['goal']) or '(ninguno todavía)'}</div>
<div class="dato"><b>Turno:</b> {esc(estado['turno'])} de {esc(estado['turnos'])} registrados {esc(orden)}</div>
<div class="dato"><b>Resumen:</b> {esc(estado['summary']) or '—'}</div>
<div class="dato ultimo">{esc(estado['ultimo']) or '(sin turnos todavía)'}</div>
<form method="post" action="/orden">
  <input type="hidden" name="token" value="{html.escape(token)}">
  <label>Instrucción nueva (se aplica en el próximo turno, sin parar el lazo):</label>
  <textarea name="texto" rows="3" placeholder="revisa primero los tests de pagos"></textarea>
  <input type="submit" value="Enviar instrucción">
</form>
<form method="post" action="/parar" onsubmit="return confirm('¿Parar el lazo?')">
  <input type="hidden" name="token" value="{html.escape(token)}">
  <button type="submit">Parar el lazo</button> <span class="nota">{esc(parada)}</span>
</form>
<p class="nota">Se actualiza cada {_REFRESCO} s · el lazo acaba el turno que está haciendo.</p>
</body></html>"""


class _FabricaManejadores:
    """La factoría existe para poder inyectar el token por prueba (sin Variables globales)."""

    def __init__(self, panel: Panel) -> None:
        self._panel = panel

    def __call__(self, *args: Any, **kwargs: Any) -> _Manejador:
        return _Manejador(self._panel, *args, **kwargs)


class _Manejador(BaseHTTPRequestHandler):
    """Rutas: `/`, `/estado.json`, `POST /orden`, `POST /parar`. Nada más."""

    server_version = "AutonomaPanel"

    def __init__(self, panel: Panel, *args: Any, **kwargs: Any) -> None:
        self._panel = panel
        super().__init__(*args, **kwargs)

    # Silencio: el token va en la URL y un log con la URL es un log con la llave.
    def log_message(self, formato: str, *args: Any) -> None:  # noqa: ARG002 — firma de la librería
        logger.debug("panel.peticion", extra={"ruta": urlparse(self.path).path})

    def do_GET(self) -> None:
        ruta = urlparse(self.path).path
        if not self._panel._autorizado(self._token_de_consulta()):  # noqa: SLF001 — mismo módulo
            self._responder(HTTPStatus.UNAUTHORIZED, "sin token")
            return
        if ruta in {"/", "/index.html"}:
            self._enviar_html(self._panel._pagina())  # noqa: SLF001
        elif ruta == "/estado.json":
            self._enviar_json(self._panel.estado())
        else:
            self._responder(HTTPStatus.NOT_FOUND, "no existe")

    def do_POST(self) -> None:
        ruta = urlparse(self.path).path
        cuerpo = self._leer_cuerpo()
        if not self._panel._autorizado(self._token_de_formulario(cuerpo)):  # noqa: SLF001
            self._responder(HTTPStatus.UNAUTHORIZED, "sin token")
            return
        if ruta == "/orden":
            self._panel.escribir_orden(cuerpo.get("texto", [""])[0])
            self._redirigir()
        elif ruta == "/parar":
            self._panel.pedir_parada()
            self._redirigir()
        else:
            self._responder(HTTPStatus.NOT_FOUND, "no existe")

    # ------------------------------------------------------------------ ayudas
    def _token_de_consulta(self) -> list[str]:
        return parse_qs(urlparse(self.path).query).get("token", [])

    def _token_de_formulario(self, cuerpo: dict[str, list[str]]) -> list[str]:
        return [*cuerpo.get("token", []), *self._token_de_consulta()]

    def _leer_cuerpo(self) -> dict[str, list[str]]:
        try:
            largo = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            largo = 0
        if largo <= 0:
            return {}
        raw = self.rfile.read(largo).decode("utf-8", errors="replace")
        return parse_qs(raw, keep_blank_values=True)

    def _responder(self, estado: HTTPStatus, texto: str) -> None:
        cuerpo = texto.encode("utf-8")
        self.send_response(estado)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(cuerpo)))
        self.end_headers()
        self.wfile.write(cuerpo)

    def _enviar_html(self, pagina: str) -> None:
        cuerpo = pagina.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(cuerpo)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(cuerpo)

    def _enviar_json(self, datos: dict[str, Any]) -> None:
        cuerpo = json.dumps(datos, ensure_ascii=False).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(cuerpo)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(cuerpo)

    def _redirigir(self) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", f"/?token={self._panel._config.token}")  # noqa: SLF001
        self.send_header("Content-Length", "0")
        self.end_headers()


def iniciar_panel(
    directorio: Path,
    *,
    direccion: str = "127.0.0.1",
    puerto: int = DEFAULT_PORT,
    secretos: tuple[str, ...] = (),
    token: str | None = None,
) -> Panel:
    """Crea y arranca el panel. El token se genera nuevo en cada arranque."""
    panel = Panel(
        PanelConfig(
            directorio=directorio,
            token=token or secrets.token_urlsafe(_TOKEN_BYTES),
            direccion=direccion,
            puerto=puerto,
            secretos=secretos,
        )
    )
    panel.start()
    return panel
