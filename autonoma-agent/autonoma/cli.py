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
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
from collections.abc import Callable, Mapping, Sequence
from getpass import getpass
from typing import Any, Final

from autonoma import __app_name__, __version__
from autonoma.agent import Agent, build_agent
from autonoma.config import Settings, load_settings, save_api_keys
from autonoma.diagnostics import run_diagnostics, run_selftest
from autonoma.errors import AutonomaError, ExitCode
from autonoma.key_handler import KeyHandler, ListenerStatus, PanicController, PanicError
from autonoma.observability import MetricsRegistry, configure_logging, current_trace_id, log_event
from autonoma.ports import ConsolePort, EventKind
from autonoma.presentation import approval_heading, operation_preview, safe_text
from autonoma.runtime import RenderPreferences, RuntimeContext
from autonoma.sessions import SessionStore

__all__ = ["HELP_TEXT", "Session", "build_console", "main", "parse_args", "repl"]

try:  # la dependencia es opcional por diseño: sin Rich se usa texto plano
    from rich.console import Console
    from rich.live import Live
    from rich.markdown import Markdown
    from rich.panel import Panel
    from rich.text import Text
    from rich.theme import Theme

    _RICH_IMPORT_ERROR: ImportError | None = None
except ImportError as exc:  # pragma: no cover - depende del entorno
    Console = Live = Markdown = Panel = Text = Theme = object  # type: ignore[assignment,misc]
    _RICH_IMPORT_ERROR = exc

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
  /status            claves, knowledge_base, pánico y métricas
  /kb                listar notas de knowledge_base
  /clear             borrar historial de conversación
  /sessions          sesiones guardadas (para --resume)
  /resume [id]       retomar el historial de una sesión
  /attach <ruta>     meter un archivo en el contexto de esta sesión
  /forget            borrar el historial visible Y la sesión en disco
  /panic             activar cancelación entre tareas

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
    key_line = "NoTrack: clave configurada" if settings.has_notrack_key else "NoTrack: FALTA NOTRACK_API_KEY  →  /key"
    return f"{BANNER}\n{key_line}\n{status.describe()}\nEscribe un prompt o /help"


def print_banner(console: ConsolePort, context: RuntimeContext, settings: Settings, status: ListenerStatus) -> None:
    body = banner_body(settings, status)
    if context.use_rich and RICH_AVAILABLE:
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
        agent = build_agent(self.settings, self.panic, metrics=self.metrics, approve=self.approve)
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
    def approve(self, name: str, args: Mapping[str, Any], user_prompt: str = "") -> bool:
        """Puerta de seguridad: requiere `SI` explícito en una TTY, nunca del modelo."""
        if not sys.stdin.isatty():
            return False
        self.console.print(approval_heading(name, user_prompt))
        self.console.print(json.dumps(dict(args), ensure_ascii=True))
        self.console.print(operation_preview(name, args))
        self.console.print("Puede leer datos privados o modificar tu equipo. Revisa todos los argumentos.")
        was_busy = self.panic.busy
        self.panic.mark_idle()  # pulsar P al escribir la confirmación no debe cancelar la confirmación
        try:
            return self.console.input("¿Autorizar esta operación? Escribe SI: ").strip() == _CONFIRMATION_WORD
        except (EOFError, KeyboardInterrupt):
            self.panic.panic()
            return False
        finally:
            if was_busy:
                self.panic.mark_busy()

    def rebuild_agent(self) -> None:
        """Recarga ajustes y recursos cerrando los anteriores (sin fugas de limpieza)."""
        previous = self.agent
        self.settings = self._load_settings()
        previous.close_resources()
        self.agent = self._build_agent()

    # ------------------------------------------------------------------ estado
    def status(self) -> None:
        lines = [
            f"NoTrack key : {'sí' if self.settings.has_notrack_key else 'NO'}",
            f"NoTrack URL : {self.settings.notrack_base_url}",
            f"Modelo      : {self.settings.notrack_model}",
            f"shell local : {'habilitado, sin aislamiento' if self.settings.allow_commands else 'deshabilitado'}",
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
        if not self.agent.notrack.configured:
            self.console.print("Configura la clave con /key antes de ejecutar prompts.", style="err")
            return int(ExitCode.CONFIGURATION)
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
            return int(ExitCode.CANCELLED)
        except AutonomaError as exc:
            reporter.stop()
            self.console.print(safe_text(exc.user_message()), style="err")
            log_event(logging.getLogger(__name__), exc.severity, "cli.turn_error", exc.to_log_fields())
            return int(exc.exit_code)
        except Exception as exc:  # noqa: BLE001 — frontera de CLI: registrar y mostrar, no trazar bruto
            reporter.stop()
            log_event(logging.getLogger(__name__), logging.ERROR, "cli.unexpected_error", {"error": type(exc).__name__})
            self.console.print(safe_text("Error interno; revisa logs/autonoma.log con el trace_id."), style="err")
            return int(ExitCode.INTERNAL)
        finally:
            reporter.stop()
        self._render_answer(answer)
        return int(ExitCode.SUCCESS)

    def _render_answer(self, answer: str) -> None:
        self.console.print()
        if self.render.use_rich and RICH_AVAILABLE:
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
    "/panic": Session._cmd_panic,
    "/kb": Session._cmd_kb,
    "/key": Session._cmd_key,
    "/brave": Session._cmd_brave,
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
) -> int:
    """Bucle interactivo (o un solo prompt con `once`) con limpieza garantizada."""
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="autonoma",
        description="Agente local autónomo: web, archivos y NoTrack.ai. Ctrl+C = cancelar.",
        epilog="Sin TTY interactiva las operaciones locales se deniegan por diseño.",
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


def main(argv: list[str] | None = None) -> None:
    """Punto de entrada del paquete y del ejecutable congelado."""
    args = parse_args(argv)
    context = build_context(args)
    if args.doctor:
        raise SystemExit(run_diagnostics(as_json=bool(args.json), data_root=context.data_root))
    if args.selftest:
        raise SystemExit(run_selftest(as_json=bool(args.json), data_root=context.data_root))
    prompt = " ".join(args.prompt).strip() or None
    overrides: dict[str, str] = {}
    if args.no_persist:
        overrides["SESSION_PERSIST"] = "false"
    if args.max_steps is not None:
        overrides["MAX_TOOL_ITERATIONS"] = str(args.max_steps)
    try:
        code = repl(
            once=prompt,
            allow_commands=bool(args.allow_commands),
            global_hotkey=bool(args.global_hotkey),
            context=context,
            resume=args.resume,
            attach=tuple(args.attach),
            overrides=overrides,
        )
    except (AutonomaError, ValueError, OSError) as exc:
        detail = exc.user_message() if isinstance(exc, AutonomaError) else str(exc)
        print(f"No se pudo iniciar Autonoma: {safe_text(detail)}", file=sys.stderr)
        code = int(exc.exit_code) if isinstance(exc, AutonomaError) else int(ExitCode.CONFIGURATION)
    maybe_pause_on_exit(args)
    raise SystemExit(code)


if __name__ == "__main__":
    main()
