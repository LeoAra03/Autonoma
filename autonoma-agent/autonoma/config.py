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
import re
import tempfile
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Any, Final

from autonoma.errors import ConfigurationError
from autonoma.ndus import extract_ndus, is_real_ndus
from autonoma.runtime import DataOrigin, DataRoot, RenderPreferences, RuntimeContext, resolve_data_root

__all__ = [
    "DEFAULT_NOTRACK_BASE_URL",
    "DEFAULT_NOTRACK_MODEL",
    "NUMERIC_BOUNDS",
    "DotEnvFile",
    "DotEnvStatus",
    "NumericBound",
    "PersonalFile",
    "Settings",
    "clear_ndus",
    "context_for",
    "dump_public_config",
    "find_personal_file",
    "load_settings",
    "project_root",
    "read_dotenv_file",
    "read_personal_file",
    "save_api_keys",
    "save_ndus",
]

DEFAULT_NOTRACK_BASE_URL: Final[str] = "https://api.notrack.ai/v1"
DEFAULT_NOTRACK_MODEL: Final[str] = "notrack-uncensored"

_MAX_DOTENV_CHARS: Final[int] = 262_144
_VALID_KEY_CHARS: Final[frozenset[str]] = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_")
_DOTENV_FORBIDDEN_VALUE_CHARS: Final[str] = "\r\n\x00"
_MIN_SECRET_SCAN_LENGTH: Final[int] = 6
_PLACEHOLDERS: Final[frozenset[str]] = frozenset(
    {
        "",
        "sk-notrack-pega-aqui-tu-clave",
        "sk-notrack-pega-aqui-tu-clave-de-notrack",
        "pega-aqui",
        "pega-aqui-tu-clave",
        "pega-aqui-tu-clave-de-notrack",
        "pega-aqui-tu-ndus",
        "pega-aqui-tu-cookie-ndus-si-la-usas",
        "tu-clave",
        "tu-ndus",
    }
)
# Fichero personal del usuario: texto libre con claves, cookies, etc. — nunca se sube a git.
_MAX_PERSONAL_CHARS: Final[int] = 262_144
_PERSONAL_FILENAMES: Final[tuple[str, ...]] = (
    "credenciales.txt",
    "mis_claves.txt",
    "claves.txt",
    "personal.txt",
    "keys.txt",
    "NDUS.txt",
)
_PERSONAL_ENV_VARS: Final[tuple[str, ...]] = (
    "AUTONOMA_CREDENTIALS_FILE",
    "AUTONOMA_PERSONAL_FILE",
    "CREDENCIALES_FILE",
    "PERSONAL_FILE",
)


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
class PersonalFile:
    """Resultado de leer el txt personal (credenciales.txt, mis_claves.txt, etc.)."""

    path: Path | None
    values: Mapping[str, str]
    status: DotEnvStatus = DotEnvStatus.MISSING


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
    # El lazo supervisor: cuántos turnos se persigue el objetivo antes de devolver el control.
    NumericBound("supervisor_max_turns", "int", 1, 200, 12),
    NumericBound("supervisor_max_minutes", "float", 1, 1_440, 90.0),
    NumericBound("supervisor_idle_seconds", "float", 0, 3_600, 0.0),
    # Freno del modo "hasta que pare": fallos duros seguidos tolerados antes de rendirse.
    NumericBound("supervisor_tope_fallos", "int", 1, 1_000, 10),
    NumericBound("approval_policy_max_bytes", "int", 1_024, 1_048_576, 262_144),
)


@dataclass(frozen=True, slots=True)
class Settings:
    """Ajustes runtime del agente. Inmutables: usar `replace()` o los helpers."""

    allow_commands: bool = False
    session_persist: bool = True
    allow_private_network: bool = False
    #: Autoriza que la política delegada alcance `command` (shell), no sólo `argv` (ejecución directa).
    allow_shell_commands: bool = False
    notrack_api_key: str = ""
    notrack_base_url: str = DEFAULT_NOTRACK_BASE_URL
    notrack_model: str = DEFAULT_NOTRACK_MODEL
    brave_api_key: str = ""
    #: Cookie personal NDUS (Amazon u otro servicio) guardada en el txt personal — opcional.
    ndus: str = ""
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
    supervisor_dir: str = ""
    approval_policy: str = ""
    system_prompt_file: str = ""
    supervisor_goal_file: str = ""
    supervisor_max_turns: int = 12
    supervisor_max_minutes: float = 90.0
    supervisor_idle_seconds: float = 0.0
    supervisor_stop_on_block: bool = True
    #: Lazo sin tope de turnos ni de tiempo: sólo corta DONE, `STOP`, Ctrl+C o el freno.
    supervisor_hasta_que_pare: bool = False
    #: Fallos duros seguidos que se toleran en el modo "hasta que pare".
    supervisor_tope_fallos: int = 10
    #: Retomar la corrida anterior del lazo donde se cortó (`supervisor/estado.json`).
    supervisor_continuar: bool = False
    approval_policy_max_bytes: int = 262_144
    root_dir: Path | None = None
    #: Carpeta donde trabaja el agente (el repositorio elegido). Si es `None`, la raíz de datos.
    workspace_dir: Path | None = None
    declared_keys: tuple[str, ...] = ()
    dotenv_status: DotEnvStatus = DotEnvStatus.MISSING
    #: Fichero personal detectado (credenciales.txt / mis_claves.txt) y su estado.
    personal_file: str = ""
    personal_status: DotEnvStatus = DotEnvStatus.MISSING

    # ------------------------------------------------------------- derivados
    @property
    def root(self) -> Path:
        return self.root_dir if self.root_dir is not None else project_root()

    @property
    def workspace(self) -> Path:
        """Dónde trabaja el agente: el repositorio elegido, o la raíz de datos si no se eligió.

        Separado de `root` a propósito: `root` guarda tus claves, sesiones e informes, y
        nadie debería mover eso a un repositorio ajeno sólo porque trabajas en él.
        """
        return self.workspace_dir if self.workspace_dir is not None else self.root

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

    def supervisor_path(self) -> Path:
        """Registro del lazo (informes, `steer.txt`, `STOP`): vive junto a las sesiones."""
        return self._resolve_under_root(self.supervisor_dir, "supervisor")

    def approval_policy_path(self) -> Path | None:
        """Ruta de la política de aprobación delegada, o `None` si no se configuró ninguna."""
        raw = self.approval_policy.strip()
        if not raw:
            return None
        path = Path(raw).expanduser()
        return path if path.is_absolute() else (self.root / path).resolve()

    @property
    def local_endpoint(self) -> bool:
        """Base URL en loopback: un modelo propio (Ollama, LM Studio) no necesita clave."""
        from urllib.parse import urlsplit  # local: evitar importar la red en la carga del módulo

        host = (urlsplit(self.notrack_base_url).hostname or "").lower()
        return host in {"localhost", "127.0.0.1", "::1", "0.0.0.0", "::"} or host.endswith(".localhost")

    @property
    def has_notrack_key(self) -> bool:
        return _is_real_key(self.notrack_api_key)

    @property
    def can_talk_to_model(self) -> bool:
        """Se puede hablar con el modelo: con clave, o con un endpoint local que no la pide."""
        return self.has_notrack_key or self.local_endpoint

    @property
    def has_brave_key(self) -> bool:
        return _is_real_key(self.brave_api_key)

    @property
    def has_ndus(self) -> bool:
        return _is_real_key(self.ndus)

    @property
    def secret_values(self) -> tuple[str, ...]:
        """Secretos activos que el filtro de logs debe censurar."""
        return tuple(
            value.strip()
            for value in (self.notrack_api_key, self.brave_api_key, self.ndus)
            if len(value.strip()) >= _MIN_SECRET_SCAN_LENGTH
        )

    # ------------------------------------------------------------- transiciones
    def with_api_keys(self, *, notrack: str | None = None, brave: str | None = None) -> Settings:
        return replace(
            self,
            notrack_api_key=self.notrack_api_key if notrack is None else notrack.strip(),
            brave_api_key=self.brave_api_key if brave is None else brave.strip(),
        )

    def with_ndus(self, ndus: str) -> Settings:
        """Reemplaza la cookie NDUS en memoria; el guardado en disco lo hace `save_ndus`."""
        return replace(self, ndus=(ndus or "").strip())

    def with_allow_commands(self, *, allow: bool) -> Settings:
        return replace(self, allow_commands=bool(allow))

    def with_root(self, root: Path) -> Settings:
        return replace(self, root_dir=root)

    def with_workspace(self, workspace: Path) -> Settings:
        return replace(self, workspace_dir=workspace)

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


# ------------------------------------------------------------------- fichero personal
def _normalize_personal_key(raw: str) -> str:
    return raw.strip().upper().replace("-", "_").replace(" ", "_").replace(".", "_")


#: Extracción canónica de la cookie: vive en `autonoma.ndus` junto al resto del
#: importador (cabeceras, JSON del inspector, `cookies.txt`). Aquí sólo se reexporta
#: con el nombre histórico para no romper a quien ya lo importara.
_extract_ndus = extract_ndus

_PERSONAL_HEADER: Final[str] = (
    "# Autonoma — credenciales personales. No compartir ni subir a git.\n"
    "# Formato: CLAVE=valor (una por línea). Las líneas con # son comentarios.\n"
)
#: Línea del txt personal que guarda la cookie: `NDUS=…`, `COOKIE=…`, `AMAZON_NDUS=…`
_PERSONAL_NDUS_KEY: Final[re.Pattern[str]] = re.compile(
    r"^\s*(?:NDUS|COOKIE_NDUS|AMAZON_NDUS|AMZ_NDUS|NDUS_COOKIE|COOKIE)\s*=",
    re.IGNORECASE,
)
#: `ndus=…` dentro de una línea (p. ej. `Cookie: ubid=x; ndus=…; otros=…`)
_PERSONAL_NDUS_TOKEN: Final[re.Pattern[str]] = re.compile(
    r"(?P<prefix>ndus\s*[:=]\s*)(?P<quote>[\"']?)(?P<value>[^;,)\s\"']+)(?P=quote)",
    re.IGNORECASE,
)


def read_personal_file(path: Path) -> dict[str, str]:
    """Lee un txt personal tolerante: `CLAVE=valor`, `Cookie: ndus=...`, o líneas sueltas.

    - Ignora vacías y comentarios (`#`, `;`, `//`).
    - Acepta `=` o `:` como separador.
    - Normaliza alias comunes a claves canónicas (`API_KEY` → `NOTRACK_API_KEY`).
    - Si una línea contiene `ndus=...` sin clave explícita, se guarda como `NDUS`.
    - Si una línea suelta parece una clave larga, se asigna a `NOTRACK_API_KEY` si aún falta.
    """
    if not path.is_file():
        return {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")[:_MAX_PERSONAL_CHARS]
    except OSError as exc:
        raise ConfigurationError(
            f"No se pudo leer {path.name}; revisa permisos",
            context={"error": type(exc).__name__},
        ) from exc
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("#", ";", "//")):
            continue
        # Extracción prioritaria de NDUS si la línea es tipo Cookie: ndus=...  o NDUS=...
        # Esto evita que `Cookie: ndus=ABC` se parta por `=` en `COOKIE:_NDUS`
        if "ndus=" in line.lower():
            extracted = _extract_ndus(line)
            if extracted:
                # Guardar NDUS siempre
                values["NDUS"] = extracted
                low = line.lower().strip()
                # Si la línea es principalmente una cookie/ndus, ya está procesada
                if low.startswith(("cookie", "ndus", "ndus_cookie")) or "cookie:" in low:
                    continue
                # Si la línea parece ser `NDUS=valor` pura (clave simple), también continuar
                # para no reprocesarla como genérica
                if "=" in line:
                    first = line.split("=", 1)[0].strip().lower().replace("-", "_").replace(" ", "_")
                    if first in {"ndus", "cookie", "cookie_ndus", "amazon_ndus", "ndus_cookie"}:
                        continue
        if "=" in line or ":" in line:
            if "=" in line:
                key_raw, _, val_raw = line.partition("=")
            else:
                key_raw, _, val_raw = line.partition(":")
            key = key_raw.strip().strip("\"'").strip()
            val = val_raw.strip().strip("\"'").strip()
            if not key:
                ndus = _extract_ndus(line)
                if ndus:
                    values["NDUS"] = ndus
                elif len(val) >= 20 and " " not in val and "NOTRACK_API_KEY" not in values:
                    values["NOTRACK_API_KEY"] = val
                continue
            norm = _normalize_personal_key(key)
            if norm in {"NOTRACK_API_KEY", "NOTRACK_KEY", "API_KEY", "SK_NOTRACK", "NOTRACK", "APIKEY"}:
                values["NOTRACK_API_KEY"] = _unquote(val)
            elif norm in {"BRAVE_API_KEY", "BRAVE_KEY"}:
                values["BRAVE_API_KEY"] = _unquote(val)
            elif norm in {"NOTRACK_BASE_URL", "BASE_URL", "API_URL", "NOTRACK_URL"}:
                values["NOTRACK_BASE_URL"] = _unquote(val)
            elif norm in {"NOTRACK_MODEL", "MODEL", "NOTRACK_MODELO"}:
                values["NOTRACK_MODEL"] = _unquote(val)
            elif norm in {"NDUS", "COOKIE_NDUS", "AMAZON_NDUS", "AMZ_NDUS", "NDUS_COOKIE", "COOKIE"}:
                extracted = _extract_ndus(val)
                if extracted:
                    values["NDUS"] = extracted
                elif norm == "COOKIE" and "ndus=" in val.lower():
                    extracted2 = _extract_ndus(val)
                    values["NDUS"] = extracted2 or _unquote(val)
                else:
                    values["NDUS"] = _unquote(val)
            else:
                values[norm] = _unquote(val)
            continue
        if "ndus=" in line.lower():
            ndus = _extract_ndus(line)
            if ndus:
                values["NDUS"] = ndus
                continue
        if len(line) >= 24 and " " not in line and "\t" not in line and ";" not in line and "," not in line:
            if "NOTRACK_API_KEY" not in values and (line.startswith(("sk-", "nt-")) or len(line) >= 30):
                values["NOTRACK_API_KEY"] = line.strip("\"'")
                continue
            if "NDUS" not in values and len(line) >= 40:
                values["NDUS"] = line.strip("\"'")
                continue
    return values


def find_personal_file(base_dir: Path, env: Mapping[str, str] | None = None) -> Path | None:
    """Busca el txt personal en orden de prioridad. Devuelve la primera ruta existente o la forzada por env."""
    source = env if env is not None else os.environ
    for var in _PERSONAL_ENV_VARS:
        raw = (source.get(var) or "").strip()
        if raw:
            candidate = Path(raw).expanduser()
            return ((base_dir / candidate) if not candidate.is_absolute() else candidate).resolve()
    search_dirs: list[Path] = []
    try:
        resolved_base = base_dir.resolve()
    except OSError:
        resolved_base = base_dir
    search_dirs.append(resolved_base)
    try:
        parent = resolved_base.parent.resolve()
        if parent != resolved_base:
            search_dirs.append(parent)
    except OSError:
        pass
    try:
        cwd = Path.cwd().resolve()
        if cwd not in search_dirs:
            search_dirs.append(cwd)
    except OSError:
        pass
    try:
        home_cfg = Path.home() / ".config" / "autonoma"
        if home_cfg not in search_dirs:
            search_dirs.append(home_cfg)
    except OSError:
        pass
    for directory in search_dirs:
        for name in _PERSONAL_FILENAMES:
            candidate = directory / name
            if candidate.is_file():
                return candidate
    return None


def personal_file_payload(path: Path | None) -> tuple[dict[str, str], DotEnvStatus, str]:
    """Lee el personal si existe; devuelve (valores, estado, ruta_str)."""
    if path is None:
        return {}, DotEnvStatus.MISSING, ""
    if not path.is_file():
        return {}, DotEnvStatus.MISSING, str(path)
    try:
        values = read_personal_file(path)
    except ConfigurationError:
        return {}, DotEnvStatus.UNREADABLE, str(path)
    try:
        size = path.stat().st_size
        if size > 0 and not values:
            return values, DotEnvStatus.MALFORMED, str(path)
    except OSError:
        pass
    return values, DotEnvStatus.OK, str(path)


def _is_valid_key(key: str) -> bool:
    return bool(key) and not key[0].isdigit() and all(char in _VALID_KEY_CHARS for char in key)


def _is_real_key(value: str) -> bool:
    """Placeholder como `pega-aqui` no cuenta como clave real."""
    text = (value or "").strip()
    if not text or text in _PLACEHOLDERS:
        return False
    return "pega-aqui" not in text.lower()


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


def _ruta_opcional(raw: str) -> Path | None:
    """Ruta de la configuración, o `None` si no se dijo nada (sentido: usa la raíz de datos)."""
    texto = (raw or "").strip()
    return Path(texto).expanduser() if texto else None


def load_settings(
    env_path: Path | None = None,
    config_path: Path | None = None,
    *,
    overrides: Mapping[str, str] | None = None,
    data_root: DataRootLike | None = None,
    env: Mapping[str, str] | None = None,
    ensure_dirs: bool = True,
    personal_path: Path | None = None,
) -> Settings:
    """Compone la configuración sin efectos sobre el entorno del proceso.

    `overrides` inyecta valores ya validados (p. ej. una clave recién guardada con
    `/key`) sin mutar `os.environ`: el estado global deja de ser un canal oculto
    entre la UI y la carga de ajustes.

    Prioridad: env > .env > txt personal (credenciales.txt/mis_claves.txt) > config.json > defecto.
    El txt personal es para uso personal rápido: pega tu NOTRACK_API_KEY y tu NDUS en
    `credenciales.txt` y el agente lo lee sin tocar `.env` ni variables.
    """
    root = (
        _coerce_data_root(data_root) if data_root is not None else resolve_data_root(os.environ if env is None else env)
    )
    base_dir = root.path
    dotenv = read_dotenv_file(env_path or base_dir / ".env")
    # Txt personal: búsqueda automática si no se forzó una ruta
    _personal_candidate = personal_path if personal_path is not None else find_personal_file(base_dir, env if env is not None else os.environ)
    personal_values, personal_status, personal_resolved = personal_file_payload(_personal_candidate)
    # También buscar personal en data_root explícito si personal_path era None y no se encontró
    # personal_values ya incluye alias mapeados a canónicas
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
        from_personal = personal_values.get(env_key)
        if from_personal:
            return from_personal
        return _normalize_json_value(json_values[json_key]) if json_key in json_values else default

    numerics: dict[str, int | float] = {
        bound.name: bound.coerce(pick(bound.env_key, bound.name, repr(bound.default))) for bound in NUMERIC_BOUNDS
    }
    # NDUS tiene su propio pick (sólo personal/env/dotenv, no json)
    ndus_value = (
        process_env.get("NDUS")
        or dotenv.values.get("NDUS")
        or personal_values.get("NDUS")
        or _normalize_json_value(json_values.get("ndus", ""))
        or ""
    )
    def _sanitize_key(value: str) -> str:
        text = (value or "").strip()
        if not text or text in _PLACEHOLDERS or "pega-aqui" in text.lower():
            return ""
        return text
    settings = Settings(
        notrack_api_key=_sanitize_key(pick("NOTRACK_API_KEY", "notrack_api_key")),
        notrack_base_url=pick("NOTRACK_BASE_URL", "notrack_base_url", DEFAULT_NOTRACK_BASE_URL)
        or DEFAULT_NOTRACK_BASE_URL,
        notrack_model=pick("NOTRACK_MODEL", "notrack_model", DEFAULT_NOTRACK_MODEL) or DEFAULT_NOTRACK_MODEL,
        brave_api_key=_sanitize_key(pick("BRAVE_API_KEY", "brave_api_key")),
        ndus=_sanitize_key(ndus_value),
        knowledge_dir=pick("KNOWLEDGE_DIR", "knowledge_dir"),
        workspace_dir=_ruta_opcional(pick("WORKSPACE_DIR", "workspace_dir")),
        log_dir=pick("LOG_DIR", "log_dir"),
        sessions_dir=pick("SESSIONS_DIR", "sessions_dir"),
        jobs_dir=pick("JOBS_DIR", "jobs_dir"),
        supervisor_dir=pick("SUPERVISOR_DIR", "supervisor_dir"),
        approval_policy=pick("APPROVAL_POLICY", "approval_policy"),
        system_prompt_file=pick("SYSTEM_PROMPT_FILE", "system_prompt_file"),
        supervisor_goal_file=pick("SUPERVISOR_GOAL_FILE", "supervisor_goal_file"),
        max_tool_iterations=int(numerics["max_tool_iterations"]),
        max_tool_calls_per_turn=int(numerics["max_tool_calls_per_turn"]),
        max_parallel_tool_calls=int(numerics["max_parallel_tool_calls"]),
        max_history_messages=int(numerics["max_history_messages"]),
        fetch_char_limit=int(numerics["fetch_char_limit"]),
        page_max_bytes=int(numerics["page_max_bytes"]),
        read_limit_chars=int(numerics["read_limit_chars"]),
        supervisor_max_turns=int(numerics["supervisor_max_turns"]),
        supervisor_max_minutes=float(numerics["supervisor_max_minutes"]),
        supervisor_idle_seconds=float(numerics["supervisor_idle_seconds"]),
        approval_policy_max_bytes=int(numerics["approval_policy_max_bytes"]),
        supervisor_stop_on_block=_as_bool(pick("SUPERVISOR_STOP_ON_BLOCK", "supervisor_stop_on_block", "true")),
        supervisor_hasta_que_pare=_as_bool(pick("SUPERVISOR_HASTA_QUE_PARE", "supervisor_hasta_que_pare", "false")),
        supervisor_continuar=_as_bool(pick("SUPERVISOR_CONTINUAR", "supervisor_continuar", "false")),
        supervisor_tope_fallos=int(numerics["supervisor_tope_fallos"]),
        session_persist=_as_bool(pick("SESSION_PERSIST", "session_persist", "true")),
        allow_private_network=_as_bool(pick("ALLOW_PRIVATE_NETWORK", "allow_private_network", "false")),
        allow_shell_commands=_as_bool(pick("ALLOW_SHELL_COMMANDS", "allow_shell_commands", "false")),
        http_timeout=float(numerics["http_timeout"]),
        command_timeout=float(numerics["command_timeout"]),
        search_results=int(numerics["search_results"]),
        fetch_pages=int(numerics["fetch_pages"]),
        root_dir=base_dir,
        declared_keys=tuple(sorted(dotenv.values)),
        dotenv_status=dotenv.status,
        personal_file=personal_resolved,
        personal_status=personal_status,
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


# --------------------------------------------------------------------------- NDUS
def _validate_ndus(value: str) -> str:
    """La cookie debe ser un valor plausible: ni plantilla, ni truncado, ni multilínea."""
    text = (value or "").strip()
    if not text:
        raise ConfigurationError(
            "NDUS vacía: pega el valor de la cookie, `ndus=valor` o la cabecera Cookie completa",
            context={"setting": "NDUS"},
        )
    if any(char in text for char in _DOTENV_FORBIDDEN_VALUE_CHARS):
        raise ConfigurationError(
            "La NDUS no puede contener saltos de línea: copia sólo el valor de la cookie",
            context={"setting": "NDUS"},
        )
    if not is_real_ndus(text):
        raise ConfigurationError(
            "La NDUS parece una plantilla o está truncada: copia el valor completo de la cookie",
            context={"setting": "NDUS"},
        )
    return text


def _personal_target_path(
    personal_path: Path | None,
    root: Path | None,
    env: Mapping[str, str] | None,
) -> Path:
    """Dónde escribir: el txt personal ya existente, o `credenciales.txt` en la raíz."""
    if personal_path is not None:
        return personal_path.expanduser()
    base = root if root is not None else project_root()
    found = find_personal_file(base, env)
    return found if found is not None else base / _PERSONAL_FILENAMES[0]


def _is_ndus_line(line: str) -> bool:
    """¿Esta línea del txt personal guarda la cookie? (comentarios aparte)."""
    stripped = line.strip()
    if not stripped or stripped.startswith(("#", ";", "//")):
        return False
    return "ndus=" in stripped.lower() or bool(_PERSONAL_NDUS_KEY.match(stripped))


def _replace_ndus_in_line(line: str, value: str) -> str:
    """Sustituye el valor conservando la forma de la línea (`Cookie: …` o `NDUS=…`)."""
    if _PERSONAL_NDUS_TOKEN.search(line):
        return _PERSONAL_NDUS_TOKEN.sub(lambda match: f"{match.group('prefix')}{value}", line, count=1)
    return f"NDUS={value}"


def _rewrite_personal_ndus(existing: str, value: str | None) -> str:
    """Reescribe el txt personal: `value=None` borra la cookie; si no había línea, la añade."""
    lines = existing.splitlines()
    output: list[str] = []
    replaced = False
    for line in lines:
        if not _is_ndus_line(line):
            output.append(line)
            continue
        if value is None:
            continue
        if replaced:
            output.append(f"# {line.strip()}  (NDUS duplicada; se conserva la primera)")
            continue
        output.append(_replace_ndus_in_line(line, value))
        replaced = True
    if value is not None and not replaced:
        if output and output[-1].strip():
            output.append("")
        output.append(f"NDUS={value}")
    return "\n".join(output).strip("\n")


def _write_text_atomic(path: Path, content: str) -> None:
    """Escritura atómica con permisos 0600 desde el temporal: el secreto nunca queda suelto."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".personal-", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
        if os.name != "nt":
            os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, path)
    except OSError as exc:
        raise ConfigurationError(
            f"No se pudo escribir {path.name}: el sistema de archivos lo impidió",
            context={"error": type(exc).__name__},
        ) from exc
    finally:
        with contextlib.suppress(OSError):
            temporary_path.unlink(missing_ok=True)


def _persist_personal_ndus(path: Path, value: str | None) -> None:
    """Aplica el cambio sobre el txt personal preservando el resto de claves."""
    existing = ""
    if path.is_file():
        try:
            existing = path.read_text(encoding="utf-8", errors="replace")[:_MAX_PERSONAL_CHARS]
        except OSError as exc:
            raise ConfigurationError(
                f"No se pudo leer {path.name}; revisa permisos",
                context={"error": type(exc).__name__},
            ) from exc
    if not existing.strip():
        existing = _PERSONAL_HEADER
    body = _rewrite_personal_ndus(existing, value)
    _write_text_atomic(path, f"{body}\n" if body.strip() else _PERSONAL_HEADER)


def save_ndus(
    ndus: str,
    *,
    personal_path: Path | None = None,
    root: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> Path:
    """Guarda la cookie NDUS en el txt personal; devuelve la ruta escrita.

    Va al txt personal y no a `.env` porque es el sitio documentado para pegar la
    cookie a mano, y porque `.env` gana en prioridad: escribir allí dejaría fuera de
    juego al archivo del usuario. El llamador aplica el valor como override si quiere
    usarlo en la sesión en curso sin recargar de disco.
    """
    value = _validate_ndus(ndus)
    path = _personal_target_path(personal_path, root, env)
    _persist_personal_ndus(path, value)
    return path


def clear_ndus(
    *,
    personal_path: Path | None = None,
    root: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> Path:
    """Borra la cookie del txt personal; el resto de claves queda intacto."""
    path = _personal_target_path(personal_path, root, env)
    _persist_personal_ndus(path, None)
    return path


def dump_public_config(settings: Settings) -> dict[str, Any]:
    """Configuración serializable sin secretos; apta para logs y `--doctor`."""
    data: dict[str, Any] = asdict(settings)
    for secret in ("notrack_api_key", "brave_api_key", "ndus"):
        data[secret] = "(set)" if str(data.get(secret, "")).strip() else ""
    data["dotenv_status"] = settings.dotenv_status.value
    data["personal_status"] = settings.personal_status.value if hasattr(settings.personal_status, "value") else str(settings.personal_status)
    data["personal_file_resolved"] = settings.personal_file
    data["personal_file_present"] = bool(settings.personal_file and Path(settings.personal_file).is_file())
    data["has_ndus"] = bool(settings.ndus.strip())
    data["root_dir"] = str(settings.root)
    data["knowledge_dir_resolved"] = str(settings.knowledge_path())
    data["log_dir_resolved"] = str(settings.log_path())
    data["sessions_dir_resolved"] = str(settings.sessions_path())
    data["supervisor_dir_resolved"] = str(settings.supervisor_path())
    policy_path = settings.approval_policy_path()
    data["approval_policy_resolved"] = str(policy_path) if policy_path is not None else ""
    data["approval_policy_present"] = bool(policy_path is not None and policy_path.is_file())
    data["local_endpoint"] = settings.local_endpoint
    return data
