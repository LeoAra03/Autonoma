"""Carga de configuración: variables de entorno, archivo .env y persistencia local."""

from __future__ import annotations

import json
import os
import math
import tempfile
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any



def project_root() -> Path:
    """Raíz del repositorio (un nivel por encima del paquete)."""
    if os.environ.get("AUTONOMA_HOME"):
        return Path(os.environ["AUTONOMA_HOME"]).expanduser().resolve()
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    source = Path(__file__).resolve().parent.parent
    if (source / "run_autonoma.py").is_file():
        return source
    base = os.environ.get("APPDATA") if os.name == "nt" else os.environ.get("XDG_CONFIG_HOME")
    return (Path(base) if base else Path.home() / ".config") / "autonoma"


def _default_env_path() -> Path:
    return project_root() / ".env"


def _default_config_path() -> Path:
    return project_root() / "config.json"


@dataclass
class Settings:
    """Ajustes runtime del agente."""

    allow_commands: bool = False
    notrack_api_key: str = ""
    notrack_base_url: str = "https://api.notrack.ai/v1"
    notrack_model: str = "notrack-uncensored"
    brave_api_key: str = ""
    knowledge_dir: str = ""
    max_tool_iterations: int = 14
    http_timeout: float = 120.0
    search_results: int = 5
    fetch_pages: int = 3
    command_timeout: float = 60.0
    log_dir: str = ""

    extra: dict[str, Any] = field(default_factory=dict)

    def knowledge_path(self) -> Path:
        raw = self.knowledge_dir or str(project_root() / "knowledge_base")
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = (project_root() / path).resolve()
        return path

    def log_path(self) -> Path:
        raw = self.log_dir or str(project_root() / "logs")
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = (project_root() / path).resolve()
        return path

    @property
    def has_notrack_key(self) -> bool:
        return bool(self.notrack_api_key.strip())

    @property
    def has_brave_key(self) -> bool:
        return bool(self.brave_api_key.strip())


def _read_dotenv_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return values
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        if key:
            values[key] = value
    return values


def _write_dotenv_file(path: Path, values: dict[str, str]) -> None:
    existing = _read_dotenv_file(path)
    existing.update({k: v for k, v in values.items() if v is not None})
    for key, value in existing.items():
        if not key.replace("_", "").isalnum() or any(c in value for c in "\r\n\x00"):
            raise ValueError("Clave o valor .env inválido")
    lines = ["# Autonoma — contiene secretos; no publicar."]
    lines.extend(f"{key}={value}" for key, value in existing.items())
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".env-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write("\n".join(lines) + "\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def load_settings(env_path: Path | None = None, config_path: Path | None = None) -> Settings:
    """Carga .env, config.json y variables de entorno (estas últimas ganan)."""
    env_file = env_path or _default_env_path()
    cfg_file = config_path or _default_config_path()

    # Leer sin mutar os.environ: recargar no debe conservar valores obsoletos.

    file_values = _read_dotenv_file(env_file)
    json_values: dict[str, Any] = {}
    if cfg_file.is_file():
        try:
            json_values = json.loads(cfg_file.read_text(encoding="utf-8"))
            if not isinstance(json_values, dict):
                raise ValueError("config.json debe contener un objeto JSON")
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("No se pudo leer config.json; revisa su formato y permisos") from exc

    def pick(env_name: str, json_name: str, default: str = "") -> str:
        env_val = os.environ.get(env_name)
        if env_val is not None:
            return env_val
        if env_name in file_values and file_values[env_name]:
            return file_values[env_name]
        raw = json_values.get(json_name, default)
        return str(raw) if raw is not None else default

    settings = Settings(
        notrack_api_key=pick("NOTRACK_API_KEY", "notrack_api_key"),
        notrack_base_url=pick("NOTRACK_BASE_URL", "notrack_base_url", "https://api.notrack.ai/v1"),
        notrack_model=pick("NOTRACK_MODEL", "notrack_model", "notrack-uncensored"),
        brave_api_key=pick("BRAVE_API_KEY", "brave_api_key"),
        knowledge_dir=pick("KNOWLEDGE_DIR", "knowledge_dir"),
        log_dir=pick("LOG_DIR", "log_dir"),
    )

    bounds = {
        "max_tool_iterations": (int, 1, 50), "http_timeout": (float, 1, 300),
        "command_timeout": (float, 1, 300), "search_results": (int, 1, 20),
        "fetch_pages": (int, 0, 5),
    }
    for name, (cast, minimum, maximum) in bounds.items():
        raw = pick(name.upper(), name, str(getattr(settings, name)))
        try:
            value = cast(raw)
            if not math.isfinite(value) or not minimum <= value <= maximum:
                raise ValueError()
        except (TypeError, ValueError, OverflowError):
            raise ValueError(f"Configuración inválida: {name} debe estar entre {minimum} y {maximum}") from None
        setattr(settings, name, value)

    settings.knowledge_path().mkdir(parents=True, exist_ok=True)
    settings.log_path().mkdir(parents=True, exist_ok=True)
    return settings


def save_api_keys(
    notrack_api_key: str | None = None,
    brave_api_key: str | None = None,
    env_path: Path | None = None,
) -> None:
    """Persiste claves en .env y en el entorno del proceso actual."""
    env_file = env_path or _default_env_path()
    payload: dict[str, str] = {}
    if notrack_api_key is not None:
        payload["NOTRACK_API_KEY"] = notrack_api_key.strip()

    if brave_api_key is not None:
        payload["BRAVE_API_KEY"] = brave_api_key.strip()

    if payload:
        _write_dotenv_file(env_file, payload)
        os.environ.update(payload)


def dump_public_config(settings: Settings) -> dict[str, Any]:
    """Configuración serializable sin secretos completos."""
    data = asdict(settings)
    if data.get("notrack_api_key"):
        data["notrack_api_key"] = "(set)"
    if data.get("brave_api_key"):
        data["brave_api_key"] = "(set)"
    return data
