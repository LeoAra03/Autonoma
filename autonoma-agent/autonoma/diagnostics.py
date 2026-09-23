"""Diagnóstico offline: configuración, permisos y límites, sin tocar proveedores.

No inicia clientes, listener ni comandos del modelo. `network_tested=false` es
deliberado: un diagnóstico que contacta proveedores contamina la medición y puede
filtrar la clave. Los checks son una lista declarativa (`_CHECKS`) y cada uno es
una función que devuelve `Check` o `None` — añadir uno no toca los demás.
"""

from __future__ import annotations

import ctypes
import importlib.util
import json
import os
import platform
import sys
import tempfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlsplit

from autonoma._version import __version__
from autonoma.config import load_settings, project_root
from autonoma.errors import AutonomaError
from autonoma.notrack_client import is_loopback_host
from autonoma.presentation import safe_text
from autonoma.runtime import DataOrigin, DataRoot

__all__ = [
    "Check",
    "CheckStatus",
    "DiagnosticReport",
    "collect_diagnostics",
    "is_elevated",
    "run_selftest",
    "writable_directory",
]

_REQUIRED_MODULES: Final[tuple[str, ...]] = ("httpx", "bs4", "lxml", "rich", "psutil")
_FROZEN_REQUIRED: Final[tuple[str, ...]] = ("autonoma.cli", "autonoma.agent", "autonoma.filesystem")


class CheckStatus(str, Enum):
    """Semáforo del diagnóstico; el estado se serializa como cadena estable."""

    OK = "ok"
    WARNING = "warning"
    ERROR = "error"

    @property
    def is_failure(self) -> bool:
        return self is CheckStatus.ERROR


@dataclass(frozen=True, slots=True)
class Check:
    """Una comprobación individual con su veredicto y su explicación accionable."""

    name: str
    status: CheckStatus
    message: str

    def as_dict(self) -> dict[str, str]:
        return {"name": self.name, "status": self.status.value, "message": self.message}


@dataclass(frozen=True, slots=True)
class DiagnosticReport(Mapping[str, Any]):
    """Informe compatible con `report['clave']` y con `json.dumps` directo."""

    platform_name: str
    python_version: str
    frozen: bool
    checks: tuple[Check, ...]
    version: str = __version__
    network_tested: bool = False
    metrics: Mapping[str, Any] | None = None
    data_root: Mapping[str, Any] | None = None

    @property
    def local_checks_passed(self) -> bool:
        return not any(check.status.is_failure for check in self.checks)

    @property
    def failures(self) -> tuple[Check, ...]:
        return tuple(check for check in self.checks if check.status.is_failure)

    @property
    def warnings(self) -> tuple[Check, ...]:
        return tuple(check for check in self.checks if check.status is CheckStatus.WARNING)

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "app": "autonoma",
            "version": self.version,
            "platform": self.platform_name,
            "python": self.python_version,
            "frozen": self.frozen,
            "checks": [check.as_dict() for check in self.checks],
            "local_checks_passed": self.local_checks_passed,
            "network_tested": self.network_tested,
        }
        if self.metrics is not None:
            payload["metrics"] = dict(self.metrics)
        if self.data_root is not None:
            payload["data_root"] = dict(self.data_root)
        return payload

    # -------------------------------------------------- interfaz `Mapping`
    def __getitem__(self, key: str) -> Any:
        return self.as_dict()[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.as_dict())

    def __len__(self) -> int:
        return len(self.as_dict())


def _root_path(data_root: DataRoot | Path | None) -> Path:
    """`DataRoot` o `Path` aceptados en toda la capa de diagnóstico, sin duplicar isinstance."""
    if isinstance(data_root, DataRoot):
        return Path(data_root.path)
    return Path(data_root) if data_root is not None else project_root()


def _root_payload(data_root: DataRoot | Path | None) -> dict[str, Any] | None:
    if data_root is None:
        return None
    if isinstance(data_root, DataRoot):
        return data_root.as_dict()
    return {"path": str(data_root), "origin": DataOrigin.FLAG.value}


def is_elevated() -> bool | None:
    """`None` cuando la plataforma no permite determinarlo: no se afirma sin evidencia.

    Los accesos van por `getattr` porque `ctypes.windll` existe sólo en Windows y
    `os.geteuid` sólo en POSIX; así el chequeo tipado pasa en ambas plataformas y las
    pruebas pueden sustituir la llamada real.
    """
    if os.name == "nt":
        windll = getattr(ctypes, "windll", None)
        if windll is None:
            return None
        try:
            return bool(windll.shell32.IsUserAnAdmin())
        except (AttributeError, OSError):
            return None
    geteuid = getattr(os, "geteuid", None)
    if geteuid is None:
        return None
    try:
        return bool(geteuid() == 0)
    except OSError:
        return None


def writable_directory(path: Path) -> bool:
    """Comprueba escritura real con un temporal que se autoelimina."""
    try:
        path.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryFile(dir=path) as stream:
            stream.write(b"autonoma diagnostic")
        return True
    except OSError:
        return False


def _module_available(name: str) -> bool:
    """Importable o ya cargado: `sys.modules` cubre los bundles congelados."""
    if name in sys.modules:
        return True
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


CheckFn = Callable[[], Sequence[Check]]


def _privileges_check() -> Sequence[Check]:
    elevated = is_elevated()
    if elevated:
        return [
            Check(
                "privileges",
                CheckStatus.WARNING,
                "Elevado: los comandos tendrían permisos de administrador; usa una terminal normal.",
            )
        ]
    if elevated is None:
        return [Check("privileges", CheckStatus.WARNING, "No se pudo determinar elevación.")]
    return [Check("privileges", CheckStatus.OK, "Sin elevación detectada.")]


def _host_access_check() -> Sequence[Check]:
    return [
        Check(
            "host_access",
            CheckStatus.WARNING,
            "Modo host: sin aislamiento ni elevación automática; aprobación por operación.",
        )
    ]


def _stdin_check() -> Sequence[Check]:
    interactive = sys.stdin.isatty()
    return [
        Check(
            "stdin",
            CheckStatus.OK if interactive else CheckStatus.WARNING,
            "TTY disponible." if interactive else "Sin TTY: operaciones locales denegadas.",
        )
    ]


def _configuration_checks(data_root: DataRoot | Path | None = None) -> Sequence[Check]:
    try:
        settings = load_settings(data_root=data_root) if data_root is not None else load_settings()
    except (AutonomaError, ValueError, OSError):
        return [
            Check(
                "configuration",
                CheckStatus.ERROR,
                "Configuración ilegible o inválida; revisa config.json, .env y permisos.",
            )
        ]
    checks: list[Check] = [Check("configuration", CheckStatus.OK, "Configuración válida.")]
    memory_paths: list[tuple[str, Path]] = [("log_directory", settings.log_path())]
    if settings.session_persist:
        # Sólo se exige que sea escribible si de verdad se va a escribir: con la memoria
        # apagada un `sessions/` bloqueado no es un problema del usuario.
        memory_paths.insert(0, ("sessions_directory", settings.sessions_path()))
    memory_paths.append(("jobs_directory", settings.jobs_path()))
    for name, path in (
        ("data_directory", settings.root_dir or project_root()),
        ("knowledge_directory", settings.knowledge_path()),
        *memory_paths,
    ):
        writable = writable_directory(path)
        checks.append(
            Check(
                name,
                CheckStatus.OK if writable else CheckStatus.ERROR,
                f"{safe_text(str(path))}: " + ("escritura comprobada." if writable else "sin escritura."),
            )
        )
    if settings.can_talk_to_model:
        key_detail = (
            "Configurada; validez no comprobada."
            if settings.has_notrack_key
            else "Sin clave: endpoint local en loopback, se confía en lo que corre en esta máquina."
        )
        key_status = CheckStatus.OK
    else:
        key_detail = "Falta clave; configura /key antes de conversar."
        key_status = CheckStatus.WARNING
    checks.append(Check("notrack_key", key_status, key_detail))
    checks.append(
        Check(
            "memory",
            CheckStatus.OK if settings.session_persist else CheckStatus.WARNING,
            "Historial en disco activo; --resume lo recupera."
            if settings.session_persist
            else "Historial en disco apagado: nada sobrevive a cerrar el proceso.",
        )
    )
    checks.append(
        Check(
            "limits",
            CheckStatus.OK,
            f"{settings.max_tool_iterations} iteraciones por turno, "
            f"{settings.max_tool_calls_per_turn} llamadas por vuelta "
            f"({settings.max_parallel_tool_calls} en paralelo), lecturas de {settings.read_limit_chars} caracteres.",
        )
    )
    if settings.allow_private_network:
        checks.append(
            Check(
                "network_policy",
                CheckStatus.WARNING,
                "allow_private_network activo: fetch_url y la investigación pueden tocar la red local.",
            )
        )
    checks.append(_endpoint_check(settings.notrack_base_url))
    checks.append(_hotkey_check())
    checks.append(_dependencies_check())
    return checks


def _endpoint_check(base_url: str) -> Check:
    """El endpoint tiene que ser HTTPS, salvo que apunte a esta misma máquina.

    `http://127.0.0.1:11434` es la firma de un modelo local (Ollama, LM Studio): marcarlo
    como error obligaría al usuario a abrir túneles para algo que nunca sale del equipo.
    La regla es la misma que `validate_base_url`, no una segunda opinión.
    """
    parsed = urlsplit(base_url)
    clean = bool(parsed.hostname) and not (parsed.username or parsed.password or parsed.query or parsed.fragment)
    https_ok = parsed.scheme == "https" and clean
    local_ok = parsed.scheme == "http" and clean and is_loopback_host(str(parsed.hostname))
    if https_ok:
        message = "HTTPS configurado; conectividad no comprobada."
    elif local_ok:
        message = "Endpoint local en loopback: sin HTTPS por diseño; la política sigue cerrada para el resto."
    else:
        message = "URL incompatible con la política HTTPS."
    return Check("notrack_url", CheckStatus.OK if https_ok or local_ok else CheckStatus.ERROR, message)


def _hotkey_check() -> Check:
    available = _module_available("pynput")
    return Check(
        "global_hotkey",
        CheckStatus.OK if available else CheckStatus.WARNING,
        "pynput instalado; listener no probado. Ctrl+C siempre es el mecanismo de terminal."
        if available
        else "pynput no instalado; usa Ctrl+C. El listener global es opcional.",
    )


def _dependencies_check() -> Check:
    missing = [name for name in _REQUIRED_MODULES if not _module_available(name)]
    if not missing:
        return Check(
            "dependencies", CheckStatus.OK, "Dependencias base presentes: " + ", ".join(_REQUIRED_MODULES) + "."
        )
    return Check(
        "dependencies",
        CheckStatus.ERROR if "httpx" in missing else CheckStatus.WARNING,
        "Faltan dependencias: " + ", ".join(missing) + ".",
    )


def _check_plan(data_root: DataRoot | Path | None) -> tuple[CheckFn, ...]:
    """Plan de comprobaciones, en orden: privilegios, modelo de host, TTY y configuración."""
    return (
        _privileges_check,
        _host_access_check,
        _stdin_check,
        lambda: _configuration_checks(data_root),
    )


def collect_diagnostics(
    *,
    metrics: Mapping[str, Any] | None = None,
    data_root: DataRoot | Path | None = None,
) -> DiagnosticReport:
    """Ejecuta la lista de comprobaciones y devuelve el informe consolidado."""
    collected: list[Check] = []
    for check_fn in _check_plan(data_root):
        try:
            collected.extend(check_fn())
        except (AutonomaError, ValueError, OSError) as exc:
            collected.append(
                Check(
                    "diagnostic",
                    CheckStatus.ERROR,
                    "No se pudo completar una comprobación local: " + type(exc).__name__,
                )
            )
    return DiagnosticReport(
        platform_name=platform.system(),
        python_version=platform.python_version(),
        frozen=bool(getattr(sys, "frozen", False)),
        checks=tuple(collected),
        network_tested=False,
        metrics=metrics,
        data_root=_root_payload(data_root),
    )


def format_report(report: DiagnosticReport) -> str:
    """Versión legible del informe (una línea por comprobación)."""
    lines = ["Autonoma — diagnóstico local (sin comprobar servicios externos)"]
    lines.extend(safe_text(f"[{check.status.value.upper()}] {check.name}: {check.message}") for check in report.checks)
    if report.warnings:
        lines.append(f"{len(report.warnings)} advertencia(s); revisa antes de habilitar operaciones locales.")
    return "\n".join(lines)


def run_diagnostics(
    *,
    as_json: bool = False,
    metrics: Mapping[str, Any] | None = None,
    data_root: DataRoot | Path | None = None,
) -> int:
    """Punto de entrada de `--doctor`. 0 = sin errores locales; 1 = hay errores."""
    try:
        report: DiagnosticReport | None = collect_diagnostics(metrics=metrics, data_root=data_root)
    except (AutonomaError, ValueError, OSError):
        report = None
    if report is None:
        fallback = DiagnosticReport(
            platform_name=platform.system(),
            python_version=platform.python_version(),
            frozen=bool(getattr(sys, "frozen", False)),
            checks=(Check("diagnostic", CheckStatus.ERROR, "No se pudo completar el diagnóstico local."),),
        )
        report = fallback
    if as_json:
        print(json.dumps(report.as_dict(), ensure_ascii=True, indent=2))
    else:
        print(format_report(report))
    return 0 if report.local_checks_passed else 1


def run_selftest(*, as_json: bool = False, data_root: DataRoot | Path | None = None) -> int:
    """Autoensayo del ejecutable: importaciones del bundle, raíz de datos y CLI.

    Pensado para el smoke test de `Autonoma.exe` en CI: verifica que el binario
    congelado trae lo necesario sin red, sin claves y sin teclado global.
    """
    checks: list[Check] = []
    frozen = bool(getattr(sys, "frozen", False))
    checks.append(
        Check(
            "frozen_mode",
            CheckStatus.OK if frozen else CheckStatus.WARNING,
            "Ejecutando desde el bundle congelado." if frozen else "Ejecutando desde fuentes (no empaquetado).",
        )
    )
    missing = [module for module in (*_REQUIRED_MODULES, *_FROZEN_REQUIRED) if not _module_available(module)]
    checks.append(
        Check(
            "bundle_imports",
            CheckStatus.OK if not missing else CheckStatus.ERROR,
            "Módulos del bundle disponibles." if not missing else "Faltan en el paquete: " + ", ".join(missing),
        )
    )
    try:
        root = _root_path(data_root)
        writable = writable_directory(root)
    except (AutonomaError, ValueError, OSError):
        root, writable = Path.cwd(), False
    checks.append(
        Check(
            "data_root",
            CheckStatus.OK if writable else CheckStatus.ERROR,
            f"{safe_text(str(root))}: escribible." if writable else f"{safe_text(str(root))}: sin escritura.",
        )
    )
    try:
        from autonoma.cli import parse_args

        parsed = parse_args(["--doctor"])
        entry_ok = bool(getattr(parsed, "doctor", False))
    except (SystemExit, ValueError, ImportError) as exc:
        checks.append(
            Check("cli_entry", CheckStatus.ERROR, f"No se pudo interpretar la línea de órdenes: {type(exc).__name__}")
        )
    else:
        checks.append(
            Check(
                "cli_entry",
                CheckStatus.OK if entry_ok else CheckStatus.ERROR,
                "`--doctor` expuesto por el punto de entrada.",
            )
        )
    report = DiagnosticReport(
        platform_name=platform.system(),
        python_version=platform.python_version(),
        frozen=frozen,
        checks=tuple(checks),
    )
    if as_json:
        payload = {"mode": "selftest", "ok": report.local_checks_passed, **report.as_dict()}
        print(json.dumps(payload, ensure_ascii=True, indent=2))
    else:
        print("Autonoma — autoensayo local (offline)")
        for check in report.checks:
            print(safe_text(f"[{check.status.value.upper()}] {check.name}: {check.message}"))
    return 0 if report.local_checks_passed else 1
