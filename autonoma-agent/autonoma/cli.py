"""Interfaz de línea de comandos de Autonoma: REPL, aprobación humana y diagnóstico.

Cambios de esta versión:
- Desaparece el `global RICH` y la mutación de `os.environ`: las preferencias de
  render y la raíz de datos viajan en un `RuntimeContext` inmutable.
- Los comandos del REPL se resuelven por tabla (`/clave` → método) en lugar de una
  cadena de `if`; cada handler hace una sola cosa.
- El progreso vive en `_ProgressReporter` (spinner, pausa y transcripción de
  eventos), no en clausuras anidadas dentro del turno.
- Logs JSON estructurados con `trace_id`, métricas en `/status` y `--selftest`
  para validar el ejecutable empaquetado.
- Arranque liviano: `httpx`, `bs4` y `rich` se cargan dentro de la función que los
  usa, no al importar el módulo (`--version` y `--help` no pagan esas ~130 ms ni
  esos ~14 MiB). El lazo respira con `--pausa` y dice su pico de memoria al parar.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import logging
import os
import sys
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from getpass import getpass
from pathlib import Path
from typing import Any, Final

from autonoma import __app_name__, __version__
from autonoma.agent import Agent, build_agent, build_approval_policy
from autonoma.approval import ApprovalPolicy, load_approval_policy, write_policy_template
from autonoma.config import (
    Settings,
    clear_ndus,
    load_json_config,
    load_settings,
    read_dotenv_file,
    save_api_keys,
    save_ndus,
)
from autonoma.diagnostics import run_diagnostics, run_selftest
from autonoma.errors import AutonomaError, CancelledByUserError, ExitCode
from autonoma.key_handler import KeyHandler, ListenerStatus, PanicController, PanicError
from autonoma.ndus import COPY_HINTS, import_ndus, mask, probe_sources
from autonoma.observability import (
    MetricsRegistry,
    configure_logging,
    current_trace_id,
    log_event,
    pico_memoria_mib,
)
from autonoma.panel import DEFAULT_PORT, iniciar_panel
from autonoma.panel import Panel as PanelWeb
from autonoma.ports import ConsolePort, EventKind
from autonoma.presentation import approval_heading, operation_preview, safe_text
from autonoma.runtime import RenderPreferences, RuntimeContext
from autonoma.sessions import SessionInfo, SessionStore
from autonoma.starter import (
    ARRANQUE_ENVVAR,
    REGISTRY_FILENAME,
    Arranque,
    aplicar_arranque,
    bases_del_entorno,
    resolver_arranque,
)
from autonoma.supervisor import (
    STEER_FILENAME,
    STOP_FILENAME,
    TURNS_FILENAME,
    SupervisorBudget,
    SupervisorOutcome,
    SupervisorReport,
    cargar_estado,
    goal_from_file,
    parse_continuation,
    run_supervisor,
    task_prompt,
)

__all__ = ["HELP_TEXT", "Session", "build_console", "main", "parse_args", "repl"]


def _rich_instalado() -> ImportError | None:
    """`None` si Rich está disponible; el error si no.

    Se pregunta con `find_spec` en vez de importando: cargar Rich (y con él
    markdown-it-py) cuesta ~50 ms y ~4 MiB, y hay comandos que no dibujan nada
    (`--version`, `--help`, `--doctor`, el lazo en segundo plano). El import real
    ocurre sólo donde se va a pintar.
    """
    try:
        return None if importlib.util.find_spec("rich") is not None else ImportError("rich no está instalado")
    except (ImportError, ValueError):  # pragma: no cover - entornos sin rich instalable
        return ImportError("rich no está instalado")


_RICH_IMPORT_ERROR: ImportError | None = _rich_instalado()

# Sonda de importación: se resuelve una vez al importar el módulo y no vuelve a cambiar.
RICH_AVAILABLE: Final[bool] = _RICH_IMPORT_ERROR is None

SPINNER_FRAMES: Final[str] = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
_SPINNER_INTERVAL: Final[float] = 0.08
_TOOL_PREVIEW_CHARS: Final[int] = 240
_STATUS_NOTE_LIMIT: Final[int] = 8
_CONFIRMATION_WORD: Final[str] = "SI"

HELP_TEXT: Final[str] = """
Comandos
  /help              esta ayuda
  /quit  /exit       salir
  /key               pegar NOTRACK_API_KEY (no se muestra)
  /brave             pegar BRAVE_API_KEY (opcional)
  /ndus [valor]      importar la cookie NDUS: valor suelto, `ndus=...` o cabecera Cookie
  /ndus auto         buscarla en los navegadores de esta máquina y guardarla
  /ndus archivo RUTA leerla de un cookies.txt exportado (o de la cabecera pegada)
  /ndus ver          ver el valor enmascarado   ·   /ndus borrar  quitarla del txt personal
  /status            claves, knowledge_base, pánico y métricas
  /kb                listar notas de knowledge_base
  /clear             borrar historial de conversación
  /sessions          sesiones guardadas (para --resume)
  /resume [id]       retomar el historial de una sesión
  /attach <ruta>     meter un archivo en el contexto de esta sesión
  /forget            borrar el historial visible Y la sesión en disco
  /next [objetivo]   un turno más del lazo con el paso que quedó pendiente
  /policy [ruta]     ver la política de aprobación delegada, o recargar otra
  /panic             activar cancelación entre tareas

Lazo (--supervise)
  --hasta-que-pare  sin tope de turnos ni de tiempo: sigue hasta DONE, STOP o Ctrl+C
  --continuar       retoma la corrida anterior donde se cortó (supervisor/estado.json)
  Pararlo desde otra terminal: crea el archivo supervisor/STOP
  Darle una instrucción sin pararlo: escribe supervisor/steer.txt

Arranque (qué repo y qué chat)
  --repo RUTA       repositorio sobre el que trabajar (sin preguntar)
  --repos-dir RUTA  carpeta madre que escanear buscando repos (repetible; o AUTONOMA_REPOS)
  --elegir          preguntar repositorio y chat aunque no haya terminal interactiva
  Sin --repo, el lazo pregunta: ¿sobre qué repositorio trabajo? ¿chat nuevo o uno existente?
  Las carpetas que escribas a mano y pidas recordar quedan en repos.json (raíz de datos).

Pánico
  P global es opcional: inicia con --global-hotkey si lo necesitas.
  Ctrl+C también cancela la tarea. /panic solo se lee entre tareas.
  La cancelación es cooperativa; ciertas operaciones pueden tardar en terminar.
""".strip()

BANNER: Final[str] = f"{__app_name__} v{__version__}  ·  agente local  ·  Ctrl+C = cancelar"


class PlainConsole:
    """Consola de texto simple: misma interfaz que `rich.console.Console`."""

    __slots__ = ()

    def print(self, *objects: Any, **kwargs: Any) -> None:
        for key in ("style", "highlight", "markup"):
            kwargs.pop(key, None)
        print(" ".join(str(item) for item in objects), flush=True)

    def input(self, prompt: str = "") -> str:
        return input(prompt)


class RichConsoleFactory:
    """Construye la consola Rich respetando color/markup del contexto de render."""

    __slots__ = ("_render",)

    def __init__(self, render: RenderPreferences) -> None:
        self._render = render

    def build(self) -> ConsolePort:
        from rich.console import Console
        from rich.theme import Theme

        theme = Theme({"ok": "green", "warn": "yellow", "err": "bold red", "muted": "dim", "accent": "cyan"})
        return Console(
            theme=theme,
            highlight=False,
            markup=False,
            no_color=self._render.no_color,
        )


def build_console(context: RuntimeContext) -> ConsolePort:
    """Fábrica de consola: la decisión de render se toma una vez, no en cada print."""
    if context.use_rich and RICH_AVAILABLE:
        return RichConsoleFactory(context.render).build()
    return PlainConsole()


def _configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                log_event(logging.getLogger(__name__), logging.DEBUG, "stdio.reconfigure_skipped", {})


def banner_body(settings: Settings, status: ListenerStatus) -> str:
    """Texto del banner: estado de la clave y del listener, sin cifras técnicas."""
    if settings.has_notrack_key:
        source = "credenciales.txt" if settings.personal_status.value == "ok" and settings.personal_file else ".env"
        key_line = f"NoTrack: clave configurada ({source})"
        if settings.has_ndus:
            key_line += " · NDUS listo"
    else:
        key_line = "NoTrack: FALTA NOTRACK_API_KEY  →  ponla en credenciales.txt o /key"
    return f"{BANNER}\n{key_line}\n{status.describe()}\nEscribe un prompt o /help"


def print_banner(console: ConsolePort, context: RuntimeContext, settings: Settings, status: ListenerStatus) -> None:
    body = banner_body(settings, status)
    if context.use_rich and RICH_AVAILABLE:
        from rich.panel import Panel

        console.print(Panel(body, title=__app_name__, border_style="cyan"))
        return
    console.print("=" * 60)
    console.print(body)
    console.print("=" * 60)


class _ProgressReporter:
    """Gestiona el spinner y traduce eventos del agente a salida legible."""

    def __init__(self, console: ConsolePort, render: RenderPreferences, *, quiet: bool | None = None) -> None:
        self._console = console
        self._render = render
        self._quiet = render.quiet if quiet is None else quiet
        self._stop = threading.Event()
        self._message = "pensando…"
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ ciclo
    def start(self) -> None:
        if not self._render.animate:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._spin, daemon=True, name="autonoma-spinner")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        self._thread = None
        if thread is not None:
            thread.join(timeout=1.0)

    def _spin(self) -> None:
        from rich.live import Live
        from rich.text import Text

        index = 0
        try:
            # `Live` exige una `rich.console.Console` real: el spinner sólo corre en modo rich.
            with Live(console=self._console, refresh_per_second=12, transient=True) as live:  # type: ignore[arg-type]
                while not self._stop.is_set():
                    frame = SPINNER_FRAMES[index % len(SPINNER_FRAMES)]
                    with self._lock:
                        message = self._message
                    live.update(Text(f"{frame}  {message}", style="accent"))
                    index += 1
                    self._stop.wait(_SPINNER_INTERVAL)
        except Exception as exc:  # noqa: BLE001 — la animación es decorativa y nunca rompe el turno
            log_event(
                logging.getLogger(__name__),
                logging.DEBUG,
                "ui.spinner_stopped",
                {"error": type(exc).__name__},
            )

    # ----------------------------------------------------------------- eventos
    def _set_message(self, message: str) -> None:
        with self._lock:
            self._message = message.replace("\n", " ")[:120]

    def on_event(self, kind: str, message: str) -> None:
        text = safe_text(message)
        if kind == EventKind.TOOL.value:
            self._set_message(text)
            self.stop()
            self._console.print(f"▸ herramienta  {text}", style="accent")
        elif kind == EventKind.THINK.value:
            self._set_message(text)
            if self._stop.is_set() or not self._render.animate:
                self._console.print(text)
        elif kind == EventKind.TIMING.value:
            self._console.print(text, style="muted")
        elif kind == EventKind.TOOL_RESULT.value:
            if not self._quiet:
                self._console.print(f"  ↳ {text.replace(chr(10), ' ')[:_TOOL_PREVIEW_CHARS]}", style="muted")
        elif kind == EventKind.PANIC.value:
            self.stop()
            self._console.print("Detenido por usuario", style="err")


class Session:
    """Sesión de trabajo: contexto, recursos, aprobación y ciclo de vida."""

    # Defaults de clase: una sesión construida a mano (sin el `__init__` completo) sigue
    # teniendo capa de memoria vacía en vez de `AttributeError` a mitad de turno.
    _attachments: Sequence[str] = ()
    _startup_notice: tuple[str, str] | None = None
    approval_policy: ApprovalPolicy | None = None
    _last_goal: str = ""
    _last_next: str = ""

    def __init__(
        self,
        console: ConsolePort,
        *,
        allow_commands: bool = False,
        global_hotkey: bool = False,
        context: RuntimeContext | None = None,
        resume: str | None = None,
        attach: Sequence[str] = (),
        overrides: Mapping[str, str] | None = None,
    ) -> None:
        self.console = console
        self.context = (context or RuntimeContext.detect()).with_allow_commands(allow=allow_commands)
        self._overrides: dict[str, str] = dict(overrides or {})
        self.settings = self._load_settings()
        self.metrics = MetricsRegistry()
        # La sesión vive fuera del agente: `/key` reconstruye recursos, y el historial
        # (con su id en disco) debe sobrevivir a esa recarga.
        self.sessions = SessionStore(self.settings.sessions_path(), enabled=self.settings.session_persist)
        self.session_id = self._select_session(resume)
        self._attachments: list[str] = []
        self._pending_attach = tuple(attach)
        self._resume_requested = resume
        self._logging = configure_logging(
            log_dir=self.settings.log_path(),
            level=logging.INFO,
            secret_values=self.settings.secret_values,
            json_logs=not self.context.render.plain,
            version=__version__,
        )
        self.panic = PanicController()
        # La política se carga ANTES del agente: su archivo tiene que quedar bajo la protección
        # de rutas, y una política ilegible es un fallo de configuración, no un silencio.
        self.approval_policy = build_approval_policy(self.settings)
        self.handler = KeyHandler(self.panic, enabled=global_hotkey)
        try:
            self._listener_status = self.handler.start()
            self.settings = self._ensure_notrack_key(self.settings)
            self.agent = self._build_agent()
            self._after_agent_built()
        except BaseException:
            self.panic.panic()
            self.handler.stop()
            self._logging.close()
            raise

    # ---------------------------------------------------------------- recursos
    def _load_settings(self) -> Settings:
        return load_settings(
            overrides=self._overrides,
            data_root=self.context.data_root,
        ).with_allow_commands(allow=self.context.allow_commands)

    def _build_agent(self) -> Agent:
        agent = build_agent(
            self.settings,
            self.panic,
            metrics=self.metrics,
            approve=self.approve,
            approval_policy=self.approval_policy,
        )
        agent.approve = self.approve
        return agent

    # ------------------------------------------------------------------ sesión
    def _select_session(self, resume: str | None) -> str:
        """Elige el id: la sesión pedida, la última guardada, o una nueva.

        Con `session_persist=false` el id se genera igual: los contadores de turnos y los
        nombres de archivo no cambian de semántica según la configuración.
        """
        if resume is None:
            return self.sessions.start()
        wanted = resume.strip()
        if not wanted or wanted in {"last", "-"}:
            latest = self.sessions.latest_id()
            if latest is None:
                self.console.print("no hay sesiones guardadas todavía; se empieza una nueva", style="warn")
                return self.sessions.start()
            return latest
        self.sessions.session_id = wanted
        return wanted

    def _after_agent_built(self) -> None:
        """Recupera el historial retomado y lee los adjuntos, con el agente ya construido."""
        if self._resume_requested is not None:
            loaded = self.agent.load_history(self._resume_history())
            if loaded:
                self._startup_notice = (
                    f"sesión {self.session_id} retomada ({loaded} turnos en ventana, tope "
                    f"{self.settings.max_history_messages})",
                    "muted",
                )
            elif self.sessions.path_for(self.session_id).exists():
                self._startup_notice = (f"la sesión {self.session_id} está vacía", "muted")
            else:
                self._startup_notice = (f"no existe la sesión {self.session_id}", "warn")
        elif self.sessions.enabled:
            self._startup_notice = (f"sesión {self.session_id} · recuperable con --resume", "muted")
        self._attachments = self._read_attachments(self._pending_attach)
        self._pending_attach = ()

    def announce(self) -> None:
        """Aviso de arranque: se imprime en el REPL, nunca al construir la sesión."""
        if self._startup_notice is not None:
            text, style = self._startup_notice
            self._startup_notice = None
            self.console.print(text, style=style)

    def _resume_history(self) -> list[dict[str, Any]]:
        """Turnos de la sesión retomada; una sesión inexistente se lee como vacía, no reviente el arranque."""
        try:
            return self.sessions.history(self.session_id)
        except AutonomaError:
            return []

    def _read_attachments(self, paths: Sequence[str]) -> list[str]:
        """Un adjunto es texto ya leído: así el modelo no lo vuelve a pedir con una herramienta."""
        chunks: list[str] = []
        for raw in paths:
            try:
                text = self.agent.fs.read_file(raw)
            except AutonomaError as exc:
                self.console.print(f"  no se pudo adjuntar {raw}: {safe_text(exc.user_message())}", style="err")
                continue
            chunks.append(f"[archivo adjunto: {raw}]\n{text}")
        return chunks

    def _persist_turn(self, prompt: str, answer: str) -> None:
        store: SessionStore | None = getattr(self, "sessions", None)
        if store is None:
            return
        try:
            # Un turno = dos líneas: la pregunta y la respuesta, para poder reproducir la ventana.
            store.append("user", prompt)
            store.append("assistant", answer)
        except AutonomaError as exc:  # una memoria que no escribe no puede matar la conversación
            self.console.print(f"  no se pudo guardar el turno: {safe_text(exc.user_message())}", style="warn")

    def _ensure_notrack_key(self, settings: Settings) -> Settings:
        """Pide la clave una sola vez si hay TTY; nunca la registra como argumento."""
        if settings.has_notrack_key or not sys.stdin.isatty():
            return settings
        self.console.print(
            "No hay NOTRACK_API_KEY. Consíguela en https://notrack.ai/api-keys "
            "y pégala ahora (no se muestra). Vacío = más tarde con /key.",
            style="warn",
        )
        value = _read_secret("NOTRACK_API_KEY: ")
        if not value:
            return settings
        return self._persist_keys(notrack=value, settings=settings)

    def _persist_keys(
        self, *, notrack: str | None = None, brave: str | None = None, settings: Settings | None = None
    ) -> Settings:
        """Guarda en `.env` y aplica como override local; el entorno del proceso no se toca."""
        base = settings if settings is not None else self.settings
        payload = save_api_keys(notrack_api_key=notrack, brave_api_key=brave, env_path=self.context.root / ".env")
        self._overrides.update(dict(payload))
        return base.with_api_keys(notrack=notrack, brave=brave)

    @property
    def listener_status(self) -> Any:
        return self._listener_status

    @property
    def render(self) -> RenderPreferences:
        return self.context.render

    # -------------------------------------------------------------- aprobación
    def _policy_gate(self, name: str, args: Mapping[str, Any]) -> bool | None:
        """Decisión de la política delegada: `True`/`False`, o `None` si no decide (hay que preguntar).

        El orden es deliberado: la política puede negar sin molestar a nadie, pero nunca puede
        inventar un `SI`. La allow-list la escribió un humano fuera del agente, y el agente no
        puede reescribirla porque su ruta está bajo protección del sistema de archivos.
        """
        policy = self.approval_policy
        if policy is None or not policy.enabled:
            return None
        state = self.agent.policy_state
        decision = policy.decide(name, args, state, tty_available=sys.stdin.isatty())
        if decision.allowed and decision.needs_confirm:
            return None  # la regla exige un SI humano pese a estar listada
        policy.record(name, decision.rule, state, allowed=decision.allowed)
        log_event(
            logging.getLogger(__name__),
            logging.INFO if decision.allowed else logging.WARNING,
            "approval.delegated" if decision.allowed else "approval.denied",
            {"tool": name, "rule": decision.rule, "reason": decision.reason, "ops": state.ops},
        )
        if decision.allowed:
            self.console.print(f"  autorizada por la política: {decision.rule}", style="ok")
            return True
        self.console.print(f"  negada por la política: {decision.reason}", style="warn")
        return False

    def approve(self, name: str, args: Mapping[str, Any], user_prompt: str = "") -> bool:
        """Puerta de seguridad: `SI` explícito en una TTY, o la política delegada. Nunca del modelo."""
        decided = self._policy_gate(name, args)
        if decided is not None:
            return decided
        if not sys.stdin.isatty():
            return False
        self.console.print(approval_heading(name, user_prompt))
        self.console.print(json.dumps(dict(args), ensure_ascii=True))
        self.console.print(operation_preview(name, args))
        self.console.print("Puede leer datos privados o modificar tu equipo. Revisa todos los argumentos.")
        was_busy = self.panic.busy
        self.panic.mark_idle()  # pulsar P al escribir la confirmación no debe cancelar la confirmación
        try:
            granted = self.console.input("¿Autorizar esta operación? Escribe SI: ").strip() == _CONFIRMATION_WORD
        except (EOFError, KeyboardInterrupt):
            self._account_approval(name, allowed=False)
            self.panic.panic()
            return False
        finally:
            if was_busy:
                self.panic.mark_busy()
        self._account_approval(name, allowed=granted)
        return granted

    def _approval_mode(self) -> str:
        policy = self.approval_policy
        if policy is None or not policy.enabled:
            return "SI humano en la TTY (sin política delegada)"
        state = self.agent.policy_state
        return (
            f"política delegada: {len(policy.allow)} reglas allow, {len(policy.deny)} deny · "
            f"gastado {state.ops}/{policy.budget.max_ops} ops, {state.writes}/{policy.budget.max_writes} escrituras, "
            f"{state.denials} negaciones"
        )

    def _account_approval(self, name: str, *, allowed: bool) -> None:
        """El `SI` de la TTY también gasta presupuesto si hay política activa: un solo reloj."""
        policy = self.approval_policy
        if policy is None or not policy.enabled:
            return
        state = self.agent.policy_state
        policy.record(name, "(tty)" if allowed else "", state, allowed=allowed)

    def rebuild_agent(self) -> None:
        """Recarga ajustes y recursos cerrando los anteriores (sin fugas de limpieza)."""
        previous = self.agent
        self.settings = self._load_settings()
        previous.close_resources()
        self.agent = self._build_agent()

    # ------------------------------------------------------------------ estado
    def status(self) -> None:
        personal = self.settings.personal_file or "credenciales.txt (no encontrado)"
        ndus_line = mask(self.settings.ndus) if self.settings.has_ndus else "no (opcional)"
        # Indicar de dónde vino la clave
        if self.settings.has_notrack_key:
            if self.settings.personal_status.value == "ok" and self.settings.personal_file:
                key_src = f"sí ({self.settings.personal_file})"
            elif self.settings.dotenv_status.value == "ok":
                key_src = "sí (.env)"
            else:
                key_src = "sí"
        else:
            key_src = "NO → ponla en credenciales.txt o .env"
        lines = [
            f"NoTrack key : {key_src}",
            f"NoTrack URL : {self.settings.notrack_base_url}",
            f"Modelo      : {self.settings.notrack_model}",
            f"NDUS cookie : {ndus_line}",
            f"personal    : {personal} [{self.settings.personal_status.value}]",
            f"shell local : {'habilitado, sin aislamiento' if self.settings.allow_commands else 'deshabilitado'}",
            f"aprobación  : {self._approval_mode()}",
            f"lazo        : {self.settings.supervisor_max_turns} turnos / {self.settings.supervisor_max_minutes:g} min",
            f"Brave key   : {'sí' if self.settings.has_brave_key else 'no (se usará Playwright/HTML)'}",
            f"knowledge   : {self.settings.knowledge_path()}  ({len(list(self.settings.knowledge_path().glob('*.md')))} md)",
            f"sesión      : {self.session_id} ({'en disco' if self.sessions.enabled else 'sin persistencia'})",
            f"pánico P    : {self._listener_status.state.value}",
            f"tarea       : {'en curso' if self.panic.busy else 'idle'}",
            f"trazas      : {current_trace_id() or '(sin turno activo)'}",
            f"turnos       ok={self.metrics.counters().get('turn.ok', 0)}"
            f" cancelados={self.metrics.counters().get('turn.cancelled', 0)}"
            f" fallidos={self.metrics.counters().get('turn.failed', 0)}",
        ]
        notes = self.agent.notes.list_notes(_STATUS_NOTE_LIMIT)
        if notes:
            lines.append("notas recientes:")
            lines.extend(f"  - {path.name}" for path in notes)
        self.console.print(safe_text("\n".join(lines)))

    def metrics_snapshot(self) -> Mapping[str, Any]:
        return self.metrics.snapshot()

    # -------------------------------------------------------------------- turno
    def run_prompt(self, prompt: str) -> int:
        """Devuelve el código de salida del turno (0 ok, no cero ante fallo accionable)."""
        self._last_goal = prompt
        answer, code = self.run_turn(prompt)
        if code == int(ExitCode.SUCCESS):
            self._render_answer(answer)
        return code

    def run_turn(self, prompt: str) -> tuple[str, int]:
        """Un turno suelto: `(respuesta, código)`. Es la pieza que consume el lazo supervisor."""
        if not self.agent.notrack.configured:
            self.console.print("Configura la clave con /key antes de ejecutar prompts.", style="err")
            return "", int(ExitCode.CONFIGURATION)
        if self._attachments:
            prompt = "\n\n".join([*self._attachments, prompt])
            self._attachments = []
        reporter = _ProgressReporter(self.console, self.render)
        reporter.start()
        try:
            answer = self.agent.run(prompt, on_event=reporter.on_event)
            self._persist_turn(prompt, answer)
        except (PanicError, KeyboardInterrupt):
            self.panic.panic()
            reporter.stop()
            self.console.print("Detenido por usuario", style="err")
            return "", int(ExitCode.CANCELLED)
        except AutonomaError as exc:
            reporter.stop()
            self.console.print(safe_text(exc.user_message()), style="err")
            log_event(logging.getLogger(__name__), exc.severity, "cli.turn_error", exc.to_log_fields())
            return "", int(exc.exit_code)
        except Exception as exc:  # noqa: BLE001 — frontera de CLI: registrar y mostrar, no trazar bruto
            reporter.stop()
            log_event(logging.getLogger(__name__), logging.ERROR, "cli.unexpected_error", {"error": type(exc).__name__})
            self.console.print(safe_text("Error interno; revisa logs/autonoma.log con el trace_id."), style="err")
            return "", int(ExitCode.INTERNAL)
        finally:
            reporter.stop()
        # El paso pendiente de este turno es el punto de partida de `/next` y del lazo.
        self._last_next = parse_continuation(answer).instruction
        return answer, int(ExitCode.SUCCESS)

    def run_supervised(self, goal: str, *, budget: SupervisorBudget | None = None) -> SupervisorOutcome:
        """Encadena turnos tras un objetivo hasta DONE / BLOCKED / tope, con informe en disco.

        No hay canal de auto-aprobación aquí: lo que autoriza es la política escrita por el usuario
        (o su `SI` en la TTY). El lazo sólo decide *cuándo* volver a preguntar por el mismo objetivo.
        """
        settings = self.settings
        effective = budget or SupervisorBudget(
            max_turns=settings.supervisor_max_turns,
            max_minutes=settings.supervisor_max_minutes,
            idle_seconds=settings.supervisor_idle_seconds,
            hasta_que_pare=settings.supervisor_hasta_que_pare,
            max_fallos_seguidos=settings.supervisor_tope_fallos,
        )
        directory = settings.supervisor_path()
        #: Reanudación: `estado.json` guarda el objetivo, el turno y el paso pendiente.
        continuation = ""
        turn_offset = 0
        if settings.supervisor_continuar:
            previo = cargar_estado(directory)
            if previo is None:
                self.console.print(
                    "no hay una corrida anterior que continuar: se empieza de cero", style="warn"
                )
            else:
                goal = goal or previo.goal
                continuation = previo.continuation
                turn_offset = max(0, previo.turno)
                self.console.print(
                    f"continuando la corrida anterior desde el turno {turn_offset + 1}"
                    + (f" · paso pendiente: {previo.continuation[:120]}" if previo.continuation else ""),
                    style="muted",
                )
        self._last_goal = goal
        if not self.agent.notrack.configured:
            # Sin modelo no hay lazo posible: se dice una vez, no se gasta el presupuesto en
            # reintentos que van a fallar igual.
            reason = (
                "no hay modelo configurado: escribe la clave con /key o apunta NOTRACK_BASE_URL a "
                "un servidor local (Ollama, LM Studio)"
            )
            self.console.print(f"el lazo no arranca: {reason}", style="err")
            informe = SupervisorReport(
                goal=goal,
                session_id=self.session_id,
                turns=(),
                state="blocked",
                summary=reason,
                stopped_by="modelo sin configurar",
            )
            informe.save(directory)
            return SupervisorOutcome(state="blocked", summary=reason, report=informe)
        if effective.hasta_que_pare:
            self.console.print(
                "lazo supervisor · MODO HASTA QUE YO PARE: sin tope de turnos ni de tiempo "
                f"(se rinde tras {effective.max_fallos_seguidos} fallos seguidos)",
                style="muted",
            )
            self.console.print(
                f"para pararlo: Ctrl+C aquí, o crea el archivo {directory / STOP_FILENAME} desde otra "
                f"terminal; para darle una orden sin pararlo, escribe {directory / STEER_FILENAME}",
                style="muted",
            )
            self.console.print(
                f"registro continuo: {directory / TURNS_FILENAME} · reanudable con --continuar",
                style="muted",
            )
        else:
            self.console.print(
                f"lazo supervisor: {effective.max_turns} turnos · {effective.max_minutes:g} min · "
                f"informe en {directory / TURNS_FILENAME}",
                style="muted",
            )
        if self.approval_policy is not None and self.approval_policy.enabled:
            self.console.print(
                f"política delegada activa ({len(self.approval_policy.allow)} reglas, "
                f"raíces: {', '.join(str(root) for root in self.approval_policy.roots) or '(las de la política)'})",
                style="ok",
            )
        elif not sys.stdin.isatty():
            self.console.print(
                "sin TTY y sin política de aprobación: toda operación local se va a negar. "
                "Configura APPROVAL_POLICY o corre en una terminal interactiva.",
                style="warn",
            )
        if not settings.allow_commands:
            self.console.print(
                "comandos deshabilitados: el lazo no podrá usar la caja de herramientas externa "
                "(arranca con --allow-commands si quieres que lea PDF, descargue o maneje un navegador).",
                style="muted",
            )

        def turn(prompt: str) -> str:
            answer, code = self.run_turn(prompt)
            if code == int(ExitCode.SUCCESS):
                return answer
            if code == int(ExitCode.CANCELLED):
                raise CancelledByUserError("turno cancelado por el usuario")
            raise AutonomaError(f"el turno del lazo terminó con código de salida {code}")

        return run_supervisor(
            goal,
            turn,
            budget=effective,
            session_id=self.session_id,
            report_dir=directory,
            on_event=lambda kind, message: self.console.print(f"  [{kind}] {safe_text(message)}", style="muted"),
            is_cancelled=lambda: self.panic.is_set,
            stop_on_block=settings.supervisor_stop_on_block,
            continuation=continuation,
            turn_offset=turn_offset,
        )

    def report_supervised(self, outcome: SupervisorOutcome) -> None:
        """Cierre legible del lazo: estado, turnos y el resumen que devolvió el modelo."""
        labels = {
            "done": ("objetivo terminado", "ok"),
            "blocked": ("bloqueado: hace falta algo tuyo", "warn"),
            "exhausted": ("presupuesto del lazo agotado", "warn"),
            "cancelled": ("cancelado", "err"),
            "failed": ("el lazo se rindió", "err"),
        }
        label, style = labels.get(outcome.state, ("el lazo terminó", "warn"))
        if STOP_FILENAME in outcome.report.stopped_by:
            label, style = "detenido por el archivo STOP", "ok"
        pico = pico_memoria_mib()
        memoria = f" · pico {pico} MiB" if pico is not None else ""
        self.console.print(f"\nlazo: {label} · {len(outcome.report.turns)} turnos{memoria}", style=style)
        if outcome.summary:
            self._render_answer(outcome.summary)

    def _render_answer(self, answer: str) -> None:
        self.console.print()
        if self.render.use_rich and RICH_AVAILABLE:
            from rich.markdown import Markdown
            from rich.panel import Panel

            self.console.print(Panel(Markdown(safe_text(answer or "")), title=__app_name__, border_style="green"))
            return
        self.console.print(safe_text(answer))

    # ----------------------------------------------------------------- REPL UI
    def handle_command(self, raw: str) -> bool:
        """`True` = seguir en el REPL; `False` = salir. Tabla de comandos explícita."""
        command, _, rest = raw.strip().partition(" ")
        handler = _COMMAND_TABLE.get(command.lower())
        if handler is None:
            self.console.print(safe_text(f"Comando desconocido: {command}. /help"), style="warn")
            return True
        return handler(self, rest)

    # -------------------------------------------------------------- comandos
    def _cmd_exit(self, _rest: str = "") -> bool:
        return False

    def _cmd_help(self, _rest: str = "") -> bool:
        self.console.print(HELP_TEXT)
        return True

    def _cmd_status(self, _rest: str = "") -> bool:
        self.status()
        return True

    def _cmd_sessions(self, _rest: str = "") -> bool:
        infos = self.sessions.list()
        if not infos:
            self.console.print("no hay sesiones guardadas (SESSION_PERSIST puede estar apagado)")
            return True
        for info in infos:
            marker = "*" if info.session_id == self.session_id else " "
            self.console.print(f"{marker} {info.summary()}")
        self.console.print("[dim]/resume <id> retoma una; /forget borra la actual[/dim]")
        return True

    def _cmd_resume(self, _rest: str = "") -> bool:
        self.session_id = self._select_session(_rest)
        loaded = self.agent.load_history(self._resume_history())
        self.console.print(
            f"sesión {self.session_id}: {loaded} turnos recuperados" if loaded else f"sesión {self.session_id} vacía"
        )
        return True

    def _cmd_forget(self, _rest: str = "") -> bool:
        target = _rest.strip() or self.session_id
        if target == self.session_id:
            self._attachments = []
        if self.sessions.delete(target) is None:
            self.console.print(f"no existe la sesión {target}", style="warn")
            return True
        self.agent.reset_history()
        self.session_id = self.sessions.start()
        self.console.print(f"borrada {target}; se empieza la sesión {self.session_id}", style="ok")
        return True

    def _cmd_attach(self, _rest: str = "") -> bool:
        raw = _rest.strip()
        if not raw:
            self.console.print("falta la ruta: /attach informes/nota.md", style="err")
            return True
        chunks = self._read_attachments([raw])
        if chunks and self.agent.record("user", chunks[0]):
            self.console.print("archivo añadido al contexto de esta sesión", style="ok")
        return True

    def _cmd_next(self, _rest: str = "") -> bool:
        raw = _rest.strip()
        goal = raw or self._last_goal
        if not goal:
            self.console.print("no hay objetivo pendiente: /next <objetivo>", style="err")
            return True
        continuation = "" if raw else self._last_next
        self._last_goal = goal
        answer, code = self.run_turn(task_prompt(goal, continuation=continuation))
        if code == int(ExitCode.SUCCESS):
            self._render_answer(answer)
        return True

    def _cmd_policy(self, _rest: str = "") -> bool:
        raw = _rest.strip()
        if raw:
            candidate = Path(raw).expanduser()
            if not candidate.is_absolute():
                candidate = (self.settings.root / candidate).resolve()
            try:
                loaded = load_approval_policy(
                    candidate,
                    default_root=self.settings.workspace,
                    max_bytes=self.settings.approval_policy_max_bytes,
                )
            except (AutonomaError, ValueError, OSError) as exc:
                detail = exc.user_message() if isinstance(exc, AutonomaError) else str(exc)
                self.console.print(f"política inválida: {safe_text(detail)}", style="err")
                return True
            # Reconstruir el agente es lo que pone la nueva política bajo protección de rutas.
            self.approval_policy = loaded
            self.settings = replace(self.settings, approval_policy=str(candidate))
            # `rebuild_agent` ya inyecta la política en el agente nuevo y, con ella, la ruta de la
            # política bajo la protección de rutas del sistema de archivos.
            self.rebuild_agent()
            self.console.print(f"política activada: {candidate}", style="ok")
            return True
        policy = self.approval_policy
        if policy is None:
            self.console.print(
                "sin política delegada: cada operación local exige tu SI en la terminal\n"
                "  para desatender, escribe approval.json y arranca con --approval-policy approval.json",
                style="muted",
            )
            return True
        self.console.print(safe_text(json.dumps(policy.describe(), indent=2, ensure_ascii=False)))
        self.console.print(safe_text(json.dumps(self.agent.policy_state.snapshot(), indent=2)))
        return True

    def _cmd_clear(self, _rest: str = "") -> bool:
        self.agent.reset_history()
        self.console.print("Historial borrado.", style="ok")
        return True

    def _cmd_panic(self, _rest: str = "") -> bool:
        self.panic.panic()
        self.console.print("Detenido por usuario", style="err")
        return True

    def _cmd_kb(self, _rest: str = "") -> bool:
        files = self.agent.notes.list_notes(40)
        if not files:
            self.console.print("knowledge_base vacía", style="muted")
            return True
        for path in files:
            self.console.print(safe_text(f"  {path.name}"))
        return True

    def _cmd_key(self, _rest: str = "") -> bool:
        value = _read_secret("NOTRACK_API_KEY: ")
        if not value:
            return True
        self.settings = self._persist_keys(notrack=value)
        self.rebuild_agent()
        self.console.print("NOTRACK_API_KEY guardada.", style="ok")
        return True

    def _cmd_brave(self, _rest: str = "") -> bool:
        value = _read_secret("BRAVE_API_KEY: ")
        if not value:
            # Vacío = "no ahora": borrar la clave por un Enter sería destructivo.
            self.console.print("BRAVE_API_KEY sin cambios.", style="muted")
            return True
        self.settings = self._persist_keys(brave=value)
        self.rebuild_agent()
        self.console.print("BRAVE_API_KEY actualizada.", style="ok")
        return True

    # ------------------------------------------------------------------- NDUS
    def _cmd_ndus(self, rest: str = "") -> bool:
        """`/ndus`: pegar, autodetectar, importar de archivo, ver o borrar la cookie."""
        raw = (rest or "").strip()
        if not raw:
            return self._ndus_interactive()
        first, _, tail = raw.partition(" ")
        subcommand = first.lower()
        if subcommand == "auto":
            return self._ndus_apply(import_ndus(auto=True), "navegadores de esta máquina")
        if subcommand in {"archivo", "file"}:
            path = Path(tail.strip()).expanduser()
            if not tail.strip():
                self.console.print("falta la ruta: /ndus archivo cookies.txt", style="err")
                return True
            if not path.is_file():
                self.console.print(f"no existe el archivo: {path}", style="err")
                return True
            return self._ndus_apply(import_ndus(file=path), f"archivo {path.name}")
        if subcommand in {"ver", "show"}:
            self._ndus_show()
            return True
        if subcommand in {"borrar", "clear"}:
            return self._ndus_clear()
        return self._ndus_apply(import_ndus(text=raw), "texto pegado")

    def _ndus_interactive(self) -> bool:
        """Sin argumentos: muestra el estado y, si hay TTY, deja pegar sin eco."""
        self._ndus_show()
        self.console.print(COPY_HINTS)
        if not sys.stdin.isatty():
            self.console.print(
                "Aquí no hay terminal interactiva: pásala como argumento (/ndus <valor>) "
                "o con la variable NDUS=valor.",
                style="muted",
            )
            return True
        value = _read_secret("NDUS (no se muestra): ")
        if not value:
            self.console.print("NDUS sin cambios.", style="muted")
            return True
        return self._ndus_apply(import_ndus(text=value), "texto pegado")

    def _ndus_show(self) -> None:
        """Estado de la cookie sin revelarla: enmascarada y con su origen."""
        if self.settings.has_ndus:
            self.console.print(f"NDUS: {mask(self.settings.ndus)}", style="ok")
            return
        self.console.print("NDUS: no configurada (opcional)", style="muted")

    def _ndus_apply(self, result: Any, origin: str) -> bool:
        """Guarda lo importado y lo deja vigente en la sesión sin recargar de disco."""
        if not result.ok:
            self.console.print(f"No se encontró NDUS en {origin}: {result.detail}", style="warn")
            self.console.print(COPY_HINTS)
            for probe in probe_sources()[:6]:
                self.console.print(probe.describe())
            return True
        try:
            path = save_ndus(result.value, personal_path=self._personal_path_or_none(), root=self.context.root)
        except (AutonomaError, OSError) as exc:
            detail = exc.user_message() if isinstance(exc, AutonomaError) else str(exc)
            self.console.print(f"No se pudo guardar la NDUS: {safe_text(detail)}", style="err")
            return True
        self._overrides["NDUS"] = result.value
        self.settings = self.settings.with_ndus(result.value)
        self.console.print(
            f"NDUS guardada en {path.name} · {mask(result.value)} · origen: {result.origin}",
            style="ok",
        )
        return True

    def _ndus_clear(self) -> bool:
        """Borra la cookie del txt personal y avisa si además vive en `.env` (prioridad)."""
        try:
            path = clear_ndus(personal_path=self._personal_path_or_none(), root=self.context.root)
        except (AutonomaError, OSError) as exc:
            detail = exc.user_message() if isinstance(exc, AutonomaError) else str(exc)
            self.console.print(f"No se pudo borrar la NDUS: {safe_text(detail)}", style="err")
            return True
        self._overrides["NDUS"] = ""
        self.settings = self.settings.with_ndus("")
        self.console.print(f"NDUS eliminada de {path.name}.", style="ok")
        if "NDUS" in self.settings.declared_keys:
            self.console.print(
                "Aviso: `.env` también define NDUS y tiene prioridad; bórrala allí para que surta efecto.",
                style="warn",
            )
        return True

    def _personal_path_or_none(self) -> Path | None:
        """El txt personal detectado, o `None` para que `save_ndus` elija dónde crearlo."""
        raw = (self.settings.personal_file or "").strip()
        return Path(raw) if raw else None

    # -------------------------------------------------------------- ciclo final
    def close(self) -> None:
        """Cierra listener, recursos del agente y handlers de log, en ese orden."""
        self.panic.panic()
        self.handler.stop()
        self.agent.close_resources()
        self._logging.close()


CommandHandler = Callable[[Session, str], bool]

_COMMAND_TABLE: Final[Mapping[str, CommandHandler]] = {
    "/quit": Session._cmd_exit,
    "/exit": Session._cmd_exit,
    "/q": Session._cmd_exit,
    "/help": Session._cmd_help,
    "/h": Session._cmd_help,
    "/?": Session._cmd_help,
    "/status": Session._cmd_status,
    "/clear": Session._cmd_clear,
    "/sessions": Session._cmd_sessions,
    "/resume": Session._cmd_resume,
    "/attach": Session._cmd_attach,
    "/forget": Session._cmd_forget,
    "/next": Session._cmd_next,
    "/policy": Session._cmd_policy,
    "/panic": Session._cmd_panic,
    "/kb": Session._cmd_kb,
    "/key": Session._cmd_key,
    "/brave": Session._cmd_brave,
    "/ndus": Session._cmd_ndus,
}


def _read_secret(prompt: str) -> str:
    """Lectura oculta de una clave; EOF o Ctrl+C se tratan como 'no ahora'."""
    try:
        return getpass(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        return ""


def repl(
    once: str | None = None,
    *,
    allow_commands: bool = False,
    global_hotkey: bool = False,
    context: RuntimeContext | None = None,
    resume: str | None = None,
    attach: Sequence[str] = (),
    overrides: Mapping[str, str] | None = None,
    supervise: str | None = None,
) -> int:
    """Bucle interactivo, un solo prompt (`once`) o el lazo supervisor (`supervise`)."""
    _configure_stdio()
    # `Session` aplica el flag de comandos al cargar los ajustes: una sola decisión.
    resolved = context or RuntimeContext.detect(allow_commands=allow_commands)
    session = Session(
        build_console(resolved),
        allow_commands=allow_commands,
        global_hotkey=global_hotkey,
        context=resolved,
        resume=resume,
        attach=attach,
        overrides=overrides,
    )
    print_banner(session.console, session.context, session.settings, session.listener_status)
    session.announce()
    try:
        if supervise:
            return _run_lazo(session, supervise)
        if once:
            return session.run_prompt(once)
        while True:
            try:
                raw = session.console.input("\nautonoma › ")
            except (EOFError, KeyboardInterrupt):
                session.console.print("\nAdiós.")
                break
            line = (raw or "").strip()
            if not line:
                continue
            if line.startswith("/") and not session.handle_command(line):
                session.console.print("Adiós.")
                break
            if not line.startswith("/"):
                session.run_prompt(line)
        return int(ExitCode.SUCCESS)
    finally:
        session.close()


def _run_lazo(session: Session, goal: str) -> int:
    """El lazo se corre aquí y no en `main` para que `session.close()` siempre limpié los procesos."""
    outcome = session.run_supervised(goal)
    session.report_supervised(outcome)
    return outcome.exit_code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="autonoma",
        description="Agente local autónomo: web, archivos y NoTrack.ai. Ctrl+C = cancelar.",
        epilog=(
            "Sin TTY y sin --approval-policy las operaciones locales se deniegan por diseño.\n"
            'Modo desatendido: autonoma --supervise --allow-commands --approval-policy approval.json "objetivo"\n'
            "Interrumpir: Ctrl+C (o tocar pánico); desde otra terminal, toca el archivo supervisor/STOP."
        ),
    )
    parser.add_argument("--doctor", action="store_true", help="diagnóstico local sin API, prompts ni listener")
    parser.add_argument("--json", action="store_true", help="salida JSON de --doctor y --selftest")
    parser.add_argument("--selftest", action="store_true", help="autoensayo del paquete/ejecutable (offline)")
    parser.add_argument("--data-dir", help="directorio local de datos y configuración para esta ejecución")
    parser.add_argument(
        "--global-hotkey",
        action="store_true",
        help="activar P global (puede cancelar mientras escribes en otras apps)",
    )
    parser.add_argument(
        "--allow-commands",
        action="store_true",
        help="habilitar shell local con aprobación por comando (sin aislamiento)",
    )
    parser.add_argument(
        "--supervise",
        action="store_true",
        help="lazo autónomo: encadena turnos sobre el objetivo hasta DONE/BLOCKED sin que pegues prompts",
    )
    parser.add_argument(
        "--turns",
        type=int,
        default=None,
        metavar="N",
        help="cuántos turnos se persigue el objetivo (1-200; --supervise)",
    )
    parser.add_argument(
        "--minutes",
        type=float,
        default=None,
        metavar="N",
        help="ventana de tiempo del lazo en minutos (1-1440; --supervise)",
    )
    parser.add_argument(
        "--goal-file",
        metavar="RUTA",
        help="objetivo leído de un archivo (líneas que empiezan por # se ignoran)",
    )
    parser.add_argument(
        "--repo",
        metavar="RUTA",
        help="repositorio (carpeta) sobre el que trabajar; sin esto, el lazo lo pregunta",
    )
    parser.add_argument(
        "--repos-dir",
        action="append",
        metavar="RUTA",
        help=f"carpeta madre con repos que escanear (repetible; o {ARRANQUE_ENVVAR})",
    )
    parser.add_argument(
        "--elegir",
        action="store_true",
        help="preguntar repositorio y chat aunque no haya terminal interactiva",
    )
    parser.add_argument(
        "--panel",
        nargs="?",
        const="127.0.0.1",
        metavar="DIRECCIÓN",
        help=f"panel para ver, dirigir y parar el lazo desde el navegador ({DEFAULT_PORT}); "
        "con --panel 0.0.0.0 lo ve el celular en tu wifi",
    )
    parser.add_argument(
        "--panel-port",
        type=int,
        default=DEFAULT_PORT,
        metavar="N",
        help=f"puerto del panel (por defecto {DEFAULT_PORT})",
    )
    parser.add_argument(
        "--approval-policy",
        metavar="RUTA",
        help="JSON con el alcance que el usuario delega en modo desatendido (allow-list por herramienta)",
    )
    parser.add_argument(
        "--init-policy",
        nargs="?",
        const="",
        default=None,
        metavar="RUTA",
        help="escribir una plantilla de política de aprobación (por defecto approval.json) y salir",
    )
    parser.add_argument("--force", action="store_true", help="con --init-policy, reemplazar una política existente")
    parser.add_argument(
        "--allow-shell-commands",
        action="store_true",
        help="permitir que la política delegue `command` (texto interpretado por el shell), no sólo argv",
    )
    parser.add_argument(
        "--hasta-que-pare",
        action="store_true",
        help="lazo sin tope de turnos ni de tiempo: sigue hasta DONE, `supervisor/STOP`, Ctrl+C o el freno de fallos",
    )
    parser.add_argument(
        "--tope-fallos",
        type=int,
        default=None,
        metavar="N",
        help="con --hasta-que-pare, cuántos fallos seguidos se toleran antes de rendirse (1-1000)",
    )
    parser.add_argument(
        "--pausa",
        type=float,
        default=None,
        metavar="SEGUNDOS",
        help="respiro entre turnos del lazo (0-3600; --supervise). Con --hasta-que-pare el valor por "
        f"defecto es {PAUSA_LARGA_POR_DEFECTO:g} s; `--pausa 0` lo quita",
    )
    parser.add_argument(
        "--continuar",
        action="store_true",
        help="retomar la corrida anterior del lazo donde se cortó (lee supervisor/estado.json)",
    )
    parser.add_argument(
        "--keep-going-on-block",
        action="store_true",
        help="un BLOCKED no corta el lazo: se trata como turno sin avance",
    )
    parser.add_argument("--plain", action="store_true", help="texto simple, sin paneles ni animaciones")
    parser.add_argument("--reduced-motion", action="store_true", help="progreso estático")
    parser.add_argument("--quiet", action="store_true", help="ocultar previsualizaciones de resultados")
    parser.add_argument(
        "--resume",
        nargs="?",
        const="",
        default=None,
        metavar="ID",
        help="retomar un historial guardado: el último si no das ID",
    )
    parser.add_argument(
        "--attach",
        action="append",
        default=[],
        metavar="RUTA",
        help="meter un archivo en el contexto inicial (repetible)",
    )
    parser.add_argument("--no-persist", action="store_true", help="no escribir sesiones en disco en esta corrida")
    parser.add_argument(
        "--ndus",
        metavar="TEXTO",
        help="importar la cookie NDUS: el valor suelto, `ndus=valor` o la cabecera Cookie completa",
    )
    parser.add_argument(
        "--ndus-file",
        metavar="RUTA",
        help="leer la cookie NDUS de un archivo: `cookies.txt` exportado, cabecera Cookie o valor suelto",
    )
    parser.add_argument(
        "--ndus-auto",
        action="store_true",
        help="buscar la cookie NDUS en los navegadores de esta máquina y guardarla si aparece",
    )
    parser.add_argument(
        "--max-steps", type=int, default=None, metavar="N", help="tope de iteraciones herramienta-modelo (1-200)"
    )
    parser.add_argument("prompt", nargs="*", help="instrucción única (sin REPL)")
    parser.add_argument("--version", action="version", version=f"{__app_name__} {__version__}")
    return parser


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.json and not (args.doctor or args.selftest):
        parser.error("--json requiere --doctor o --selftest")
    if args.doctor and (args.prompt or args.allow_commands or args.global_hotkey):
        parser.error("--doctor no admite prompts, --allow-commands ni --global-hotkey")
    if args.selftest and (args.prompt or args.allow_commands or args.global_hotkey):
        parser.error("--selftest no admite prompts, --allow-commands ni --global-hotkey")
    if not args.supervise:
        extra = [
            flag
            for flag, value in (
                ("--turns", args.turns),
                ("--minutes", args.minutes),
                ("--pausa", args.pausa),
                ("--goal-file", args.goal_file),
                ("--keep-going-on-block", args.keep_going_on_block),
            )
            if value
        ]
        if extra:
            parser.error(f"{' y '.join(extra)} requiere --supervise")
    if args.force and args.init_policy is None:
        parser.error("--force sólo acompaña a --init-policy")
    ndus_flags = [
        flag
        for flag, value in (("--ndus", args.ndus), ("--ndus-file", args.ndus_file), ("--ndus-auto", args.ndus_auto))
        if value
    ]
    if ndus_flags and (args.doctor or args.selftest or args.init_policy is not None):
        parser.error(f"{' y '.join(ndus_flags)} no se mezcla con --doctor, --selftest ni --init-policy")
    if args.init_policy is not None and (args.doctor or args.selftest or args.supervise or args.prompt):
        parser.error("--init-policy no se mezcla con prompts, --doctor, --selftest ni --supervise")
    if args.hasta_que_pare and (args.turns is not None or args.minutes is not None):
        parser.error("--hasta-que-pare no se mezcla con --turns ni --minutes: el lazo no tiene tope")
    if args.tope_fallos is not None and not args.hasta_que_pare:
        parser.error("--tope-fallos requiere --hasta-que-pare")
    if args.pausa is not None and not 0 <= args.pausa <= 3600:
        parser.error("--pausa debe estar entre 0 y 3600 segundos")
    for flag, value in (("--hasta-que-pare", args.hasta_que_pare), ("--continuar", args.continuar)):
        if value and not args.supervise:
            parser.error(f"{flag} requiere --supervise")
    if args.supervise and not (
        " ".join(args.prompt).strip() or (args.goal_file or "").strip() or args.continuar
    ):
        parser.error("--supervise necesita un objetivo: un prompt, --goal-file RUTA o --continuar")
    return args


def build_context(args: argparse.Namespace) -> RuntimeContext:
    """Contexto inmutable a partir de los argumentos: sin mutar `os.environ`."""
    return RuntimeContext.detect(
        data_dir=args.data_dir,
        plain=bool(args.plain),
        reduced_motion=bool(args.reduced_motion),
        quiet=bool(args.quiet),
        allow_commands=bool(args.allow_commands),
        rich_available=RICH_AVAILABLE,
    )


NO_PAUSE_ENV = "AUTONOMA_NO_PAUSE"

#: Respiron por defecto del lazo sin tope (`--hasta-que-pare`): entre turnos el proceso
#: sólo espera al proveedor, así que sin pausa encadena consultas durante horas con la CPU
#: y la red clavadas. Dos segundos no se notan en una corrida larga y sí en el ventilador.
#: Se apaga con `--pausa 0`, y cualquier valor propio (entorno, .env, config.json) manda.
PAUSA_LARGA_POR_DEFECTO: Final[float] = 2.0
_CLAVE_PAUSA: Final[str] = "SUPERVISOR_IDLE_SECONDS"


def pausa_ya_configurada(data_root: Path) -> bool:
    """¿La persona ya fijó la pausa entre turnos? Entonces no se le impone ninguna."""
    if os.environ.get(_CLAVE_PAUSA):
        return True
    try:
        if read_dotenv_file(data_root / ".env").values.get(_CLAVE_PAUSA):
            return True
        if _CLAVE_PAUSA.lower() in load_json_config(data_root / "config.json"):
            return True
    except OSError:  # sin ficheros de configuración no hay valor que respetar
        return False
    return False


def should_pause_on_exit(
    *, frozen: bool, has_prompt: bool, interactive: bool, diagnostic: bool, disabled: bool
) -> bool:
    """Si hay que esperar un Enter antes de cerrar la ventana.

    Doble clic sobre `Autonoma.exe` abre una consola que desaparece con el proceso: un
    error quedaría ilegible. Sólo aplica al binario congelado, en modo interactivo, sin
    prompt en la línea de órdenes y fuera de los modos de diagnóstico (que usan scripts).
    """
    return bool(frozen and interactive and not has_prompt and not diagnostic and not disabled)


def _tty_available() -> bool:
    """Seam de terminal: las pruebas lo sustituyen en lugar de retocar el módulo `sys`."""
    try:
        return bool(sys.stdin.isatty() and sys.stdout.isatty())
    except (ValueError, OSError):  # flujos cerrados (pythonw, consola destruida)
        return False


def maybe_pause_on_exit(args: argparse.Namespace, env: Mapping[str, str] | None = None) -> None:
    """Espera un Enter si quien abre el .exe lo hizo haciendo doble clic (nunca falla)."""
    pause = should_pause_on_exit(
        frozen=bool(getattr(sys, "frozen", False)),
        has_prompt=bool(getattr(args, "prompt", None)),
        interactive=_tty_available(),
        diagnostic=bool(getattr(args, "doctor", False) or getattr(args, "selftest", False)),
        disabled=bool(((os.environ if env is None else env).get(NO_PAUSE_ENV) or "").strip()),
    )
    if not pause:
        return
    try:
        input("\nPulsa Enter para cerrar esta ventana.\n")
    except (EOFError, KeyboardInterrupt, OSError):
        return


def _emit_policy_template(args: argparse.Namespace, context: RuntimeContext) -> None:
    """`--init-policy`: la plantilla la escribe el usuario desde la CLI, nunca el agente."""
    raw = (args.init_policy or "").strip()
    target = Path(raw).expanduser() if raw else context.root / "approval.json"
    try:
        written = write_policy_template(target, overwrite=bool(args.force))
    except (AutonomaError, ValueError, OSError) as exc:
        detail = exc.user_message() if isinstance(exc, AutonomaError) else str(exc)
        print(f"No se pudo escribir la plantilla: {safe_text(detail)}", file=sys.stderr)
        raise SystemExit(int(ExitCode.CONFIGURATION)) from exc
    print(f"plantilla de política escrita en {written}")
    print(
        "ajústala (rutas y comandos concretos) y arranca el lazo así: autonoma --supervise "
        f'--allow-commands --approval-policy {written.name} "termina la tanda pendiente".\n'
        'Para no repetirlo, fíjalo en config.json: "approval_policy": "'
        f'{written.name}".'
    )
    raise SystemExit(int(ExitCode.SUCCESS))


def _import_ndus_from_args(args: argparse.Namespace, context: RuntimeContext) -> bool:
    """Importa la cookie NDUS desde los flags; `False` si no se pudo (es opcional).

    El valor nunca se imprime: sólo su máscara. Tras guardarla, la sesión en curso
    la recibe como override para no tener que releer el disco.
    """
    try:
        result = import_ndus(
            text=args.ndus or None,
            file=Path(args.ndus_file).expanduser() if args.ndus_file else None,
            auto=bool(args.ndus_auto),
        )
    except OSError as exc:
        print(f"No se pudo leer la cookie NDUS: {safe_text(str(exc))}", file=sys.stderr)
        return False
    if not result.ok:
        print(f"NDUS no encontrada: {result.detail}", file=sys.stderr)
        for probe in probe_sources()[:8]:
            print(probe.describe(), file=sys.stderr)
        print(COPY_HINTS, file=sys.stderr)
        return False
    try:
        path = save_ndus(result.value, root=context.root)
    except (AutonomaError, OSError) as exc:
        detail = exc.user_message() if isinstance(exc, AutonomaError) else str(exc)
        print(f"No se pudo guardar la NDUS: {safe_text(detail)}", file=sys.stderr)
        return False
    print(f"NDUS importada desde {result.origin} · {mask(result.value)} → {path}")
    return True


def _supervisor_dir(context: RuntimeContext) -> Path:
    """Dónde vive `supervisor/` en esta ejecución (respeta `SUPERVISOR_DIR`)."""
    raw = os.environ.get("SUPERVISOR_DIR", "").strip()
    candidate = Path(raw).expanduser() if raw else context.root / "supervisor"
    return candidate if candidate.is_absolute() else (context.root / candidate).resolve()


def _abrir_panel(
    args: argparse.Namespace,
    context: RuntimeContext,
    consola: ConsolePort,
    *,
    overrides: Mapping[str, str] | None = None,
) -> PanelWeb:
    """Abre el panel y dice por dónde entrar. Si escucha fuera del equipo, lo advierte."""
    direccion = args.panel or "127.0.0.1"
    ajustes = load_settings(overrides=overrides, data_root=context.data_root, ensure_dirs=False)
    panel = iniciar_panel(
        _supervisor_dir(context),
        direccion=direccion,
        puerto=int(args.panel_port),
        secretos=tuple(ajustes.secret_values),
    )
    urls = panel.urls()
    consola.print(f"panel: {urls[0]}" if urls else "panel: escuchando (sin URL que mostrar)")
    for url in urls[1:]:
        consola.print(f"       {url}")
    if direccion not in {"127.0.0.1", "localhost", "::1"}:
        consola.print(
            "escuchando fuera de este equipo: cualquiera en la red puede intentar entrar "
            "(hace falta el token, y Windows puede pedirte permiso de firewall)",
            style="warn",
        )
    return panel


def main(argv: list[str] | None = None) -> None:
    """Punto de entrada del paquete y del ejecutable congelado."""
    args = parse_args(argv)
    context = build_context(args)
    if args.doctor:
        raise SystemExit(run_diagnostics(as_json=bool(args.json), data_root=context.data_root))
    if args.selftest:
        raise SystemExit(run_selftest(as_json=bool(args.json), data_root=context.data_root))
    if args.init_policy is not None:
        return _emit_policy_template(args, context)
    prompt = " ".join(args.prompt).strip() or None
    if args.ndus or args.ndus_file or args.ndus_auto:
        imported = _import_ndus_from_args(args, context)
        # Sin prompt ni terminal interactiva no hay nada más que hacer: se importa y sale.
        if not prompt and not sys.stdin.isatty():
            maybe_pause_on_exit(args)
            raise SystemExit(int(ExitCode.SUCCESS) if imported else int(ExitCode.CONFIGURATION))
    overrides: dict[str, str] = {}
    if args.no_persist:
        overrides["SESSION_PERSIST"] = "false"
    if args.max_steps is not None:
        overrides["MAX_TOOL_ITERATIONS"] = str(args.max_steps)
    if args.approval_policy:
        overrides["APPROVAL_POLICY"] = str(args.approval_policy)
    if args.allow_shell_commands:
        overrides["ALLOW_SHELL_COMMANDS"] = "true"
    if args.turns is not None:
        overrides["SUPERVISOR_MAX_TURNS"] = str(args.turns)
    if args.minutes is not None:
        overrides["SUPERVISOR_MAX_MINUTES"] = str(args.minutes)
    if args.keep_going_on_block:
        overrides["SUPERVISOR_STOP_ON_BLOCK"] = "false"
    if args.hasta_que_pare:
        overrides["SUPERVISOR_HASTA_QUE_PARE"] = "true"
    if args.continuar:
        overrides["SUPERVISOR_CONTINUAR"] = "true"
    if args.tope_fallos is not None:
        overrides["SUPERVISOR_TOPE_FALLOS"] = str(args.tope_fallos)
    if args.pausa is not None:
        overrides[_CLAVE_PAUSA] = str(args.pausa)
    elif args.hasta_que_pare and not pausa_ya_configurada(context.data_root.path):
        overrides[_CLAVE_PAUSA] = str(PAUSA_LARGA_POR_DEFECTO)
    if args.goal_file:
        overrides["SUPERVISOR_GOAL_FILE"] = str(args.goal_file)

    # Arranque asistido: en qué repositorio y con qué chat, antes de irse a segundo plano.
    # Sólo pregunta si va a trabajar solo (`--supervise`): para un prompt suelto sería ruido.
    resume = args.resume
    consola = build_console(context)
    elegido: Path | None = Path(args.repo).expanduser() if args.repo else None
    if elegido is None and args.supervise and (sys.stdin.isatty() or args.elegir):
        _configure_stdio()
        previos = load_settings(overrides=overrides, data_root=context.data_root, ensure_dirs=False)
        chats: tuple[SessionInfo, ...] = ()
        if previos.session_persist:
            chats = SessionStore(previos.sessions_path()).list()
        arranque = resolver_arranque(
            consola,
            registro=context.root / REGISTRY_FILENAME,
            bases=bases_del_entorno(extra=[Path(item).expanduser() for item in args.repos_dir or ()]),
            chats=chats,
            interactivo=True,
        )
        elegido = arranque.repo
        if arranque.chat and resume is None:
            resume = arranque.chat
    if elegido is not None:
        destino = elegido.expanduser().resolve()
        if not destino.is_dir():
            print(f"No existe la carpeta '{destino}'.", file=sys.stderr)
            raise SystemExit(int(ExitCode.CONFIGURATION))
        # Desde aquí, las rutas relativas del agente y las raíces de la política son el repo.
        aplicar_arranque(Arranque(repo=destino, chat=resume, preguntado=True), overrides, consola)

    supervise_goal: str | None = None
    if args.supervise:
        try:
            if prompt or (args.goal_file or "").strip():
                supervise_goal = prompt or goal_from_file(Path(args.goal_file).expanduser())
            else:
                # `--continuar` sin objetivo: el objetivo es el de la corrida anterior.
                previo = cargar_estado(_supervisor_dir(context))
                if previo is None:
                    print(
                        "No hay una corrida anterior que continuar: falta "
                        f"{_supervisor_dir(context) / 'estado.json'}. "
                        "Da un objetivo o usa --goal-file RUTA.",
                        file=sys.stderr,
                    )
                    raise SystemExit(int(ExitCode.CONFIGURATION))
                supervise_goal = previo.goal
        except (AutonomaError, OSError, ValueError) as exc:
            detail = exc.user_message() if isinstance(exc, AutonomaError) else str(exc)
            print(f"No se pudo leer el objetivo: {safe_text(detail)}", file=sys.stderr)
            raise SystemExit(int(ExitCode.CONFIGURATION)) from exc
    panel: PanelWeb | None = None
    if args.panel:
        panel = _abrir_panel(args, context, consola, overrides=overrides)
    try:
        code = repl(
            once=prompt,
            allow_commands=bool(args.allow_commands),
            global_hotkey=bool(args.global_hotkey),
            context=context,
            resume=resume,
            attach=tuple(args.attach),
            overrides=overrides,
            supervise=supervise_goal,
        )
    except (AutonomaError, ValueError, OSError) as exc:
        detail = exc.user_message() if isinstance(exc, AutonomaError) else str(exc)
        print(f"No se pudo iniciar Autonoma: {safe_text(detail)}", file=sys.stderr)
        code = int(exc.exit_code) if isinstance(exc, AutonomaError) else int(ExitCode.CONFIGURATION)
    finally:
        # El panel vive en su propio hilo: si el lazo termina, nadie debe quedarse escuchando.
        if panel is not None:
            panel.stop()
    maybe_pause_on_exit(args)
    raise SystemExit(code)


if __name__ == "__main__":
    main()
