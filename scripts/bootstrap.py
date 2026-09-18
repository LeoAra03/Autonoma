#!/usr/bin/env python3
"""Instalador y lanzador de un solo comando para Autonoma.

No depende de npm, de pipx ni de nada instalado: sólo la biblioteca estándar de Python.
Es lo que ejecutan `npm start`, `Run-Autonoma.bat`, `run-autonoma.sh` y `python -m` a mano.

Comandos:
    setup      crea el venv, instala el paquete y prepara `.env` (idempotente y rápido)
    run        lanza el agente; si hay `.env` con clave, ya trabaja (`run "tu prompt"`)
    key        guarda (o reemplaza) la NOTRACK_API_KEY en `.env`
    status     qué python, qué venv, versión, estado de la clave y raíz de datos
    doctor / selftest / test / lint / types / bench / build / clean

Diseño: cada paso es una función pequeña y comprobable; `_run` acepta `--dry-run` para
que se pueda probar sin tocar el disco. Los fallos se reportan como `BootstrapError` con
el "qué hacer" en el propio mensaje (nunca una traceback).
"""

from __future__ import annotations

import argparse
import contextlib
import getpass
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path

MIN_PYTHON: tuple[int, int] = (3, 10)
ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "autonoma-agent"
ENV_TEMPLATE = PACKAGE / ".env.example"
DATA_ENV = PACKAGE / ".env"
KEY_NAME = "NOTRACK_API_KEY"
BRAVE_KEY_NAME = "BRAVE_API_KEY"
PLACEHOLDERS = frozenset({"", "sk-notrack-pega-aqui-tu-clave", "pega-aqui", "tu-clave"})
PYTHON_DOWNLOAD = "https://www.python.org/downloads/"
DEV_EXTRAS = ("test", "check")  # `test`/`lint`/`types`/`bench` las necesitan; `run` no
STAMP_NAME = "autonoma-install.json"
WINDOWS_LAUNCHER = ("py.exe", "py")
PYTHON_CANDIDATES = ("python3", "python")
PROBE = "import sys;v=sys.version_info;print('%%d.%%d.%%d' %% v[:3]);sys.exit(0 if v[:2]>=(%d,%d) else 1)"


class BootstrapError(RuntimeError):
    """Error del instalador con mensaje accionable para una persona, no para un log."""


def say(step: str, message: str) -> None:
    """Salida compacta y estable: `[1/2] texto` (el orden ayuda a ver dónde se paró)."""
    print(f"[{step}] {message}", flush=True)


def _run(
    argv: Sequence[str],
    *,
    dry_run: bool = False,
    capture: bool = False,
    timeout: float = 900.0,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Ejecuta un comando; con `dry_run` sólo lo devuelve sin correrlo.

    Un fallo ajeno nunca se propaga crudo: se envuelve en `BootstrapError` con las
    últimas líneas útiles del proceso hijo.
    """
    printable = " ".join(shlex.quote(str(part)) for part in argv)
    if dry_run:
        print(f"    (dry-run) {printable}", flush=True)
        return subprocess.CompletedProcess([str(part) for part in argv], 0, "", "")
    try:
        return subprocess.run(  # noqa: S603 - argv es una lista construida aquí, sin shell ni entrada del usuario
            [str(part) for part in argv],
            cwd=ROOT,
            check=False,
            capture_output=capture,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=dict(env) if env is not None else None,
        )
    except FileNotFoundError as exc:
        raise BootstrapError(f"No se encontró el ejecutable: {argv[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise BootstrapError(f"Timout de {timeout:.0f}s en: {printable}") from exc


def probe_python(argv: Sequence[str]) -> str | None:
    """Versión legible si ese intérprete existe y es >= MIN_PYTHON; `None` en cualquier otro caso."""
    code = PROBE % (MIN_PYTHON[0], MIN_PYTHON[1])
    try:
        done = subprocess.run(  # noqa: S603 - intérprete descubierto en el PATH, ejecutado con -c de confianza
            [*argv, "-c", code], capture_output=True, text=True, timeout=25, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    return (done.stdout or "").strip()


def on_windows() -> bool:
    """Única lectura de la plataforma: las funciones reciben `windows=` para poder probarse."""
    return os.name == "nt"


def python_candidates(explicit: str | None, *, windows: bool | None = None) -> list[list[str]]:
    """Lista ordenada de comandos a probar; lo que diga el usuario va primero."""
    out: list[list[str]] = []
    if explicit:
        out.append(shlex.split(explicit))
    if on_windows() if windows is None else windows:
        out.extend([[name, "-3"] for name in WINDOWS_LAUNCHER])
    for name in PYTHON_CANDIDATES:
        found = shutil.which(name)
        if found:
            out.append([found])
    return out


def find_python(explicit: str | None = None) -> tuple[list[str], str]:
    """Primer Python 3.10+ utilizable, con su versión ya leída."""
    for argv in python_candidates(explicit, windows=None):
        version = probe_python(argv)
        if version:
            return list(argv), version
    raise BootstrapError(
        f"Autonoma necesita Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} o más nuevo y no lo encontró.\n"
        f"  Instala Python desde {PYTHON_DOWNLOAD} (en Windows marca 'Add python.exe to PATH')\n"
        "  o dime cuál usar: npm start -- --python C:\\Python312\\python.exe -- run"
    )


def venv_python(venv: Path, *, windows: bool | None = None) -> Path:
    is_nt = on_windows() if windows is None else windows
    return venv / ("Scripts/python.exe" if is_nt else "bin/python")


def venv_executable(venv: Path, name: str, *, windows: bool | None = None) -> Path:
    is_nt = on_windows() if windows is None else windows
    return venv / ("Scripts" if is_nt else "bin") / (f"{name}.exe" if is_nt else name)


def contract_fingerprint() -> str:
    """Huella de lo que obliga a reinstalar: manifiesto del paquete y sus requirements."""
    digest = hashlib.sha256()
    for name in ("pyproject.toml", "requirements.txt", "requirements-test.txt"):
        path = PACKAGE / name
        if path.is_file():
            digest.update(name.encode())
            digest.update(b"\0")
            digest.update(path.read_bytes())
    return digest.hexdigest()[:32]


def read_stamp(venv: Path) -> Mapping[str, object]:
    path = venv / STAMP_NAME
    if not path.is_file():
        return {}
    try:
        raw: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def write_stamp(venv: Path, *, fingerprint: str, python: str) -> None:
    payload = {"fingerprint": fingerprint, "python": python, "installed_at": int(time.time())}
    with contextlib.suppress(OSError):  # una marca de caché no puede romper una instalación correcta
        (venv / STAMP_NAME).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def already_installed(venv: Path, *, fingerprint: str, entry_name: str = "autonoma") -> bool:
    """Vale con que exista la marca, el entry point y el paquete importable."""
    stamp = read_stamp(venv)
    if not stamp or stamp.get("fingerprint") != fingerprint:
        return False
    if not venv_executable(venv, entry_name).is_file():
        return False
    probe = probe_python([str(venv_python(venv))])
    return bool(probe)


def ensure_venv(python: Sequence[str], venv: Path, *, dry_run: bool) -> None:
    if (venv / "pyvenv.cfg").is_file():
        return
    say("1/2", f"creando el entorno en {venv.name}/ (una sola vez)")
    done = _run([*python, "-m", "venv", str(venv)], dry_run=dry_run, capture=True, timeout=300)
    if done.returncode != 0 and not dry_run:
        raise BootstrapError(f"No se pudo crear el entorno: {(done.stderr or done.stdout)[-400:].strip()}")


def install_package(venv: Path, *, dry_run: bool, extras: Sequence[str] = ()) -> None:
    """Instalación editable: editar el código no exige reinstalar nada."""
    suffix = "[" + ",".join(extras) + "]" if extras else ""
    target = str(PACKAGE) + suffix
    say("2/2", "instalando autonoma-agent" + suffix)
    done = _run(
        [str(venv_python(venv)), "-m", "pip", "install", "--quiet", "--disable-pip-version-check", "--upgrade", target],
        dry_run=dry_run,
        capture=True,
        timeout=1200,
    )
    if done.returncode != 0 and not dry_run:
        tail = ((done.stderr or "") + (done.stdout or ""))[-700:].strip()
        raise BootstrapError(
            "Fallo instalando el paquete (¿sin acceso a PyPI?).\n"
            f"  Salida del instalador:\n  {tail}\n"
            "  Si tu red bloquea PyPI, usa el ejecutable portable: dist/Autonoma.exe"
        )


def has_module(venv: Path, module: str) -> bool:
    if not venv_python(venv).is_file():
        return False
    done = _run([str(venv_python(venv)), "-c", f"import {module}"], capture=True, timeout=60)
    return done.returncode == 0


def ensure_dev_tools(venv: Path, *, dry_run: bool) -> None:
    """`test`/`lint`/`types`/`bench` necesitan los extras de desarrollo; el usuario normal no."""
    if has_module(venv, "pytest") and has_module(venv, "ruff"):
        return
    install_package(venv, dry_run=dry_run, extras=DEV_EXTRAS)


def upsert_env_value(path: Path, key: str, value: str) -> None:
    """Escribe `CLAVE=valor` en `.env` conservando el resto (sin reescribir secretos ajenos)."""
    line = f"{key}={value}"
    if not path.is_file():
        path.write_text(line + "\n", encoding="utf-8")
        return
    lines = path.read_text(encoding="utf-8").splitlines()
    replaced = False
    for index, current in enumerate(lines):
        if current.strip().startswith(f"{key}=") or current.strip().startswith(f"# {key}="):
            lines[index] = line
            replaced = True
            break
    if not replaced:
        lines.append(line)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def env_state() -> dict[str, object]:
    """Estado de la configuración visible para `status`, `key` y el arranque."""
    if not DATA_ENV.is_file() and ENV_TEMPLATE.is_file():
        return {"path": str(DATA_ENV), "exists": False, "has_key": False, "has_brave_key": False}
    values = parse_env_file(DATA_ENV)
    return {
        "path": str(DATA_ENV),
        "exists": DATA_ENV.is_file(),
        "has_key": is_real_key(values.get(KEY_NAME, "")),
        "has_brave_key": is_real_key(values.get(BRAVE_KEY_NAME, "")),
    }


def parse_env_file(path: Path) -> Mapping[str, str]:
    if not path.is_file():
        return {}
    out: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip().strip("'\"")
    return out


def is_real_key(value: str) -> bool:
    return value.strip() not in PLACEHOLDERS


def ask_secret(prompt: str) -> str:
    """Lectura oculta de una clave; vacío o interrupción significan "más tarde", nunca un fallo."""
    try:
        return (getpass.getpass(prompt) or "").strip()
    except (EOFError, KeyboardInterrupt):
        return ""


def ensure_env_file(*, dry_run: bool, interactive: bool) -> bool:
    """Garantiza un `.env` utilizable. Devuelve `True` si hay clave lista para trabajar."""
    if not DATA_ENV.is_file():
        if dry_run:
            print(f"    (dry-run) crear {DATA_ENV} desde {ENV_TEMPLATE.name}", flush=True)
            return False
        if ENV_TEMPLATE.is_file():
            DATA_ENV.write_text(ENV_TEMPLATE.read_text(encoding="utf-8"), encoding="utf-8")
            say("env", f"creado {DATA_ENV.name} a partir de {ENV_TEMPLATE.name}")
        else:
            DATA_ENV.write_text(f"{KEY_NAME}=\n", encoding="utf-8")
    if is_real_key(parse_env_file(DATA_ENV).get(KEY_NAME, "")):
        return True
    if not interactive:
        say("aviso", "sin NOTRACK_API_KEY: necesitas una clave para hablar con el modelo.")
        say("aviso", f"ponla en {DATA_ENV} o ejecuta: python scripts/bootstrap.py key")
        return False
    print(
        "Necesito tu clave de NoTrack (https://notrack.ai/api-keys). Se guarda sólo en este equipo,\n"
        f"en {DATA_ENV}. Vacío = la pides más tarde."
    )
    value = ask_secret("NOTRACK_API_KEY: ")
    if not value:
        say("aviso", "sin clave: el agente seguirá funcionando en modo archivos/notas, sin modelo.")
        return False
    upsert_env_value(DATA_ENV, KEY_NAME, value)
    say("ok", "clave guardada en .env")
    return True


def interactive(opts: argparse.Namespace) -> bool:
    """Sólo se pregunta si nadie dijo `--no-input` y hay una terminal de verdad."""
    return bool(not opts.no_input and sys.stdin.isatty() and sys.stdout.isatty())


def resolved_venv(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()
    env_home = (os.environ.get("AUTONOMA_VENV") or "").strip()
    return Path(env_home).expanduser().resolve() if env_home else ROOT / ".venv"


def agent_command(venv: Path, args: Sequence[str]) -> list[str]:
    """El entry point instalado si existe; si no, `python -m autonoma` (mismo comportamiento)."""
    entry = venv_executable(venv, "autonoma")
    if entry.is_file():
        return [str(entry), *args]
    return [str(venv_python(venv)), "-m", "autonoma", *args]


def dev_command(venv: Path, name: str, args: Sequence[str]) -> list[str]:
    """Cómo se corre cada tarea de desarrollo dentro del venv del repo."""
    python = str(venv_python(venv))
    table: dict[str, list[str]] = {
        "test": [python, "-m", "pytest", "tests", "-q", "--cov=autonoma", "--cov-branch"],
        "lint": [python, "-m", "ruff", "check", "autonoma", "tests", "scripts"],
        "format": [python, "-m", "ruff", "format", "autonoma", "tests"],
        "types": [python, "-m", "mypy", "autonoma"],
        "bench": [python, "scripts/bench.py", *args],
        "build": [python, "-m", "PyInstaller", "--noconfirm", "--clean", "autonoma.spec"],
    }
    try:
        return table[name]
    except KeyError as exc:  # defensa: el parser ya limita `name`
        raise BootstrapError(f"tarea desconocida: {name}") from exc


def print_status(venv: Path) -> int:
    """Resumen en texto (y `--json` para scripts): qué está listo y qué falta."""
    installed = already_installed(venv, fingerprint=contract_fingerprint())
    version = ""
    if installed and venv_python(venv).is_file():
        done = _run(
            [str(venv_python(venv)), "-c", "import autonoma;print(autonoma.__version__)"],
            capture=True,
            timeout=60,
        )
        version = (done.stdout or "").strip() if done.returncode == 0 else ""
    payload = {
        "venv": str(venv),
        "venv_ready": (venv / "pyvenv.cfg").is_file(),
        "installed": installed,
        "version": version,
        "package": str(PACKAGE),
        "env": env_state(),
        "portable": sorted(p.name for p in (PACKAGE / "dist").glob("Autonoma*")) if (PACKAGE / "dist").is_dir() else [],
    }
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0 if installed else 1


def setup(venv: Path, *, dry_run: bool, interactive: bool) -> None:
    """`setup` idempotente: lo que ya está, se salta en milisegundos."""
    if not PACKAGE.is_dir():
        raise BootstrapError(f"no encuentro el paquete en {PACKAGE}; ejecuta esto desde el repo de Autonoma")
    python, version = find_python(os.environ.get("AUTONOMA_PYTHON"))
    say("0/2", f"python {version}")
    ensure_venv(python, venv, dry_run=dry_run)
    if dry_run or not already_installed(venv, fingerprint=contract_fingerprint()):
        install_package(venv, dry_run=dry_run)
        if not dry_run:
            write_stamp(venv, fingerprint=contract_fingerprint(), python=version)
    else:
        say("2/2", "entorno y paquete ya instalados")
    ensure_env_file(dry_run=dry_run, interactive=interactive)


def launch(command: Sequence[str], *, cwd: Path | None = None) -> int:
    """Cede la terminal al proceso hijo y devuelve su código: el instalador no filtra salida.

    `cwd` por defecto es la raíz del repo, que es donde `autonoma` resuelve su raíz de datos
    en un checkout; las tareas de desarrollo se corren dentro de `autonoma-agent/`.
    """
    return subprocess.call([str(part) for part in command], cwd=str(cwd or ROOT))  # noqa: S603 - lista construida aquí, sin shell


def run_setup(venv: Path, opts: argparse.Namespace) -> int:
    setup(venv, dry_run=opts.dry_run, interactive=interactive(opts))
    return 0


def run_run(venv: Path, opts: argparse.Namespace) -> int:
    """El camino feliz: `npm start` llega aquí — instalar si hace falta y trabajar."""
    setup(venv, dry_run=opts.dry_run, interactive=interactive(opts))
    command = agent_command(venv, list(opts.args))
    if opts.dry_run:
        print("    (dry-run) " + " ".join(shlex.quote(part) for part in command), flush=True)
        return 0
    return launch(command)


def run_key(venv: Path, opts: argparse.Namespace) -> int:
    setup(venv, dry_run=opts.dry_run, interactive=False)
    value = (opts.args[0].strip() if opts.args else "").strip()
    if not value:
        if opts.no_input or not sys.stdin.isatty():
            raise BootstrapError("pasa la clave como argumento o ejecútalo en una terminal")
        value = ask_secret("NOTRACK_API_KEY: ")
    if not value:
        raise BootstrapError("clave vacía: no se cambió nada")
    if opts.dry_run:
        print(f"    (dry-run) escribir {KEY_NAME} en {DATA_ENV}", flush=True)
        return 0
    if not DATA_ENV.is_file() and ENV_TEMPLATE.is_file():
        DATA_ENV.write_text(ENV_TEMPLATE.read_text(encoding="utf-8"), encoding="utf-8")
    upsert_env_value(DATA_ENV, KEY_NAME, value)
    say("ok", f"{KEY_NAME} guardada en {DATA_ENV}")
    return 0


def run_in_venv(venv: Path, opts: argparse.Namespace, name: str, *, needs_package: bool = True) -> int:
    """Corre una tarea dentro del venv, asegurando antes lo que esa tarea exige."""
    setup(venv, dry_run=opts.dry_run, interactive=False)
    if needs_package:
        ensure_dev_tools(venv, dry_run=opts.dry_run)
    if not venv_python(venv).is_file() and not opts.dry_run:
        raise BootstrapError("el entorno aún no está listo; vuelve a ejecutar `setup`")
    cwd = PACKAGE if name in ("test", "lint", "format", "types", "bench", "build") else ROOT
    command = dev_command(venv, name, list(opts.args))
    if opts.dry_run:
        print("    (dry-run) " + " ".join(shlex.quote(part) for part in command), flush=True)
        return 0
    return launch(command, cwd=cwd)


def run_clean(venv: Path, opts: argparse.Namespace) -> int:
    if opts.dry_run:
        print(f"    (dry-run) borrar {venv}", flush=True)
        return 0
    if venv.is_dir():
        shutil.rmtree(venv, ignore_errors=True)
        say("ok", f"entorno eliminado ({venv.name})")
    else:
        say("ok", "no había nada que limpiar")
    return 0


def run_status(venv: Path, opts: argparse.Namespace) -> int:  # noqa: ARG001 - firma común de los handlers
    return print_status(venv)


COMMANDS: Mapping[str, tuple[object, bool]] = {
    "setup": (run_setup, False),
    "run": (run_run, False),
    "key": (run_key, False),
    "status": (run_status, False),
    "clean": (run_clean, False),
    "doctor": (lambda venv, opts: run_cli_direct(venv, opts, ["--doctor", *opts.args]), False),
    "selftest": (lambda venv, opts: run_cli_direct(venv, opts, ["--selftest", "--json", *opts.args]), False),
    "test": (lambda venv, opts: run_in_venv(venv, opts, "test"), True),
    "lint": (lambda venv, opts: run_in_venv(venv, opts, "lint"), True),
    "format": (lambda venv, opts: run_in_venv(venv, opts, "format"), True),
    "types": (lambda venv, opts: run_in_venv(venv, opts, "types"), True),
    "bench": (lambda venv, opts: run_in_venv(venv, opts, "bench"), True),
    "build": (lambda venv, opts: run_in_venv(venv, opts, "build"), True),
}


def run_cli_direct(venv: Path, opts: argparse.Namespace, cli_args: Sequence[str]) -> int:
    """`doctor`/`selftest` no necesitan las herramientas de desarrollo, sólo el paquete."""
    setup(venv, dry_run=opts.dry_run, interactive=False)
    command = agent_command(venv, list(cli_args))
    if opts.dry_run:
        print("    (dry-run) " + " ".join(shlex.quote(part) for part in command), flush=True)
        return 0
    return launch(command)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bootstrap",
        description="Instala y lanza Autonoma con un solo comando (npm start, Run-Autonoma.bat o esto).",
        epilog="Todo lo que pongas después del comando se pasa tal cual a autonoma:  bootstrap run --plain 'resume mis notas'",
    )
    parser.add_argument("command", choices=tuple(COMMANDS))
    parser.add_argument("--python", help="intérprete a usar (por defecto se busca en el PATH)")
    parser.add_argument("--venv", help="directorio del entorno virtual (por defecto ./.venv)")
    parser.add_argument("--no-input", action="store_true", help="nunca preguntar; sólo avisar (modo CI)")
    parser.add_argument("--dry-run", action="store_true", help="mostrar los comandos sin ejecutarlos")
    parser.add_argument("args", nargs=argparse.REMAINDER, help="argumentos para autonoma")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    raw = list(argv if argv is not None else sys.argv[1:])
    if raw and not raw[0].startswith("-") and raw[0] not in COMMANDS:
        raw.insert(0, "run")  # `bootstrap "hola"` = `bootstrap run "hola"`; con flags hay que decirlo
    raw = [part for part in raw if part != "--"]  # `npm start -- x` deja un separador suelto
    opts = parser.parse_args(raw)
    if opts.python:
        os.environ["AUTONOMA_PYTHON"] = opts.python
    venv = resolved_venv(opts.venv)
    handler = COMMANDS[opts.command][0]
    return int(handler(venv, opts))


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BootstrapError as exc:
        print(f"autonoma: {exc}", file=sys.stderr)
        sys.exit(2)
    except KeyboardInterrupt:
        print("\nautonoma: cancelado", file=sys.stderr)
        sys.exit(int(os.environ.get("AUTONOMA_EXIT_CANCEL", "130")))
