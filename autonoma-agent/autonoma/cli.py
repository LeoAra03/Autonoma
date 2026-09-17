"""Interfaz de línea de comandos de Autonoma."""

from __future__ import annotations

import argparse
import os
import json
from logging.handlers import RotatingFileHandler
import logging
import sys
import threading
from getpass import getpass
from pathlib import Path
from typing import Any

from autonoma import __app_name__, __version__
from autonoma.agent import build_agent
from autonoma.config import load_settings, save_api_keys
from autonoma.key_handler import KeyHandler, PanicController, PanicError
from autonoma.notrack_client import NoTrackError
from autonoma.presentation import safe_text, operation_preview
from autonoma.diagnostics import run_diagnostics

try:
    from rich.console import Console
    from rich.markdown import Markdown
    from rich.panel import Panel
    from rich.theme import Theme

    RICH = not bool(os.environ.get("AUTONOMA_PLAIN"))
except ImportError:  # pragma: no cover
    RICH = False
    Console = None  # type: ignore[misc, assignment]


HELP_TEXT = """
Comandos
  /help              esta ayuda
  /quit  /exit       salir
  /key               pegar NOTRACK_API_KEY (no se muestra)
  /brave             pegar BRAVE_API_KEY (opcional)
  /status            claves, knowledge_base, pánico
  /kb                listar notas de knowledge_base
  /clear             borrar historial de conversación
  /panic             activar cancelación entre tareas

Pánico
  P global es opcional: inicia con --global-hotkey si lo necesitas.
  Ctrl+C también cancela la tarea. /panic solo se lee entre tareas.
  La cancelación es cooperativa; ciertas operaciones pueden tardar en terminar.
""".strip()

BANNER = f"{__app_name__} v{__version__}  ·  agente local  ·  Ctrl+C = cancelar"


class _PlainConsole:
    def print(self, *args: Any, **kwargs: Any) -> None:
        kwargs.pop("style", None)
        kwargs.pop("highlight", None)
        text = " ".join(str(a) for a in args)
        print(text)

    def input(self, prompt: str = "") -> str:
        return input(prompt)


def _console() -> Any:
    if RICH:
        theme = Theme(
            {
                "ok": "green",
                "warn": "yellow",
                "err": "bold red",
                "muted": "dim",
                "accent": "cyan",
            }
        )
        return Console(theme=theme, highlight=False, markup=False, no_color="NO_COLOR" in os.environ)
    return _PlainConsole()


def _setup_logging(log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    logfile = log_dir / "autonoma.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            RotatingFileHandler(logfile, maxBytes=2_000_000, backupCount=3, encoding="utf-8"),
        ],
    )


def _configure_stdio() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass


def _print_banner(console: Any, key_ok: bool, listener_ok: bool, listener_err: str | None) -> None:
    panic_line = "Listener P: activo" if listener_ok else f"Listener P: inactivo — usa Ctrl+C. {listener_err or ''}"
    key_line = "NoTrack: clave configurada" if key_ok else "NoTrack: FALTA NOTRACK_API_KEY  →  /key"
    body = f"{BANNER}\n{key_line}\n{panic_line}\nEscribe un prompt o /help"
    if RICH:
        console.print(Panel(body, title=__app_name__, border_style="cyan"))
    else:
        console.print("=" * 60)
        console.print(body)
        console.print("=" * 60)


def _need_key(console: Any, settings: Any) -> None:
    if settings.has_notrack_key or not sys.stdin.isatty():
        return
    console.print(
        "No hay NOTRACK_API_KEY. Consíguela en https://notrack.ai/api-keys "
        "y pégala ahora (no se muestra). Vacío = más tarde con /key.",
        style="warn",
    )
    try:
        value = getpass("NOTRACK_API_KEY: ").strip()
    except (EOFError, KeyboardInterrupt):
        return
    if value:
        save_api_keys(notrack_api_key=value)
        settings.notrack_api_key = value
        console.print("Clave guardada en .env", style="ok")


class Session:
    def __init__(self, console: Any, allow_commands: bool = False, global_hotkey: bool = False) -> None:
        self.console = console
        self.settings = load_settings()
        self.settings.allow_commands = allow_commands
        _setup_logging(self.settings.log_path())
        self.panic = PanicController()
        self.handler = KeyHandler(self.panic)
        self.listener_ok = False
        try:
            if global_hotkey:
                self.listener_ok = self.handler.start()
            else:
                self.handler.last_error = "Desactivado por defecto; --global-hotkey para habilitar."
            _need_key(console, self.settings)
            self.agent = build_agent(self.settings, self.panic)
            self.agent.approve = self.approve
        except BaseException:
            self.panic.panic()
            self.handler.stop()
            raise

    def approve(self, name: str, args: dict[str, Any]) -> bool:
        if not sys.stdin.isatty():
            return False
        self.console.print("Operación local: " + name + "\n" + json.dumps(args, ensure_ascii=True))
        self.console.print(operation_preview(name, args))
        self.console.print("Puede leer datos privados o modificar tu equipo. Revisa todos los argumentos.")
        was_busy = self.panic.busy
        self.panic.mark_idle()  # P al escribir una confirmación no debe cancelarla.
        try:
            return self.console.input("¿Autorizar esta operación? Escribe SI: ").strip() == "SI"
        except (EOFError, KeyboardInterrupt):
            self.panic.panic()
            return False
        finally:
            if was_busy:
                self.panic.mark_busy()

    def rebuild_agent(self) -> None:
        allow_commands = self.settings.allow_commands
        self.settings = load_settings()
        self.settings.allow_commands = allow_commands
        self.agent.notrack.close()
        self.agent.search.shutdown()
        self.agent.fs.kill_all()
        self.panic.unregister_cleanup(self.agent.notrack.close)
        self.panic.unregister_cleanup(self.agent.search.close)
        self.panic.unregister_cleanup(self.agent.fs.kill_all)
        self.agent = build_agent(self.settings, self.panic)

        self.agent.approve = self.approve

    def status(self) -> None:
        s = self.settings
        kb = list(self.agent.search.list_notes(8))
        lines = [
            f"NoTrack key : {'sí' if s.has_notrack_key else 'NO'}",
            f"NoTrack URL : {s.notrack_base_url}",
            f"Modelo      : {s.notrack_model}",
            f"shell local : {'habilitado, sin aislamiento' if s.allow_commands else 'deshabilitado'}",
            f"Brave key   : {'sí' if s.has_brave_key else 'no (se usará Playwright/HTML)'}",
            f"knowledge   : {s.knowledge_path()}  ({len(list(s.knowledge_path().glob('*.md')))} md)",
            f"pánico P    : {'activo' if self.listener_ok else 'inactivo'}",
            f"tarea       : {'en curso' if self.panic.busy else 'idle'}",
        ]
        if kb:
            lines.append("notas recientes:")
            lines.extend(f"  - {p.name}" for p in kb)
        self.console.print(safe_text("\n".join(lines)))

    def run_prompt(self, prompt: str) -> int:
        if not self.agent.notrack.configured:
            self.console.print("Configura la clave con /key antes de ejecutar prompts.", style="err")
            return 1

        stop_spinner = threading.Event()
        current = {"msg": "pensando…"}

        def spin() -> None:
            if not RICH or os.environ.get("AUTONOMA_REDUCED_MOTION"):
                return
            frames = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
            i = 0
            from rich.live import Live
            from rich.text import Text

            try:
                with Live(console=self.console, refresh_per_second=12, transient=True) as live:
                    while not stop_spinner.is_set():
                        frame = frames[i % len(frames)]
                        live.update(Text(f"{frame}  {current['msg']}", style="accent"))
                        i += 1
                        stop_spinner.wait(0.08)
            except Exception:  # noqa: BLE001
                pass

        spinner_thread: threading.Thread | None = None
        if RICH:
            spinner_thread = threading.Thread(target=spin, daemon=True)
            spinner_thread.start()

        def on_event(kind: str, message: str) -> None:
            message = safe_text(message)
            if kind in {"think", "tool"}:
                current["msg"] = message.replace("\n", " ")[:120]
            if kind == "tool":
                stop_spinner.set()
                if spinner_thread is not None:
                    spinner_thread.join(timeout=1)
                self.console.print(f"▸ herramienta  {message}", style="accent")
            elif kind == "think" and (stop_spinner.is_set() or not RICH or os.environ.get("AUTONOMA_REDUCED_MOTION")):
                self.console.print(message)
            elif kind == "timing":
                self.console.print(message, style="muted")
            elif kind == "tool_result" and not os.environ.get("AUTONOMA_QUIET"):
                preview = message.replace("\n", " ")[:240]
                self.console.print(f"  ↳ {preview}", style="muted")
            elif kind == "panic":
                stop_spinner.set()
                self.console.print("Detenido por usuario", style="err")

        try:
            answer = self.agent.run(prompt, on_event=on_event)
        except (PanicError, KeyboardInterrupt):
            self.panic.panic()
            stop_spinner.set()
            self.console.print("Detenido por usuario", style="err")
            return 1
        except NoTrackError as exc:
            stop_spinner.set()
            self.console.print(safe_text(f"NoTrack: {exc}"), style="err")
            return 1
        except Exception as exc:  # noqa: BLE001
            stop_spinner.set()
            logging.exception("Fallo en el agente")
            self.console.print(safe_text(f"Error: {exc}"), style="err")
            return 1
        finally:
            stop_spinner.set()
            if spinner_thread is not None:
                spinner_thread.join(timeout=1.0)

        self.console.print()
        if RICH:
            self.console.print(Panel(Markdown(safe_text(answer or "")), title="Autonoma", border_style="green"))
        else:
            self.console.print(safe_text(answer))

        return 0

    def handle_command(self, raw: str) -> bool:
        """True = seguir el REPL, False = salir."""
        cmd, _, rest = raw.strip().partition(" ")
        cmd = cmd.lower()
        if cmd in {"/quit", "/exit", "/q"}:
            return False
        if cmd in {"/help", "/h", "/?"}:
            self.console.print(HELP_TEXT)
            return True
        if cmd == "/status":
            self.status()
            return True
        if cmd == "/clear":
            self.agent.reset_history()
            self.console.print("Historial borrado.", style="ok")
            return True
        if cmd == "/panic":
            self.panic.panic()
            self.console.print("Detenido por usuario", style="err")
            return True
        if cmd == "/kb":
            files = self.agent.search.list_notes(40)
            if not files:
                self.console.print("knowledge_base vacía", style="muted")
            else:
                for p in files:
                    self.console.print(safe_text(f"  {p.name}"))
            return True
        if cmd == "/key":
            try:
                value = getpass("NOTRACK_API_KEY: ").strip()
            except (EOFError, KeyboardInterrupt):
                return True
            if value:
                save_api_keys(notrack_api_key=value)
                self.settings.notrack_api_key = value
                self.rebuild_agent()
                self.console.print("NOTRACK_API_KEY guardada.", style="ok")
            return True
        if cmd == "/brave":
            try:
                value = getpass("BRAVE_API_KEY: ").strip()
            except (EOFError, KeyboardInterrupt):
                return True
            save_api_keys(brave_api_key=value)
            self.settings.brave_api_key = value
            self.rebuild_agent()
            self.console.print("BRAVE_API_KEY actualizada.", style="ok")
            return True
        self.console.print(safe_text(f"Comando desconocido: {cmd}. /help"), style="warn")
        return True

    def close(self) -> None:
        try:
            self.panic.panic()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.handler.stop()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.agent.notrack.close()
            self.agent.search.shutdown()
            self.agent.fs.kill_all()
        except Exception:  # noqa: BLE001
            pass


def repl(once: str | None = None, *, allow_commands: bool = False, global_hotkey: bool = False) -> int:
    _configure_stdio()
    console = _console()
    session = Session(console, allow_commands=allow_commands, global_hotkey=global_hotkey)
    _print_banner(
        console,
        key_ok=session.settings.has_notrack_key,
        listener_ok=session.listener_ok,
        listener_err=session.handler.last_error,
    )
    try:
        if once:
            return session.run_prompt(once)
        while True:
            try:
                raw = console.input("\nautonoma › ")
            except (EOFError, KeyboardInterrupt):
                console.print("\nAdiós.")
                break
            raw = (raw or "").strip()
            if not raw:
                continue
            if raw.startswith("/"):
                if not session.handle_command(raw):
                    console.print("Adiós.")
                    break
                continue
            session.run_prompt(raw)
        return 0
    finally:
        session.close()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="autonoma",
        description="Agente local autónomo: web, archivos y NoTrack.ai. Ctrl+C = cancelar.",
    )
    parser.add_argument("--doctor", action="store_true", help="Diagnóstico local sin API, prompts ni listener")
    parser.add_argument("--json", action="store_true", help="Salida JSON de --doctor")
    parser.add_argument("--data-dir", help="Directorio local de datos/configuración de esta ejecución")
    parser.add_argument("--global-hotkey", action="store_true", help="Activar P global (puede cancelar al escribir en otras apps)")
    parser.add_argument("--allow-commands", action="store_true", help="Habilitar shell local con aprobación por comando (sin aislamiento)")
    parser.add_argument("--plain", action="store_true", help="Texto simple, sin paneles ni animaciones")
    parser.add_argument("--reduced-motion", action="store_true", help="Progreso estático")
    parser.add_argument("--quiet", action="store_true", help="Ocultar previsualizaciones de resultados")
    parser.add_argument("prompt", nargs="*", help="Instrucción única (sin REPL)")
    parser.add_argument("--version", action="version", version=f"{__app_name__} {__version__}")
    args = parser.parse_args(argv)
    if args.json and not args.doctor:
        parser.error("--json requiere --doctor")
    if args.doctor and (args.prompt or args.allow_commands or args.global_hotkey):
        parser.error("--doctor no admite prompts, --allow-commands ni --global-hotkey")
    return args


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    global RICH
    if args.data_dir:
        os.environ["AUTONOMA_HOME"] = str(Path(args.data_dir).expanduser().resolve())
    if args.doctor:
        raise SystemExit(run_diagnostics(as_json=args.json))
    if args.plain:
        RICH = False
    if args.reduced_motion:
        os.environ["AUTONOMA_REDUCED_MOTION"] = "1"
    if args.quiet:
        os.environ["AUTONOMA_QUIET"] = "1"
    prompt = " ".join(args.prompt).strip() or None
    try:
        code = repl(once=prompt, allow_commands=args.allow_commands, global_hotkey=args.global_hotkey)
    except (ValueError, OSError) as exc:
        print(f"No se pudo iniciar Autonoma: {exc}", file=sys.stderr)
        code = 1
    raise SystemExit(code)


if __name__ == "__main__":
    main()
