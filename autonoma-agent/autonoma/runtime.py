"""Contexto de ejecución inmutable: raíz de datos y preferencias de render.

Sustituye el antiguo patrón de mutar `os.environ` y un `global RICH` desde
`main()`: esas banderas globales mutables hacían que el comportamiento dependiera
del orden de llamadas y de fugas entre sesiones. Aquí el contexto se resuelve una
vez, se pasa explícitamente y es `frozen`.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Final

__all__ = ["DataOrigin", "DataRoot", "RenderPreferences", "RuntimeContext"]

_HOME_ENV: Final[str] = "AUTONOMA_HOME"
_APP_DIR_NAME: Final[str] = "autonoma"
_PLAIN_ENV: Final[str] = "AUTONOMA_PLAIN"
_REDUCED_MOTION_ENV: Final[str] = "AUTONOMA_REDUCED_MOTION"
_QUIET_ENV: Final[str] = "AUTONOMA_QUIET"
_NO_COLOR_ENV: Final[str] = "NO_COLOR"


class DataOrigin(str, Enum):
    """De dónde salió la raíz de datos; útil para diagnósticos y para el .exe."""

    FLAG = "flag"
    ENV = "env"
    FROZEN_EXECUTABLE = "frozen-executable"
    CHECKOUT = "checkout"
    USER_CONFIG = "user-config"


@dataclass(frozen=True, slots=True)
class DataRoot:
    """Directorio donde viven `.env`, `config.json`, `knowledge_base/` y `logs/`."""

    path: Path
    origin: DataOrigin

    def as_dict(self) -> dict[str, str]:
        return {"path": str(self.path), "origin": self.origin.value}


def _truthy(raw: str | None) -> bool:
    return (raw or "").strip().lower() not in {"", "0", "false", "no", "off"}


@dataclass(frozen=True, slots=True)
class RenderPreferences:
    """Preferencias de salida; inmutables y derivadas de argv + entorno."""

    plain: bool = False
    reduced_motion: bool = False
    quiet: bool = False
    no_color: bool = False
    rich_available: bool = True

    @classmethod
    def from_environment(
        cls,
        env: Mapping[str, str] | None = None,
        *,
        plain: bool = False,
        reduced_motion: bool = False,
        quiet: bool = False,
        rich_available: bool = True,
    ) -> RenderPreferences:
        source = env if env is not None else os.environ
        return cls(
            plain=plain or _truthy(source.get(_PLAIN_ENV)),
            reduced_motion=reduced_motion or _truthy(source.get(_REDUCED_MOTION_ENV)),
            quiet=quiet or _truthy(source.get(_QUIET_ENV)),
            no_color=_NO_COLOR_ENV in source,
            rich_available=rich_available,
        )

    @property
    def use_rich(self) -> bool:
        """Rich sólo si está instalado y el usuario no pidió texto simple."""
        return self.rich_available and not self.plain

    @property
    def animate(self) -> bool:
        return self.use_rich and not self.reduced_motion

    def as_dict(self) -> dict[str, bool]:
        return {
            "plain": self.plain,
            "reduced_motion": self.reduced_motion,
            "quiet": self.quiet,
            "no_color": self.no_color,
            "rich": self.use_rich,
        }


def resolve_data_root(
    env: Mapping[str, str] | None = None,
    *,
    override: str | os.PathLike[str] | None = None,
    source_root: Path | None = None,
    frozen: bool | None = None,
    executable: str | None = None,
) -> DataRoot:
    """Resuelve la raíz de datos sin mutar nada: flag > `AUTONOMA_HOME` > congelado > checkout > user config."""
    source = env if env is not None else os.environ
    if override:
        return DataRoot(Path(override).expanduser().resolve(), DataOrigin.FLAG)
    home = (source.get(_HOME_ENV) or "").strip()
    if home:
        return DataRoot(Path(home).expanduser().resolve(), DataOrigin.ENV)
    is_frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    if is_frozen:
        program = executable or sys.executable
        return DataRoot(Path(program).resolve().parent, DataOrigin.FROZEN_EXECUTABLE)
    repo = source_root or Path(__file__).resolve().parent.parent
    if (repo / "run_autonoma.py").is_file():
        return DataRoot(repo, DataOrigin.CHECKOUT)
    base = source.get("APPDATA") if os.name == "nt" else source.get("XDG_CONFIG_HOME")
    root = Path(base) if base else Path.home() / ".config"
    return DataRoot(root / _APP_DIR_NAME, DataOrigin.USER_CONFIG)


@dataclass(frozen=True, slots=True)
class RuntimeContext:
    """Raíz de datos + preferencias + interruptores de riesgo, para toda la sesión."""

    data_root: DataRoot
    render: RenderPreferences
    allow_commands: bool = False

    @classmethod
    def detect(
        cls,
        *,
        env: Mapping[str, str] | None = None,
        data_dir: str | None = None,
        plain: bool = False,
        reduced_motion: bool = False,
        quiet: bool = False,
        allow_commands: bool = False,
        rich_available: bool = True,
        source_root: Path | None = None,
    ) -> RuntimeContext:
        source = env if env is not None else os.environ
        return cls(
            data_root=resolve_data_root(source, override=data_dir, source_root=source_root),
            render=RenderPreferences.from_environment(
                source,
                plain=plain,
                reduced_motion=reduced_motion,
                quiet=quiet,
                rich_available=rich_available,
            ),
            allow_commands=allow_commands,
        )

    @property
    def root(self) -> Path:
        return self.data_root.path

    @property
    def use_rich(self) -> bool:
        return self.render.use_rich

    def with_allow_commands(self, allow: bool) -> RuntimeContext:
        return replace(self, allow_commands=allow)

    def with_render(self, **changes: bool) -> RuntimeContext:
        return replace(self, render=replace(self.render, **changes))

    def as_dict(self) -> dict[str, object]:
        return {
            "data_root": self.data_root.as_dict(),
            "render": self.render.as_dict(),
            "allow_commands": self.allow_commands,
        }
