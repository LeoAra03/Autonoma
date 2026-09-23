"""Configuración: entorno, `.env` y `config.json` con validación tipada e inmutable.

`Settings` es `frozen`: cualquier cambio produce una instancia nueva (`replace`).
Leer configuración ya no muta `os.environ`, y cada valor inválido levanta un
`ConfigurationError` que nombra el ajuste concreto en lugar de un `ValueError` opaco.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import tempfile
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Any, Final

from autonoma.errors import ConfigurationError
from autonoma.runtime import DataOrigin, DataRoot, RenderPreferences, RuntimeContext, resolve_data_root

__all__ = [
    "DEFAULT_NOTRACK_BASE_URL",
    "DEFAULT_NOTRACK_MODEL",
    "NUMERIC_BOUNDS",
    "DotEnvFile",
    "DotEnvStatus",
    "NumericBound",
    "Settings",
    "context_for",
    "dump_public_config",
    "load_settings",
    "project_root",
    "read_dotenv_file",
    "save_api_keys",
]

DEFAULT_NOTRACK_BASE_URL: Final[str] = "https://api.notrack.ai/v1"
DEFAULT_NOTRACK_MODEL: Final[str] = "notrack-uncensored"

_MAX_DOTENV_CHARS: Final[int] = 262_144
_VALID_KEY_CHARS: Final[frozenset[str]] = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_")
_DOTENV_FORBIDDEN_VALUE_CHARS: Final[str] = "\r\n\x00"
_MIN_SECRET_SCAN_LENGTH: Final[int] = 6


class DotEnvStatus(str, Enum):
    """Estado observable de `.env`; el ausencia y el fallo no se confunden."""

    MISSING = "missing"
    OK = "ok"
    MALFORMED = "malformed"
    UNREADABLE = "unreadable"


@dataclass(frozen=True, slots=True)
class DotEnvFile:
    """Resultado de leer `.env`: valores y estado, sin fallos silenciosos."""

    path: Path
    values: Mapping[str, str]
    status: DotEnvStatus = DotEnvStatus.OK
    malformed_keys: tuple[str, ...] = ()

    @property
    def is_present(self) -> bool:
        return self.status is not DotEnvStatus.MISSING


@dataclass(frozen=True, slots=True)
class NumericBound:
    """Límite tipado de un ajuste numérico: un solo lugar para todos."""

    name: str
    kind: str
    minimum: float
    maximum: float
    default: Any

    @property
    def env_key(self) -> str:
        return self.name.upper()

    def coerce(self, raw: str) -> int | float:
        """Conversión sin sorpresas: `NaN`, texto y fuera de rango se rechazan."""
        text = (raw or "").strip()
        if self.kind == "int":
            try:
                value: int | float = int(text)
            except ValueError as exc:
                raise self._invalid(raw) from exc
        else:
            try:
                value = float(text)
            except ValueError as exc:
                raise self._invalid(raw) from exc
        if not math.isfinite(value) or not self.minimum <= value <= self.maximum:
            raise ConfigurationError(
                f"Configuración inválida: {self.name} debe estar entre {self.minimum:g} y {self.maximum:g}",
                context={"setting": self.name, "received": text or "<vacío>"},
            )
        return value

    def _invalid(self, raw: str) -> ConfigurationError:
        return ConfigurationError(
            f"Configuración inválida: {self.name} debe ser {self.kind}",
            context={"setting": self.name, "received": (raw or "").strip() or "<vacío>"},
        )


NUMERIC_BOUNDS: Final[tuple[NumericBound, ...]] = (
    # Los techos son generosos porque el coste de un paso de más es tiempo y no seguridad:
    # la frontera real sigue siendo la aprobación humana de cada operación local.
    NumericBound("max_tool_iterations", "int", 1, 200, 24),
    NumericBound("max_tool_calls_per_turn", "int", 1, 32, 16),
    NumericBound("max_parallel_tool_calls", "int", 1, 8, 4),
    NumericBound("max_history_messages", "int", 2, 512, 64),
    NumericBound("http_timeout", "float", 1, 300, 120.0),
    NumericBound("command_timeout", "float", 1, 1800, 120.0),
    NumericBound("search_results", "int", 1, 20, 5),
    NumericBound("fetch_pages", "int", 0, 5, 3),
    NumericBound("fetch_char_limit", "int", 1_000, 200_000, 12_000),
    NumericBound("page_max_bytes", "int", 100_000, 16_000_000, 4_000_000),
    NumericBound("read_limit_chars", "int", 1_000, 400_000, 80_000),
)


@dataclass(frozen=True, slots=True)
class Settings:
    """Ajustes runtime del agente. Inmutables: usar `replace()` o los helpers."""

    allow_commands: bool = False
    session_persist: bool = True
    allow_private_network: bool = False
    notrack_api_key: str = ""
    notrack_base_url: str = DEFAULT_NOTRACK_BASE_URL
    notrack_model: str = DEFAULT_NOTRACK_MODEL
    brave_api_key: str = ""
    knowledge_dir: str = ""
    sessions_dir: str = ""
    jobs_dir: str = ""
    max_tool_iterations: int = 24
    max_tool_calls_per_turn: int = 16
    max_parallel_tool_calls: int = 4
    max_history_messages: int = 64
    fetch_char_limit: int = 12_000
    page_max_bytes: int = 4_000_000
    read_limit_chars: int = 80_000
    http_timeout: float = 120.0
    search_results: int = 5
    fetch_pages: int = 3
    command_timeout: float = 60.0
    log_dir: str = ""
    root_dir: Path | None = None
    declared_keys: tuple[str, ...] = ()
    dotenv_status: DotEnvStatus = DotEnvStatus.MISSING

    # ------------------------------------------------------------- derivados
    @property
    def root(self) -> Path:
        return self.root_dir if self.root_dir is not None else project_root()

    def _resolve_under_root(self, raw: str, fallback: str) -> Path:
        path = Path(raw).expanduser() if raw else self.root / fallback
        if not path.is_absolute():
            path = (self.root / path).resolve()
        return path

    def knowledge_path(self) -> Path:
        return self._resolve_under_root(self.knowledge_dir, "knowledge_base")

    def log_path(self) -> Path:
        return self._resolve_under_root(self.log_dir, "logs")

    def sessions_path(self) -> Path:
        return self._resolve_under_root(self.sessions_dir, "sessions")

    def jobs_path(self) -> Path:
        return self._resolve_under_root(self.jobs_dir, "jobs")

    @property
    def local_endpoint(self) -> bool:
        """Base URL en loopback: un modelo propio (Ollama, LM Studio) no necesita clave."""
        from urllib.parse import urlsplit  # local: evitar importar la red en la carga del módulo

        host = (urlsplit(self.notrack_base_url).hostname or "").lower()
        return host in {"localhost", "127.0.0.1", "::1", "0.0.0.0", "::"} or host.endswith(".localhost")

    @property
    def has_notrack_key(self) -> bool:
        return bool(self.notrack_api_key.strip())

    @property
    def can_talk_to_model(self) -> bool:
        """Se puede hablar con el modelo: con clave, o con un endpoint local que no la pide."""
        return self.has_notrack_key or self.local_endpoint

    @property
    def has_brave_key(self) -> bool:
        return bool(self.brave_api_key.strip())

    @property
    def secret_values(self) -> tuple[str, ...]:
        """Secretos activos que el filtro de logs debe censurar."""
        return tuple(
            value.strip()
            for value in (self.notrack_api_key, self.brave_api_key)
            if len(value.strip()) >= _MIN_SECRET_SCAN_LENGTH
        )

    # ------------------------------------------------------------- transiciones
    def with_api_keys(self, *, notrack: str | None = None, brave: str | None = None) -> Settings:
        return replace(
            self,
            notrack_api_key=self.notrack_api_key if notrack is None else notrack.strip(),
            brave_api_key=self.brave_api_key if brave is None else brave.strip(),
        )

    def with_allow_commands(self, *, allow: bool) -> Settings:
        return replace(self, allow_commands=bool(allow))

    def with_root(self, root: Path) -> Settings:
        return replace(self, root_dir=root)

    def ensure_directories(self) -> Settings:
        """Crea las carpetas locales (`knowledge_base/`, `logs/`, `sessions/`)."""
        pairs: list[tuple[str, Path]] = [("knowledge_base", self.knowledge_path()), ("logs", self.log_path())]
        if self.session_persist:
            pairs.append(("sessions", self.sessions_path()))
        for label, path in pairs:
            try:
                path.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise ConfigurationError(
                    f"No se pudo preparar el directorio {label}: {path}",
                    context={"error": type(exc).__name__},
                ) from exc
        return self


def project_root() -> Path:
    """Raíz de datos efectiva (checkout, `AUTONOMA_HOME` o ejecutable congelado)."""
    return resolve_data_root(os.environ).path


def read_dotenv_file(path: Path) -> DotEnvFile:
    """Parseo tolerante en forma y estricto en claves; nunca toca el entorno."""
    if not path.is_file():
        return DotEnvFile(path=path, values={}, status=DotEnvStatus.MISSING)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")[:_MAX_DOTENV_CHARS]
    except OSError as exc:
        raise ConfigurationError(
            f"No se pudo leer {path.name}; revisa permisos y propiedad",
            context={"error": type(exc).__name__},
        ) from exc
    values: dict[str, str] = {}
    malformed: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        clean_key = key.strip()
        if not separator or not _is_valid_key(clean_key):
            malformed.append(clean_key or "<sin-nombre>")
            continue
        values[clean_key] = _unquote(value.strip())
    status = DotEnvStatus.MALFORMED if malformed else DotEnvStatus.OK
    return DotEnvFile(path=path, values=values, status=status, malformed_keys=tuple(malformed))


def _is_valid_key(key: str) -> bool:
    return bool(key) and not key[0].isdigit() and all(char in _VALID_KEY_CHARS for char in key)


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def _write_dotenv_file(path: Path, updates: Mapping[str, str]) -> None:
    """Reemplazo atómico que preserva claves ajenas y valida lo que escribe."""
    existing = dict(read_dotenv_file(path).values)
    existing.update({key: value.strip() for key, value in updates.items()})
    for key, value in existing.items():
        if not _is_valid_key(key) or any(char in value for char in _DOTENV_FORBIDDEN_VALUE_CHARS):
            raise ConfigurationError(
                "Clave o valor .env inválido",
                context={"key": key if _is_valid_key(key) else "<inválida>"},
            )
    lines = ["# Autonoma — contiene secretos; no publicar."]
    lines.extend(f"{key}={value}" for key, value in existing.items())
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".env-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write("\n".join(lines) + "\n")
        if os.name != "nt":
            os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except OSError as exc:
        raise ConfigurationError(
            f"No se pudo escribir {path.name}: el sistema de archivos lo impidió",
            context={"error": type(exc).__name__},
        ) from exc
    finally:
        with contextlib.suppress(OSError):
            Path(temporary).unlink(missing_ok=True)


def load_json_config(path: Path) -> Mapping[str, Any]:
    """`config.json` debe ser un objeto JSON; cualquier otra cosa es accionable."""
    if not path.is_file():
        return {}
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise ConfigurationError(
            f"No se pudo leer {path.name}; revisa permisos",
            context={"error": type(exc).__name__},
        ) from exc
    try:
        parsed: Any = json.loads(raw)
    except (ValueError, RecursionError) as exc:
        raise ConfigurationError(
            f"{path.name} no es JSON válido: revisa comas y llaves",
            context={"error": type(exc).__name__},
        ) from exc
    if not isinstance(parsed, dict):
        raise ConfigurationError(f"{path.name} debe contener un objeto JSON")
    return {str(key): value for key, value in parsed.items()}


def _as_bool(raw: str) -> bool:
    """Booleanos sin sorpresas: sólo las formas habituales de sí/no, y nada más."""
    text = (raw or "").strip().lower()
    if text in {"1", "true", "yes", "si", "sí", "on", "y"}:
        return True
    if text in {"", "0", "false", "no", "off", "n"}:
        return False
    raise ConfigurationError(
        f"Configuración inválida: se esperaba un booleano, llegó {raw!r}",
        context={"received": raw.strip()[:40]},
    )


def _normalize_json_value(raw: Any) -> str:
    if isinstance(raw, str):
        return raw
    if isinstance(raw, bool):
        return "true" if raw else "false"
    if isinstance(raw, (int, float)):
        return repr(raw)
    return str(raw)


DataRootLike = DataRoot | Path | str


def _coerce_data_root(data_root: DataRootLike) -> DataRoot:
    """Acepta una raíz ya resuelta o una ruta del usuario, y la normaliza."""
    if isinstance(data_root, DataRoot):
        return data_root
    return DataRoot(path=Path(data_root).expanduser().resolve(), origin=DataOrigin.FLAG)


def load_settings(
    env_path: Path | None = None,
    config_path: Path | None = None,
    *,
    overrides: Mapping[str, str] | None = None,
    data_root: DataRootLike | None = None,
    env: Mapping[str, str] | None = None,
    ensure_dirs: bool = True,
) -> Settings:
    """Compone la configuración sin efectos sobre el entorno del proceso.

    `overrides` inyecta valores ya validados (p. ej. una clave recién guardada con
    `/key`) sin mutar `os.environ`: el estado global deja de ser un canal oculto
    entre la UI y la carga de ajustes.
    """
    root = (
        _coerce_data_root(data_root) if data_root is not None else resolve_data_root(os.environ if env is None else env)
    )
    base_dir = root.path
    dotenv = read_dotenv_file(env_path or base_dir / ".env")
    json_values = load_json_config(config_path or base_dir / "config.json")
    process_env: dict[str, str] = dict(os.environ) if env is None else dict(env)
    if overrides:
        process_env.update(overrides)

    def pick(env_key: str, json_key: str, default: str = "") -> str:
        from_env = process_env.get(env_key)
        if from_env is not None:
            return from_env
        from_dotenv = dotenv.values.get(env_key)
        if from_dotenv:
            return from_dotenv
        return _normalize_json_value(json_values[json_key]) if json_key in json_values else default

    numerics: dict[str, int | float] = {
        bound.name: bound.coerce(pick(bound.env_key, bound.name, repr(bound.default))) for bound in NUMERIC_BOUNDS
    }
    settings = Settings(
        notrack_api_key=pick("NOTRACK_API_KEY", "notrack_api_key"),
        notrack_base_url=pick("NOTRACK_BASE_URL", "notrack_base_url", DEFAULT_NOTRACK_BASE_URL)
        or DEFAULT_NOTRACK_BASE_URL,
        notrack_model=pick("NOTRACK_MODEL", "notrack_model", DEFAULT_NOTRACK_MODEL) or DEFAULT_NOTRACK_MODEL,
        brave_api_key=pick("BRAVE_API_KEY", "brave_api_key"),
        knowledge_dir=pick("KNOWLEDGE_DIR", "knowledge_dir"),
        log_dir=pick("LOG_DIR", "log_dir"),
        sessions_dir=pick("SESSIONS_DIR", "sessions_dir"),
        jobs_dir=pick("JOBS_DIR", "jobs_dir"),
        max_tool_iterations=int(numerics["max_tool_iterations"]),
        max_tool_calls_per_turn=int(numerics["max_tool_calls_per_turn"]),
        max_parallel_tool_calls=int(numerics["max_parallel_tool_calls"]),
        max_history_messages=int(numerics["max_history_messages"]),
        fetch_char_limit=int(numerics["fetch_char_limit"]),
        page_max_bytes=int(numerics["page_max_bytes"]),
        read_limit_chars=int(numerics["read_limit_chars"]),
        session_persist=_as_bool(pick("SESSION_PERSIST", "session_persist", "true")),
        allow_private_network=_as_bool(pick("ALLOW_PRIVATE_NETWORK", "allow_private_network", "false")),
        http_timeout=float(numerics["http_timeout"]),
        command_timeout=float(numerics["command_timeout"]),
        search_results=int(numerics["search_results"]),
        fetch_pages=int(numerics["fetch_pages"]),
        root_dir=base_dir,
        declared_keys=tuple(sorted(dotenv.values)),
        dotenv_status=dotenv.status,
    )
    return settings.ensure_directories() if ensure_dirs else settings


def context_for(
    settings: Settings, render: RenderPreferences | None = None, *, env: Mapping[str, str] | None = None
) -> RuntimeContext:
    """Contexto de ejecución coherente con unos ajustes ya cargados."""
    root = (
        DataRoot(path=settings.root, origin=DataOrigin.FLAG)
        if settings.root_dir is not None
        else resolve_data_root(os.environ if env is None else env)
    )
    return RuntimeContext(
        data_root=root,
        render=render or RenderPreferences(),
        allow_commands=settings.allow_commands,
    )


def save_api_keys(
    notrack_api_key: str | None = None,
    brave_api_key: str | None = None,
    env_path: Path | None = None,
) -> Mapping[str, str]:
    """Persiste claves en `.env` y devuelve lo persistido; no muta el proceso.

    El llamador aplica el resultado como `overrides` de `load_settings`, de modo
    que el entorno global deja de ser el canal entre la UI y la configuración.
    """
    payload: dict[str, str] = {}
    if notrack_api_key is not None:
        payload["NOTRACK_API_KEY"] = notrack_api_key.strip()
    if brave_api_key is not None:
        payload["BRAVE_API_KEY"] = brave_api_key.strip()
    if not payload:
        return {}
    _write_dotenv_file(env_path or project_root() / ".env", payload)
    return payload


def dump_public_config(settings: Settings) -> dict[str, Any]:
    """Configuración serializable sin secretos; apta para logs y `--doctor`."""
    data: dict[str, Any] = asdict(settings)
    for secret in ("notrack_api_key", "brave_api_key"):
        data[secret] = "(set)" if str(data.get(secret, "")).strip() else ""
    data["dotenv_status"] = settings.dotenv_status.value
    data["root_dir"] = str(settings.root)
    data["knowledge_dir_resolved"] = str(settings.knowledge_path())
    data["log_dir_resolved"] = str(settings.log_path())
    data["sessions_dir_resolved"] = str(settings.sessions_path())
    data["local_endpoint"] = settings.local_endpoint
    return data
