"""Política de aprobación delegada: el humano fija el alcance, el agente lo obedece.

El modo desatendido (un `--supervise` que encadena turnos mientras el usuario hace otra cosa)
necesita autorizar operaciones locales sin TTY. Este módulo es ese canal y el único: un archivo
JSON escrito AFUERA del agente, con allow-list explícita por herramienta, presupuesto por turno y
negación por defecto.

Tres invariantes de diseño, todas comprobables:

- El modelo no puede ampliar su propio alcance. La ruta de la política se añade a las raíces
  protegidas del gestor de archivos, así que `write_file`/`edit_file`/`delete_path` sobre ella fallsan
  en la capa de rutas, no por buena voluntad.
- Nada se permite por omisión. Sin `allow`, sin reglas que coincidan o con presupuesto agotado, la
  respuesta es "no" y el turno sigue por otra vía.
- Un prefijo de comando no es un shell. Si el comando encadenado (`;`, `&&`, `|`, redirecciones)
  excede lo que la regla nombra, se niega: la autorización cubre UN comando, no lo que venga detrás.
"""

from __future__ import annotations

import json
import math
import os
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from autonoma.errors import ConfigurationError, ToolExecutionDeniedError
from autonoma.tool_contracts import LAUNCH_TOOLS

__all__ = [
    "POLICY_TEMPLATE",
    "ApprovalPolicy",
    "Budget",
    "Decision",
    "PolicyState",
    "Rule",
    "command_ceiling",
    "enforce_ceiling",
    "load_approval_policy",
    "normalize_command",
    "parse_policy_json",
    "write_policy_template",
]

POLICY_VERSION: Final[int] = 1

#: Herramientas que pueden destruir o lanzar procesos: la política las trata como destructivas.
DESTRUCTIVE_TOOLS: Final[frozenset[str]] = frozenset(
    {"delete_path", "move_path", "copy_path", "run_command", "spawn_command", "kill_job"}
)

#: Lo que cuenta como "escritura" para el presupuesto del turno (incluye procesos: pueden escribir).
_WRITE_TOOLS: Final[frozenset[str]] = frozenset(
    {
        "write_file",
        "edit_file",
        "append_file",
        "mkdir",
        "copy_path",
        "move_path",
        "delete_path",
        "run_command",
        "spawn_command",
        "kill_job",
    }
)

#: Qué argumentos son rutas para cada herramienta. `cwd` en comandos se valida si la política fija raíces.
_PATH_KEYS: Final[Mapping[str, tuple[str, ...]]] = {
    "read_file": ("path",),
    "write_file": ("path",),
    "edit_file": ("path",),
    "append_file": ("path",),
    "search_files": ("path",),
    "list_dir": ("path",),
    "mkdir": ("path",),
    "delete_path": ("path",),
    "copy_path": ("src", "dst"),
    "move_path": ("src", "dst"),
    "save_knowledge": ("filename",),
    "run_command": ("cwd",),
    "spawn_command": ("cwd",),
}

#: Caracteres de control de shell. Con ellos un prefijo autorizado dejaría de describir el comando.
_SHELL_CONTROL_CHARS: Final[str] = ";&|<>`\n\r"
_SUBSHELL_MARKS: Final[tuple[str, ...]] = ("$(", "${")

#: Programas que ningún modo de ejecución debe lanzar desde el agente (apagados y particionado).
_CEILING_PROGRAMS: Final[frozenset[str]] = frozenset(
    {"shutdown", "reboot", "halt", "poweroff", "diskpart", "fdisk", "sfdisk", "parted", "wmic"}
)
#: Intérpretes que ejecutan lo que llevan en `-c`: el techo se aplica también a ese payload,
#: porque `bash -c "rm -rf /"` es el mismo comando con un envoltorio.
_CEILING_WRAPPERS: Final[frozenset[str]] = frozenset(
    {"sh", "bash", "zsh", "dash", "ksh", "cmd", "powershell", "pwsh", "python", "python3", "perl", "ruby", "node"}
)
_WRAPPER_FLAGS: Final[tuple[str, ...]] = ("-c", "/c", "-command")
#: Un nombre de programa "plausible" evita que un texto entre comillas dispare el corte.
_PROGRAM_TOKEN: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_./\\-]{1,64}$")
_SHELL_SEPARATORS: Final[re.Pattern[str]] = re.compile(r"(?:&&|\|\||[;&|\n])")
#: Destinos de `rm -rf` que no son un borrado, son una reinstalación del sistema.
_ABSOLUTE_WIPES: Final[frozenset[str]] = frozenset({"/", "/*", "~", "~/", "$HOME", "${HOME}", "*", "./*"})


def _wrapped_payload(tokens: Sequence[str]) -> str | None:
    """El texto que un intérprete va a ejecutar (`bash -c "..."`), si lo hay.

    Sin esto, `bash -c "rm -rf /"` es el mismo comando con un envoltorio y el techo por tokens lo
    dejaría pasar. El corte sigue siendo un suelo, no un sandbox: un `-c` con código arbitrario no
    se analiza, sólo se le aplica la misma regla al payload.
    """
    flags = {flag.strip("-/") for flag in _WRAPPER_FLAGS}
    for index, token in enumerate(tokens):
        if token.lower().strip("-/") in flags and index + 1 < len(tokens):
            return " ".join(tokens[index + 1 :]).strip("\"'")
    return None


def _command_segments(raw: str) -> list[list[str]]:
    """Segmentos de una línea de shell, troceada por sus separadores, sin comillas sueltas."""
    text = " ".join((raw or "").split())
    segments: list[list[str]] = []
    for chunk in _SHELL_SEPARATORS.split(text):
        tokens = chunk.strip().split()
        if tokens:
            segments.append(tokens)
    return segments


def _is_wipe_target(token: str) -> bool:
    cleaned = token.rstrip("'\"()")
    if cleaned in _ABSOLUTE_WIPES:
        return True
    # Un segmento de un solo nivel (`/usr`, `/etc`, `C:\Windows`) es raíz del sistema, no un proyecto.
    return bool(re.fullmatch(r"/[A-Za-z0-9_.-]+", cleaned)) or bool(re.fullmatch(r"[A-Za-z]:[\\/]", cleaned))


def command_ceiling(command: str) -> str | None:
    """Motivo por el que un comando no puede autorizarse nunca; `None` si pasa el corte duro.

    Es un techo, no un filtro: vive por debajo de la política y de `--allow-commands`, así que ni una
    política mal escrita ni `force` lo mueven. Se evalúa por tokens, no por subcadenas, para no
    bloquear `format.py` ni un mensaje de commit que mencione `reboot`.
    """
    text = command or ""
    if ":(){ :|:& };:" in " ".join(text.split()) or "){ :|:&" in text:
        return "bomba de fork"
    if re.search(r"[|>]\s*(?:sudo\s+)?(?:/bin/)?(?:ba|z|k|da)?sh\b", text):
        return "tubería de entrada a un shell"
    for tokens in _command_segments(text):
        head = tokens[0]
        if "/" in head:
            head = head.rsplit("/", 1)[-1]
        if head.lower() in {"sudo", "doas", "command", "env", "time", "nohup", "nice"} and len(tokens) > 1:
            tokens = tokens[1:]
            head = tokens[0].rsplit("/", 1)[-1]
        lowered = head.lower()
        if not _PROGRAM_TOKEN.match(lowered):
            continue
        if lowered in _CEILING_PROGRAMS or lowered.startswith("mkfs"):
            return f"programa fuera de todo lo delegable: {lowered}"
        if lowered == "rm":
            flags = "".join(token[1:] for token in tokens[1:] if token.startswith("-"))
            if (
                "r" in flags
                and "f" in flags
                and any(_is_wipe_target(token) for token in tokens[1:] if not token.startswith("-"))
            ):
                return "borrado recursivo forzado sobre una raíz del sistema"
        if lowered in _CEILING_WRAPPERS:
            payload = _wrapped_payload(tokens)
            if payload is not None:
                nested = command_ceiling(payload)
                if nested is not None:
                    return f"código entregado a {lowered}: {nested}"
        if lowered == "dd" and any(token.lower().startswith("of=/dev/") for token in tokens[1:]):
            return "escritura directa sobre un dispositivo de bloque"
        if lowered in {"chmod", "chown"}:
            flags = "".join(token[1:] for token in tokens[1:] if token.startswith("-"))
            targets = [token for token in tokens[1:] if not token.startswith("-")]
            if "R" in flags and targets and _is_wipe_target(targets[-1]):
                return f"cambio de permisos recursivo sobre {targets[-1]}"
        elif lowered == "reg" and len(tokens) > 2 and tokens[1].lower() in {"delete", "add"}:
            if tokens[2].lower().startswith(("hklm", "hkey_local_machine")):
                return "escritura en el registro de sistema"
        elif lowered == "del" and any(token.lower() in {"/s", "/q", "/s/q", "/q/s"} for token in tokens[1:]):
            targets = [token for token in tokens[1:] if not token.startswith("/")]
            if targets and _is_wipe_target(targets[0]):
                return "borrado forzado recursivo sobre una raíz de Windows"
    return None


_ALLOWED_TOP_KEYS: Final[frozenset[str]] = frozenset(
    {"version", "roots", "budget", "max_denials", "allow", "deny", "allow_shell_commands"}
)
_ALLOWED_RULE_KEYS: Final[frozenset[str]] = frozenset(
    {"tool", "tools", "commands", "paths", "label", "requires_approval", "allow_chaining", "max_ops"}
)
_ALLOWED_BUDGET_KEYS: Final[frozenset[str]] = frozenset({"max_ops", "max_writes", "max_deletes", "max_seconds"})
_MAX_RULES: Final[int] = 64
_MAX_GLOBS: Final[int] = 64
_MAX_PREFIX_CHARS: Final[int] = 4_000


def _real(path: Path) -> Path:
    """Ruta canónica sin importar si existe: `realpath` deshace symlinks antes de comparar."""
    return Path(os.path.realpath(str(Path(path).expanduser())))


def _relativize(target: str, roots: Sequence[Path]) -> str | None:
    """`target` relativo a la primera raíz que lo contenga, o `None` si cuelga fuera de todas."""
    resolved = _real(Path(target))
    for root in roots:
        base = _real(root)
        try:
            relative = resolved.relative_to(base)
        except ValueError:
            continue
        return relative.as_posix() or "."
    return None


def _compile_glob(pattern: str) -> re.Pattern[str]:
    """Globs con `**` (cualquier profundidad, incluida cero carpetas), `*`, `?` y `[abc]`.

    No se usa `fnmatch`: su `*` cruza separadores de carpeta, así que `src/*` autorizaría
    `src/../../.ssh/clave`. Aquí `*` nunca cruza `/` y el patrón se ancla completo.
    """
    out: list[str] = []
    index = 0
    length = len(pattern)
    while index < length:
        char = pattern[index]
        if char == "*":
            if pattern[index : index + 2] == "**":
                index += 2
                if pattern[index : index + 1] == "/":
                    index += 1
                    out.append("(?:.*/)?")
                else:
                    out.append(".*")
                continue
            out.append("[^/]*")
            index += 1
            continue
        if char == "?":
            out.append("[^/]")
            index += 1
            continue
        if char == "[":
            close = pattern.find("]", index + 1)
            if close < 0:
                out.append(re.escape(char))
                index += 1
                continue
            body = pattern[index + 1 : close]
            if body.startswith("!"):
                body = "^" + body[1:]
            out.append("[" + body.replace("\\", "\\\\") + "]")
            index = close + 1
            continue
        out.append(re.escape(char))
        index += 1
    return re.compile("^" + "".join(out) + "$")


def normalize_command(raw: str) -> str:
    """Comando comparable: espacios colapsados, sin control de shell oculto por tabuladores."""
    return " ".join((raw or "").split())


def _has_shell_control(raw: str) -> bool:
    """Control de shell *fuera de comillas*: `git commit -m "a | b"` no es un encadenamiento."""
    text = raw or ""
    quote: str | None = None
    index = 0
    while index < len(text):
        char = text[index]
        if quote is not None:
            if char == "\\" and quote == '"':
                index += 2
                continue
            if char == quote:
                quote = None
            index += 1
            continue
        if char in "'\"" or char == "`":
            quote = char
            index += 1
            continue
        if char in _SHELL_CONTROL_CHARS:
            return True
        if text.startswith(("$(", "${"), index):
            return True
        index += 1
    return quote is not None  # comilla sin cerrar: el comando no describe lo que parece


@dataclass(frozen=True, slots=True)
class Budget:
    """Toques del turno desatendido. Un valor `<= 0` significa "sin límite" (no "cero permitido")."""

    max_ops: int = 80
    max_writes: int = 40
    max_deletes: int = 3
    max_seconds: float = 1_800.0

    @staticmethod
    def from_payload(payload: Mapping[str, Any]) -> Budget:
        unknown = sorted(set(payload) - _ALLOWED_BUDGET_KEYS)
        if unknown:
            raise ConfigurationError(
                "Claves desconocidas en `budget` de la política de aprobación",
                context={"keys": ",".join(unknown)},
            )

        defaults = Budget()

        def number(key: str, default: int | float) -> int | float:
            raw = payload.get(key, default)
            if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                raise ConfigurationError(
                    f"`budget.{key}` debe ser numérico",
                    context={"key": f"budget.{key}", "received": type(raw).__name__},
                )
            value = float(raw)
            if not math.isfinite(value):  # NaN e infinitos: un presupuesto degenerado no se hereda
                raise ConfigurationError(f"`budget.{key}` debe ser finito", context={"key": f"budget.{key}"})
            return int(value) if key != "max_seconds" else value

        return Budget(
            max_ops=int(number("max_ops", defaults.max_ops)),
            max_writes=int(number("max_writes", defaults.max_writes)),
            max_deletes=int(number("max_deletes", defaults.max_deletes)),
            max_seconds=float(number("max_seconds", defaults.max_seconds)),
        )

    def as_payload(self) -> dict[str, float]:
        return {
            "max_ops": self.max_ops,
            "max_writes": self.max_writes,
            "max_deletes": self.max_deletes,
            "max_seconds": self.max_seconds,
        }


@dataclass(frozen=True, slots=True)
class Rule:
    """Una línea de alcance: qué herramienta, con qué comando o bajo qué rutas."""

    label: str
    tools: frozenset[str]
    command_prefixes: tuple[str, ...] = ()
    path_globs: tuple[str, ...] = ()
    requires_approval: bool = True
    allow_chaining: bool = False
    max_ops: int = 0
    _globs: tuple[re.Pattern[str], ...] = ()

    def matches(self, tool: str, command: str | None, paths: Sequence[str]) -> bool:
        if "*" not in self.tools and tool not in self.tools:
            return False
        if self.command_prefixes:
            if command is None:
                return False
            if not any(_command_matches(command, prefix) for prefix in self.command_prefixes):
                return False
        if self.path_globs:
            if not paths:
                return False
            if not all(_path_matches(item, self._globs) for item in paths):
                return False
        return True

    def as_payload(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "tools": sorted(self.tools),
            "commands": list(self.command_prefixes),
            "paths": list(self.path_globs),
            "requires_approval": self.requires_approval,
            "max_ops": self.max_ops,
        }


def _command_matches(command: str, prefix: str) -> bool:
    """El prefijo debe cerrar en frontera de token: `git` no autoriza `gitleaks`."""
    haystack = command.lower() if os.name == "nt" else command
    needle = prefix.lower() if os.name == "nt" else prefix
    if needle.endswith(" "):
        return haystack.startswith(needle)
    return haystack == needle or haystack.startswith(needle + " ")


def _path_matches(relative: str, globs: Sequence[re.Pattern[str]]) -> bool:
    candidate = relative.lower() if os.name == "nt" else relative
    return any(pattern.match(candidate) for pattern in globs)


def _build_rule(payload: Mapping[str, Any], *, has_roots: bool, index: int) -> Rule:
    if not isinstance(payload, Mapping):
        raise ConfigurationError(
            "Cada regla de la política debe ser un objeto JSON", context={"rule_index": str(index)}
        )
    unknown = sorted(set(payload) - _ALLOWED_RULE_KEYS)
    if unknown:
        raise ConfigurationError(
            "Claves desconocidas en una regla de la política de aprobación",
            context={"keys": ",".join(unknown), "rule_index": str(index)},
        )
    single = payload.get("tool")
    many = payload.get("tools")
    if single is not None and many is not None:
        raise ConfigurationError("Una regla no puede mezclar `tool` y `tools`", context={"rule_index": str(index)})
    raw_tools = [single] if single is not None else list(many or [])
    if not raw_tools:
        raise ConfigurationError("A cada regla le hace falta `tool` o `tools`", context={"rule_index": str(index)})
    tools = frozenset(str(item).strip() for item in raw_tools if str(item).strip())
    if len(tools) != len(raw_tools) or not tools:
        raise ConfigurationError(
            "Las herramientas de una regla deben ser textos no vacíos", context={"rule_index": str(index)}
        )

    raw_commands = payload.get("commands") or []
    raw_paths = payload.get("paths") or []
    for name, value in (("commands", raw_commands), ("paths", raw_paths)):
        if isinstance(value, str) or not isinstance(value, Sequence):
            raise ConfigurationError(
                f"`{name}` debe ser una lista de textos", context={"rule_index": str(index), "field": name}
            )
        if len(value) > _MAX_GLOBS:
            raise ConfigurationError(
                f"`{name}` admite a lo sumo {_MAX_GLOBS} patrones", context={"rule_index": str(index)}
            )
    commands = tuple(str(item) for item in raw_commands)
    if any(not item.strip() or len(item) > _MAX_PREFIX_CHARS or "\x00" in item for item in commands):
        raise ConfigurationError(
            "Los prefijos de comando deben ser textos acotados y no vacíos", context={"rule_index": str(index)}
        )
    paths = tuple(str(item).strip().replace("\\", "/") for item in raw_paths)
    if any(not item for item in paths):
        raise ConfigurationError("Los patrones de ruta no pueden estar vacíos", context={"rule_index": str(index)})
    if paths and not has_roots:
        raise ConfigurationError(
            "Toda regla con `paths` necesita `roots` en la política: sin raíz no hay alcance",
            context={"rule_index": str(index)},
        )
    label = str(payload.get("label") or (commands[0] if commands else paths[0] if paths else sorted(tools)[0]))[:120]
    return Rule(
        label=label,
        tools=tools,
        command_prefixes=commands,
        path_globs=paths,
        requires_approval=_as_bool(payload.get("requires_approval", True), "requires_approval", index),
        allow_chaining=_as_bool(payload.get("allow_chaining", False), "allow_chaining", index),
        max_ops=_as_count(payload.get("max_ops", 0), "max_ops", index),
        _globs=tuple(_compile_glob(item) for item in paths),
    )


def _as_bool(value: Any, key: str, index: int) -> bool:
    if isinstance(value, bool):
        return value
    raise ConfigurationError(f"`{key}` debe ser true o false", context={"field": key, "rule_index": str(index)})


def _as_count(value: Any, key: str, index: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigurationError(f"`{key}` debe ser un entero", context={"field": key, "rule_index": str(index)})
    if value < 0:
        raise ConfigurationError(f"`{key}` no puede ser negativo", context={"field": key, "rule_index": str(index)})
    return value


@dataclass(frozen=True, slots=True)
class Decision:
    """Resultado de evaluar una operación contra la política. Nunca ambiguo: se permite o se dice por qué."""

    allowed: bool
    reason: str
    rule: str = ""
    needs_confirm: bool = False

    def as_payload(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "rule": self.rule,
            "needs_confirm": self.needs_confirm,
        }


class PolicyState:
    """Contadores del turno en curso: presupuesto consumido, negaciones seguidas y reloj.

    Lo lleva la sesión, no la política: la política es inmutable y compartible; el gasto es de un turno.
    """

    __slots__ = (
        "_clock",
        "budget",
        "consecutive_denials",
        "deletes",
        "denials",
        "max_consecutive_denials",
        "ops",
        "rule_ops",
        "started",
        "writes",
    )

    def __init__(self, budget: Budget, *, max_consecutive_denials: int = 3, clock: Any = time.monotonic) -> None:
        self.budget = budget
        self.max_consecutive_denials = max_consecutive_denials
        self.ops = 0
        self.writes = 0
        self.deletes = 0
        self.denials = 0
        self.consecutive_denials = 0
        self.rule_ops: dict[str, int] = {}
        self.started = float(clock())
        self._clock = clock

    def elapsed(self) -> float:
        return max(0.0, float(self._clock()) - self.started)

    def reset_turn(self) -> None:
        """Nuevo turno: se reinicia el gasto y la insistencia; `denials` queda como total histórico.

        La racha de negaciones se mide dentro de un turno: "el agente está dando vueltas contra el
        mismo muro" es un juicio sobre lo que hizo *ahora*, no sobre hace veinte turnos.
        """
        self.consecutive_denials = 0
        self.ops = 0
        self.writes = 0
        self.deletes = 0
        self.rule_ops.clear()
        self.started = float(self._clock())

    def exhausted(self) -> str | None:
        """Motivo por el que ya no se autoriza nada en este turno, o `None` si queda presupuesto."""
        if self.budget.max_ops > 0 and self.ops >= self.budget.max_ops:
            return f"presupuesto de operaciones del turno agotado ({self.budget.max_ops})"
        if self.budget.max_writes > 0 and self.writes >= self.budget.max_writes:
            return f"presupuesto de escrituras del turno agotado ({self.budget.max_writes})"
        if self.budget.max_deletes > 0 and self.deletes >= self.budget.max_deletes:
            return f"presupuesto de borrados del turno agotado ({self.budget.max_deletes})"
        if self.budget.max_seconds > 0 and self.elapsed() >= self.budget.max_seconds:
            return f"ventana de tiempo del supervisor agotada ({self.budget.max_seconds:g} s)"
        if self.max_consecutive_denials > 0 and self.consecutive_denials >= self.max_consecutive_denials:
            return (
                f"{self.consecutive_denials} negaciones seguidas: la política no cubre lo que intenta el "
                "agente; se detiene para no dar vueltas contra el mismo muro"
            )
        return None

    def snapshot(self) -> dict[str, Any]:
        return {
            "ops": self.ops,
            "writes": self.writes,
            "deletes": self.deletes,
            "denials": self.denials,
            "consecutive_denials": self.consecutive_denials,
            "elapsed_seconds": round(self.elapsed(), 1),
            "budget": self.budget.as_payload(),
        }


@dataclass(frozen=True, slots=True)
class ApprovalPolicy:
    """Allow-list leída de un JSON: decide, no ejecuta. La decisión la aplica la CLI."""

    source: Path | None
    roots: tuple[Path, ...]
    allow: tuple[Rule, ...]
    deny: tuple[Rule, ...]
    budget: Budget
    max_consecutive_denials: int
    declared_tools: frozenset[str]
    #: Con `False` la política sólo delega `argv` (programa + argumentos literales). Delegar
    #: `command` es delegar un intérprete de shell, y eso tiene que ser una decisión escrita.
    allow_shell_commands: bool = False
    unenforceable: tuple[str, ...] = ()

    @property
    def enabled(self) -> bool:
        """Sin reglas `allow` la política no delega nada: manda la aprobación humana por TTY."""
        return bool(self.allow)

    @property
    def protected_files(self) -> tuple[str, ...]:
        """Archivos que el agente no puede editar: la propia política, si existe en disco."""
        return tuple(str(self.source)) if self.source is not None else ()

    @classmethod
    def from_payload(
        cls,
        payload: Mapping[str, Any],
        *,
        source: Path | None = None,
        default_root: Path | None = None,
        allow_shell_commands: bool = False,
    ) -> ApprovalPolicy:
        if not isinstance(payload, Mapping):
            raise ConfigurationError("La política de aprobación debe ser un objeto JSON")
        unknown = sorted(set(payload) - _ALLOWED_TOP_KEYS)
        if unknown:
            raise ConfigurationError(
                "Claves desconocidas en la política de aprobación",
                context={"keys": ",".join(unknown), "path": str(source or "<memoria>")},
            )
        version = payload.get("version", POLICY_VERSION)
        if isinstance(version, bool) or not isinstance(version, int) or version != POLICY_VERSION:
            raise ConfigurationError(
                f"Versión de política no soportada (se espera {POLICY_VERSION})",
                context={"received": str(version)},
            )
        raw_roots = payload.get("roots") or []
        if isinstance(raw_roots, str):
            raw_roots = [raw_roots]
        if not isinstance(raw_roots, Sequence):
            raise ConfigurationError("`roots` debe ser una lista de directorios")
        base = default_root if default_root is not None else Path.cwd()
        roots = tuple(
            sorted(
                {(base / Path(str(item).strip())).resolve() for item in raw_roots if str(item).strip()},
                key=str,
            )
        )
        if len(raw_roots) > 0 and not roots:
            raise ConfigurationError("`roots` está presente pero vacío: no se puede acotar nada")
        for root in roots:
            if not root.is_dir():
                raise ConfigurationError(
                    f"Raíz de la política inexistente: {root}",
                    context={"path": str(root)},
                )
        raw_shell = payload.get("allow_shell_commands", False)
        if not isinstance(raw_shell, bool):
            raise ConfigurationError("`allow_shell_commands` debe ser true o false en la política")
        allow_shell = bool(allow_shell_commands) or raw_shell
        budget = Budget.from_payload(payload.get("budget") or {})
        max_denials = _as_count(payload.get("max_denials", 3), "max_denials", -1)
        for key in ("allow", "deny"):
            raw = payload.get(key) or []
            if isinstance(raw, Mapping) or not isinstance(raw, Sequence):
                raise ConfigurationError(f"`{key}` debe ser una lista de reglas")
            if len(raw) > _MAX_RULES:
                raise ConfigurationError(f"`{key}` admite a lo sumo {_MAX_RULES} reglas")
        allow = tuple(
            _build_rule(item, has_roots=bool(roots), index=index)
            for index, item in enumerate(payload.get("allow") or [])
        )
        deny = tuple(
            _build_rule(item, has_roots=bool(roots), index=index)
            for index, item in enumerate(payload.get("deny") or [])
        )
        declared = frozenset(tool for rule in (*allow, *deny) for tool in rule.tools if tool != "*")
        unenforceable = tuple(sorted(tool for tool in declared if tool not in _enforceable_tools()))
        return cls(
            source=source,
            roots=roots,
            allow=allow,
            deny=deny,
            budget=budget,
            max_consecutive_denials=max_denials,
            declared_tools=declared,
            allow_shell_commands=allow_shell,
            unenforceable=unenforceable,
        )

    def decide(
        self,
        tool: str,
        args: Mapping[str, Any],
        state: PolicyState,
        *,
        tty_available: bool = False,
    ) -> Decision:
        """`Decision` para una operación. Esta función no escribe, no lanza y no pregunta: sólo evalúa."""
        blocked = state.exhausted()
        if blocked is not None:
            return Decision(allowed=False, reason=blocked)
        raw_command = args.get("command")
        raw_argv = args.get("argv")
        textual = isinstance(raw_command, str) and bool(raw_command.strip())
        argv: list[str] | None = None
        if isinstance(raw_argv, Sequence) and not isinstance(raw_argv, str):
            argv = [str(item) for item in raw_argv]
        # La identidad del comando es la misma en las dos formas: con `argv` se compara la lista
        # unida, así que una lista autorizada por prefijo significa exactamente lo que se ejecuta.
        command: str | None = None
        if textual:
            command = normalize_command(raw_command or "")
        elif argv:
            command = normalize_command(" ".join(argv))
        if tool in LAUNCH_TOOLS and command is None:
            return Decision(allowed=False, reason="falta `command` o `argv`: no hay nada que autorizar")
        if command is not None:
            ceiling = command_ceiling(command)
            if ceiling is not None:
                return Decision(allowed=False, reason=f"comando fuera de todo lo delegable: {ceiling}")
            if textual and not self.allow_shell_commands:
                return Decision(
                    allowed=False,
                    reason=(
                        "la política delega `argv` (programa y argumentos literales), no una línea de "
                        "shell; para autorizar el shell escribe allow_shell_commands: true en la política"
                    ),
                )
        paths = self._paths_for(tool, args)
        if paths is None:
            return Decision(
                allowed=False,
                reason=f"ruta fuera de las raíces de la política ({', '.join(str(root) for root in self.roots)})",
            )
        for rule in self.deny:
            if rule.matches(tool, command, paths):
                return Decision(
                    allowed=False, reason=f"bloqueado por la política (regla `{rule.label}`)", rule=rule.label
                )
        chained = textual and _has_shell_control(raw_command or "")
        for rule in self.allow:
            if not rule.matches(tool, command, paths):
                continue
            if chained and command is not None and not rule.allow_chaining:
                return Decision(
                    allowed=False,
                    reason=(
                        f"la regla `{rule.label}` autoriza un comando, no un encadenamiento; "
                        "se necesita aprobación humana o una regla con allow_chaining"
                    ),
                    rule=rule.label,
                )
            if rule.max_ops > 0 and state.rule_ops.get(rule.label, 0) >= rule.max_ops:
                return Decision(
                    allowed=False,
                    reason=f"la regla `{rule.label}` ya gastó sus {rule.max_ops} operaciones",
                    rule=rule.label,
                )
            if rule.requires_approval and not tty_available:
                return Decision(
                    allowed=False,
                    reason=f"la regla `{rule.label}` exige un `SI` humano en TTY; en desatendido se niega",
                    rule=rule.label,
                )
            return Decision(
                allowed=True,
                reason=f"autorizada por la política (regla `{rule.label}`)",
                rule=rule.label,
                needs_confirm=rule.requires_approval and tty_available,
            )
        return Decision(
            allowed=False,
            reason=(
                "fuera del alcance delegado por la política"
                + (f" de {self.source.name}" if self.source is not None else "")
                + "; si hace falta, amplíala tú o pide el SI en una TTY"
            ),
        )

    def record(self, tool: str, rule_label: str, state: PolicyState, *, allowed: bool) -> None:
        """Cuenta el gasto del turno. Se llama una vez por operación, se permita o se niegue."""
        if not allowed:
            state.denials += 1
            state.consecutive_denials += 1
            return
        state.consecutive_denials = 0
        state.ops += 1
        if tool in _WRITE_TOOLS:
            state.writes += 1
        if tool == "delete_path":
            state.deletes += 1
        if rule_label:
            state.rule_ops[rule_label] = state.rule_ops.get(rule_label, 0) + 1

    def _paths_for(self, tool: str, args: Mapping[str, Any]) -> tuple[str, ...] | None:
        """Rutas relevantes relativas a las raíces; `None` = alguna cae fuera (se niega sin más vueltas)."""
        keys = _PATH_KEYS.get(tool, ())
        if not keys or not self.roots:
            return ()
        collected: list[str] = []
        for key in keys:
            raw = args.get(key)
            if not isinstance(raw, str) or not raw.strip():
                continue
            relative = _relativize(raw, self.roots)
            if relative is None:
                return None
            collected.append(relative)
        return tuple(collected)

    def describe(self) -> dict[str, Any]:
        return {
            "source": str(self.source) if self.source is not None else None,
            "enabled": self.enabled,
            "roots": [str(root) for root in self.roots],
            "budget": self.budget.as_payload(),
            "max_consecutive_denials": self.max_consecutive_denials,
            "allow_shell_commands": self.allow_shell_commands,
            "allow": [rule.as_payload() for rule in self.allow],
            "deny": [rule.as_payload() for rule in self.deny],
            "unenforceable": list(self.unenforceable),
        }


def _enforceable_tools() -> frozenset[str]:
    """Herramientas reales del contrato: una regla que nombre otra es ruido, no autorización."""
    from autonoma.tool_contracts import tool_names  # local: evita un ciclo en la carga del módulo

    return frozenset(tool_names())


def load_approval_policy(
    path: Path,
    *,
    default_root: Path | None = None,
    max_bytes: int = 262_144,
    allow_shell_commands: bool = False,
) -> ApprovalPolicy:
    """Lee y valida el JSON. Un archivo ilegible o inválido es un fallo de configuración, no un "sigue sin política"."""
    target = Path(path).expanduser()
    if not target.exists():
        raise ConfigurationError(
            f"No existe la política de aprobación: {target}",
            context={"path": str(target)},
        )
    if not target.is_file():
        raise ConfigurationError(
            f"La política de aprobación no es un archivo regular: {target}",
            context={"path": str(target)},
        )
    try:
        raw = target.read_bytes()[:max_bytes]
    except OSError as exc:
        raise ConfigurationError(
            f"No se pudo leer la política de aprobación: {exc}",
            context={"path": str(target)},
        ) from exc
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConfigurationError(
            "La política de aprobación no es JSON válido",
            context={"path": str(target), "error": type(exc).__name__},
        ) from exc
    return ApprovalPolicy.from_payload(
        payload if isinstance(payload, Mapping) else {},
        source=target,
        default_root=default_root,
        allow_shell_commands=allow_shell_commands,
    )


def parse_policy_json(
    text: str,
    *,
    default_root: Path | None = None,
    allow_shell_commands: bool = False,
) -> ApprovalPolicy:
    """Versión en memoria de `load_approval_policy`, para pruebas y para `--approval-policy-json`."""
    try:
        payload = json.loads(text or "")
    except json.JSONDecodeError as exc:
        raise ConfigurationError("La política de aprobación no es JSON válido", context={"error": str(exc)}) from exc
    if not isinstance(payload, Mapping):
        raise ConfigurationError("La política de aprobación debe ser un objeto JSON")
    return ApprovalPolicy.from_payload(
        payload, source=None, default_root=default_root, allow_shell_commands=allow_shell_commands
    )


#: Plantilla conservadora: leo todo, escribo en el código, ejecuto sólo la batería de verificación.
#: Los borrados y el shell siguen pidiendo TTY, y el presupuesto corta un lazo desbocado.
POLICY_TEMPLATE: Final[Mapping[str, Any]] = {
    "version": POLICY_VERSION,
    "roots": ["."],
    "budget": {"max_ops": 80, "max_writes": 40, "max_deletes": 2, "max_seconds": 1800},
    "max_denials": 3,
    "allow": [
        {"tool": "read_file", "paths": ["**"], "requires_approval": False, "label": "leer el proyecto"},
        {"tool": "search_files", "paths": ["**"], "requires_approval": False},
        {"tool": "list_dir", "paths": ["**"], "requires_approval": False},
        {
            "tool": "write_file",
            "paths": ["src/**", "docs/**", "tests/**"],
            "requires_approval": False,
            "label": "escribir en el proyecto",
        },
        {"tool": "edit_file", "paths": ["src/**", "docs/**", "tests/**"], "requires_approval": False},
        {"tool": "append_file", "paths": ["docs/**", "knowledge_base/**"], "requires_approval": False},
        {"tool": "mkdir", "paths": ["src/**", "docs/**", "tests/**"], "requires_approval": False},
        {
            "tool": "run_command",
            "commands": ["git status", "git diff", "git log", "pytest", "python -m pytest", "ruff", "mypy"],
            "requires_approval": False,
            "label": "herramientas de verificación",
        },
    ],
    "deny": [
        {"tool": "delete_path", "label": "borrar siempre se pide a mano"},
        {"tool": "move_path", "label": "mover también"},
        {"tool": "copy_path", "label": "copiar también"},
    ],
}


def write_policy_template(path: Path, *, overwrite: bool = False) -> Path:
    """Deja la plantilla en `path` y devuelve la ruta. No pisa una política existente sin que se pida."""
    target = Path(path).expanduser()
    if target.exists() and not overwrite:
        raise ConfigurationError(
            f"Ya existe una política en {target}; quítala o pasa --force para reemplazarla",
            context={"path": str(target)},
        )
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(dict(POLICY_TEMPLATE), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    except OSError as exc:
        raise ConfigurationError(
            f"No se pudo escribir la plantilla de política: {exc}",
            context={"path": str(target)},
        ) from exc
    return target


def enforce_ceiling(command: str) -> None:
    """Corte duro compartido con el motor de procesos: lanzar esto no es negociable por política."""
    reason = command_ceiling(command)
    if reason is not None:
        raise ToolExecutionDeniedError(
            f"comando bloqueado por el techo duro de seguridad: {reason}",
            context={"command": normalize_command(command)[:160]},
        )
