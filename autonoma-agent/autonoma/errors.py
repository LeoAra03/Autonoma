"""Taxonomía de errores: códigos estables, severidad, códigos de salida y redacción.

Este módulo es la hoja del grafo de dependencias: no importa nada del paquete.
Toda falla esperada se modela como un tipo (`AutonomaError` y subclases) en lugar
de cadenas libres, de modo que UI, logs y métricas puedan reaccionar por `code`
sin parsear mensajes ni tragar excepciones genéricas.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import Enum, IntEnum
from types import MappingProxyType
from typing import Any, ClassVar, Final

__all__ = [
    "AutonomaError",
    "CancelledByUserError",
    "ConfigurationError",
    "ErrorCode",
    "ErrorTraits",
    "ExitCode",
    "FileSystemError",
    "NetworkPolicyError",
    "PathPolicyError",
    "ProcessError",
    "ProcessLaunchError",
    "ProcessTimeoutError",
    "ProviderContractError",
    "ProviderError",
    "ProviderUnavailableError",
    "SearchBackendError",
    "ToolContractError",
    "ToolExecutionDeniedError",
    "describe",
    "redact",
    "traits_for",
]

_MAX_DETAIL_CHARS: Final[int] = 600
_REDACTED: Final[str] = "(redactado)"
_SECRET_KEY_HINTS: Final[tuple[str, ...]] = ("key", "token", "secret", "password", "authorization", "cookie")


class ExitCode(IntEnum):
    """Códigos de salida estables del proceso (documentados en README)."""

    SUCCESS = 0
    INTERNAL = 1
    CONFIGURATION = 2
    PROVIDER = 3
    NETWORK = 4
    APPROVAL_REQUIRED = 5
    FILESYSTEM = 6
    SEARCH = 7
    CANCELLED = 130


class ErrorCode(str, Enum):
    """Clasificación cerrada de fallos; la UI y los logs deciden por código."""

    CONFIGURATION = "configuration"
    TOOL_CONTRACT = "tool_contract"
    APPROVAL_REQUIRED = "approval_required"
    PATH_POLICY = "path_policy"
    FILESYSTEM_IO = "filesystem_io"
    PROCESS_LAUNCH = "process_launch"
    PROCESS_TIMEOUT = "process_timeout"
    NETWORK_POLICY = "network_policy"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    PROVIDER_HTTP = "provider_http"
    PROVIDER_CONTRACT = "provider_contract"
    SEARCH_BACKEND = "search_backend"
    CANCELLED = "cancelled"
    INTERNAL = "internal"


@dataclass(frozen=True, slots=True)
class ErrorTraits:
    """Metadatos declarativos por código de error: sin `if/else` dispersos."""

    severity: int
    retryable: bool
    exit_code: ExitCode
    hint: str


_TRAITS: Final[Mapping[ErrorCode, ErrorTraits]] = {
    ErrorCode.CONFIGURATION: ErrorTraits(
        logging.ERROR, False, ExitCode.CONFIGURATION, "Corrige .env o config.json y vuelve a iniciar."
    ),
    ErrorCode.TOOL_CONTRACT: ErrorTraits(
        logging.WARNING, False, ExitCode.INTERNAL, "La herramienta se rechazó antes de ejecutarse; reformula la instrucción."
    ),
    ErrorCode.APPROVAL_REQUIRED: ErrorTraits(
        logging.INFO, False, ExitCode.APPROVAL_REQUIRED, "Requiere aprobación humana explícita en una TTY interactiva."
    ),
    ErrorCode.PATH_POLICY: ErrorTraits(
        logging.WARNING, False, ExitCode.FILESYSTEM, "Usa rutas absolutas de unidad sin enlaces simbólicos ni reparse points."
    ),
    ErrorCode.FILESYSTEM_IO: ErrorTraits(
        logging.ERROR, False, ExitCode.FILESYSTEM, "Revisa permisos y existencia de la ruta; el agente no reintenta operaciones destructivas."
    ),
    ErrorCode.PROCESS_LAUNCH: ErrorTraits(
        logging.ERROR, False, ExitCode.FILESYSTEM, "El programa no existe o no puede iniciarse en ese directorio."
    ),
    ErrorCode.PROCESS_TIMEOUT: ErrorTraits(
        logging.WARNING, True, ExitCode.FILESYSTEM, "Aumenta timeout o divide el trabajo; el árbol del proceso fue terminado."
    ),
    ErrorCode.NETWORK_POLICY: ErrorTraits(
        logging.WARNING, False, ExitCode.NETWORK, "Solo se permiten destinos HTTP(S) públicos en puertos estándar."
    ),
    ErrorCode.PROVIDER_UNAVAILABLE: ErrorTraits(
        logging.ERROR, True, ExitCode.PROVIDER, "Verifica la conexión y la URL del proveedor; reintenta más tarde."
    ),
    ErrorCode.PROVIDER_HTTP: ErrorTraits(
        logging.ERROR, False, ExitCode.PROVIDER, "Revisa la clave/cuota del proveedor con /key y /status."
    ),
    ErrorCode.PROVIDER_CONTRACT: ErrorTraits(
        logging.ERROR, True, ExitCode.PROVIDER, "La respuesta no cumple el contrato; reintenta o cambia de modelo."
    ),
    ErrorCode.SEARCH_BACKEND: ErrorTraits(
        logging.WARNING, True, ExitCode.SEARCH, "Ningún backend de búsqueda respondió; agrega BRAVE_API_KEY para mayor fiabilidad."
    ),
    ErrorCode.CANCELLED: ErrorTraits(
        logging.INFO, True, ExitCode.CANCELLED, "Tarea detenida por el usuario; el estado en disco puede estar parcial."
    ),
    ErrorCode.INTERNAL: ErrorTraits(
        logging.ERROR, False, ExitCode.INTERNAL, "Falla interna; revisa logs/autonoma.log con el trace_id indicado."
    ),
}


def traits_for(code: ErrorCode) -> ErrorTraits:
    return _TRAITS[code]


def redact(text: str, secrets: Iterable[str] = ()) -> str:
    """Elimina secretos conocidos y recorta; apto para logs y para el modelo."""
    if not text:
        return text
    scrubbed = text if len(text) <= _MAX_DETAIL_CHARS else text[:_MAX_DETAIL_CHARS] + "…[truncado]"
    for secret in secrets:
        cleaned = secret.strip()
        if len(cleaned) >= 6:
            scrubbed = scrubbed.replace(cleaned, _REDACTED)
    return scrubbed


def _freeze_context(context: Mapping[str, Any]) -> Mapping[str, str]:
    """Valores a texto plano, con claves sensibles enmascaradas y longitud acotada."""
    frozen: dict[str, str] = {}
    for key, value in context.items():
        if any(hint in key.lower() for hint in _SECRET_KEY_HINTS):
            frozen[key] = _REDACTED
            continue
        frozen[key] = redact(str(value))
    return MappingProxyType(frozen)


class AutonomaError(RuntimeError):
    """Error de dominio con código, severidad y contexto listo para log estructurado."""

    code: ClassVar[ErrorCode] = ErrorCode.INTERNAL

    def __init__(self, message: str, *, context: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message: str = message
        self.context: Mapping[str, str] = _freeze_context(context or {})

    @property
    def traits(self) -> ErrorTraits:
        return traits_for(self.code)

    @property
    def retryable(self) -> bool:
        return self.traits.retryable

    @property
    def exit_code(self) -> ExitCode:
        return self.traits.exit_code

    @property
    def hint(self) -> str:
        return self.traits.hint

    @property
    def severity(self) -> int:
        return self.traits.severity

    def user_message(self) -> str:
        """Mensaje para humanos: qué pasó y qué hacer, sin detalles internos."""
        return f"{self.message} — {self.hint}" if self.hint else self.message

    def to_log_fields(self) -> dict[str, str]:
        """Campos planos y serializables para el logger JSON."""
        fields: dict[str, str] = {"error_code": self.code.value, "error": type(self).__name__}
        fields.update({f"ctx_{key}": value for key, value in self.context.items()})
        return fields


class ConfigurationError(AutonomaError, ValueError):
    """Configuración ausente o inválida (también `ValueError` por compatibilidad)."""

    code: ClassVar[ErrorCode] = ErrorCode.CONFIGURATION


class ToolContractError(AutonomaError, ValueError):
    """Argumentos fuera de contrato: ninguna herramienta debe ejecutarse."""

    code: ClassVar[ErrorCode] = ErrorCode.TOOL_CONTRACT


class ToolExecutionDeniedError(AutonomaError):
    """Falta aprobación humana o la operación está deshabilitada por política."""

    code: ClassVar[ErrorCode] = ErrorCode.APPROVAL_REQUIRED


class PathPolicyError(AutonomaError, ValueError):
    """Ruta rechazada por la política (Windows ambiguo, dispositivos, reparse points)."""

    code: ClassVar[ErrorCode] = ErrorCode.PATH_POLICY


class FileSystemError(AutonomaError):
    """Error de E/S o de política de disco durante una operación CRUD."""

    code: ClassVar[ErrorCode] = ErrorCode.FILESYSTEM_IO


class ProcessError(AutonomaError):
    """El proceso terminó de forma inesperada o no cumple los límites."""

    code: ClassVar[ErrorCode] = ErrorCode.PROCESS_LAUNCH


class ProcessLaunchError(ProcessError):
    """El proceso no pudo iniciarse."""

    code: ClassVar[ErrorCode] = ErrorCode.PROCESS_LAUNCH


class ProcessTimeoutError(ProcessError):
    """El proceso superó su plazo y fue terminado junto con su árbol."""

    code: ClassVar[ErrorCode] = ErrorCode.PROCESS_TIMEOUT


class NetworkPolicyError(AutonomaError, ValueError):
    """Destino de red bloqueado por la política de egreso."""

    code: ClassVar[ErrorCode] = ErrorCode.NETWORK_POLICY


class ProviderError(AutonomaError):
    """El proveedor del modelo respondió mal o no respondió."""

    code: ClassVar[ErrorCode] = ErrorCode.PROVIDER_UNAVAILABLE


class ProviderUnavailableError(ProviderError):
    """Conectividad o TLS: el endpoint no respondió."""

    code: ClassVar[ErrorCode] = ErrorCode.PROVIDER_UNAVAILABLE


class ProviderHttpError(ProviderError):
    """HTTP 4xx/5xx del proveedor; el cuerpo remoto nunca se reproduce."""

    code: ClassVar[ErrorCode] = ErrorCode.PROVIDER_HTTP
    RETRYABLE_STATUS: ClassVar[frozenset[int]] = frozenset({408, 409, 425, 429, 500, 502, 503, 504})

    def __init__(self, message: str, *, status_code: int, context: Mapping[str, Any] | None = None) -> None:
        super().__init__(message, context={**(context or {}), "status_code": status_code})
        self.status_code: int = status_code

    @property
    def retryable(self) -> bool:
        """Reintetable sólo para transitorios; 401/403/404 no se reintentan jamás."""
        return self.status_code in self.RETRYABLE_STATUS


class ProviderContractError(ProviderError):
    """La respuesta no cumple el contrato OpenAI-compatible esperado."""

    code: ClassVar[ErrorCode] = ErrorCode.PROVIDER_CONTRACT


class SearchBackendError(AutonomaError):
    """Todos los backends de búsqueda fallaron; se conserva el detalle por backend."""

    code: ClassVar[ErrorCode] = ErrorCode.SEARCH_BACKEND

    def __init__(self, message: str, *, failures: tuple[tuple[str, str], ...], context: Mapping[str, Any] | None = None) -> None:
        super().__init__(message, context={**(context or {}), "backends": "; ".join(f"{name}: {detail}" for name, detail in failures)})
        self.failures: tuple[tuple[str, str], ...] = failures


class CancelledByUserError(AutonomaError):
    """Cancelación cooperativa solicitada por el usuario (tecla P o Ctrl+C)."""

    code: ClassVar[ErrorCode] = ErrorCode.CANCELLED


def describe(exc: BaseException) -> tuple[ErrorCode, bool]:
    """Clasifica cualquier excepción para métricas/logs sin exponer su detalle."""
    if isinstance(exc, AutonomaError):
        return exc.code, exc.retryable
    if isinstance(exc, (KeyboardInterrupt,)):
        return ErrorCode.CANCELLED, True
    if isinstance(exc, FileNotFoundError):
        return ErrorCode.FILESYSTEM_IO, False
    if isinstance(exc, TimeoutError):
        return ErrorCode.PROCESS_TIMEOUT, True
    if isinstance(exc, OSError):
        return ErrorCode.NETWORK_POLICY if isinstance(exc, ConnectionError) else ErrorCode.INTERNAL, True
    return ErrorCode.INTERNAL, False


def classify_log_level(exc: BaseException) -> int:
    """Nivel de log coherente con la taxonomía (ruido → silencio, fallo → error)."""
    if isinstance(exc, AutonomaError):
        return exc.severity
    if isinstance(exc, (KeyboardInterrupt, SystemExit)):
        return logging.DEBUG
    return logging.ERROR
