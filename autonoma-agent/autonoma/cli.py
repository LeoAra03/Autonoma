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
import sys
import threading
from collections.abc import Callable, Mapping
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

    def __init__(
        self,
        console: ConsolePort,
        *,
        allow_commands: bool = False,
        global_hotkey: bool = False,
        context: RuntimeContext | None = None,
    ) -> None:
        self.console = console
        self.context = (context or RuntimeContext.detect()).with_allow_commands(allow=allow_commands)
        self._overrides: dict[str, str] = {}
        self.settings = self._load_settings()
        self.metrics = MetricsRegistry()
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

    def _persist_keys(self, *, notrack: str | None = None, brave: str | None = None, settings: Settings | None = None) -> Settings:
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
        reporter = _ProgressReporter(self.console, self.render)
        reporter.start()
        try:
            answer = self.agent.run(prompt, on_event=reporter.on_event)
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
) -> int:
    """Bucle interactivo (o un solo prompt con `once`) con limpieza garantizada."""
    _configure_stdio()
    # `Session` aplica el flag de comandos al cargar los ajustes: una sola decisión.
    resolved = context or RuntimeContext.detect(allow_commands=allow_commands)
    session = Session(build_console(resolved), allow_commands=allow_commands, global_hotkey=global_hotkey, context=resolved)
    print_banner(session.console, session.context, session.settings, session.listener_status)
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


def main(argv: list[str] | None = None) -> None:
    """Punto de entrada del paquete y del ejecutable congelado."""
    args = parse_args(argv)
    context = build_context(args)
    if args.doctor:
        raise SystemExit(run_diagnostics(as_json=bool(args.json), data_root=context.data_root))
    if args.selftest:
        raise SystemExit(run_selftest(as_json=bool(args.json), data_root=context.data_root))
    prompt = " ".join(args.prompt).strip() or None
    try:
        code = repl(
            once=prompt,
            allow_commands=bool(args.allow_commands),
            global_hotkey=bool(args.global_hotkey),
            context=context,
        )
    except (AutonomaError, ValueError, OSError) as exc:
        detail = exc.user_message() if isinstance(exc, AutonomaError) else str(exc)
        print(f"No se pudo iniciar Autonoma: {safe_text(detail)}", file=sys.stderr)
        code = int(exc.exit_code) if isinstance(exc, AutonomaError) else int(ExitCode.CONFIGURATION)
    raise SystemExit(code)


if __name__ == "__main__":
    main()
