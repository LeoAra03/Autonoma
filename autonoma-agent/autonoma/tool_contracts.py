"""Contrato único de herramientas: la misma tabla genera el esquema y el validador.

- Las 13 especificaciones se construyen **una vez** al importar (`TOOL_SPECS`) y se
  indexan en un `dict` (`spec_for`): `validate_arguments` pasó de O(herramientas)
  reconstruyendo diccionarios en cada llamada a O(1) sobre una tabla inmutable.
- Tipos y límites de campo son datos declarativos, no ramas `if` de un builder.
- Errores tipados con el parámetro implicado en el contexto del log.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Final, Literal

from autonoma.errors import ToolContractError

__all__ = [
    "LOCAL_TOOLS",
    "TOOL_SPECS",
    "ToolField",
    "ToolSpec",
    "ToolValidationError",
    "parse_arguments",
    "spec_for",
    "tool_schemas",
    "tool_names",
    "validate_arguments",
]

_MAX_ARGUMENTS_CHARS: Final[int] = 100_000
_MAX_CONTENT_CHARS: Final[int] = 80_000
_MAX_SCALAR_CHARS: Final[int] = 4_096
_FORBIDDEN_CHARS: Final[str] = "\x00"
_BLANK_FORBIDDEN_KEYS: Final[frozenset[str]] = frozenset({"path", "src", "dst", "query", "title", "command", "url"})

FieldType = Literal["string", "integer", "number", "boolean"]

LOCAL_TOOLS: Final[frozenset[str]] = frozenset(
    {"read_file", "write_file", "copy_path", "move_path", "delete_path", "list_dir", "mkdir", "run_command"}
)


class ToolValidationError(ToolContractError):
    """Argumentos no conformes: ninguna herramienta debe ejecutarse."""


_TYPE_TUPLE: Final[Mapping[str, tuple[type, ...]]] = MappingProxyType(
    {"string": (str,), "boolean": (bool,), "integer": (int,), "number": (int, float)}
)


def _is_finite_number(value: Any) -> bool:
    """Enteros exactos siempre finitos; flotales NaN/inf se rechazan explícitamente."""
    if isinstance(value, float):
        return math.isfinite(value)
    return isinstance(value, int)


@dataclass(frozen=True, slots=True)
class ToolField:
    """Definición atómica de un parámetro: fuente única de verdad."""

    name: str
    type: FieldType
    description: str = ""
    required: bool = True
    max_length: int | None = None
    minimum: float | None = None
    maximum: float | None = None

    def as_schema(self) -> dict[str, Any]:
        schema: dict[str, Any] = {"type": self.type}
        if self.description:
            schema["description"] = self.description
        if self.type == "string":
            schema["maxLength"] = self.max_length or _MAX_SCALAR_CHARS
            schema["minLength"] = 0 if self.name not in _BLANK_FORBIDDEN_KEYS else 1
        elif self.type in ("integer", "number"):
            schema["minimum"] = self.minimum
            schema["maximum"] = self.maximum
        return schema

    def reject_reason(self, value: Any) -> str | None:
        """Motivo del rechazo, o `None` cuando el valor es aceptable."""
        expected = _TYPE_TUPLE[self.type]
        if self.type == "boolean":
            if not isinstance(value, bool):
                return f"Tipo inválido para {self.name}: se requiere boolean"
        elif isinstance(value, bool) or not isinstance(value, expected):
            return f"Tipo inválido para {self.name}: se requiere {self.type}"
        if self.type == "string":
            text = str(value)
            if len(text) > (self.max_length or _MAX_SCALAR_CHARS) or _FORBIDDEN_CHARS in text:
                return f"Tamaño o contenido inválido para {self.name}"
            if self.name in _BLANK_FORBIDDEN_KEYS and not text.strip():
                return f"{self.name} no puede estar vacío"
        elif not _is_finite_number(value):
            return f"{self.name} debe ser finito"
        elif self.minimum is not None and self.maximum is not None and not self.minimum <= value <= self.maximum:
            return f"{self.name} debe estar entre {self.minimum:g} y {self.maximum:g}"
        return None

    def validate(self, value: Any) -> Any:
        reason = self.reject_reason(value)
        if reason is not None:
            raise ToolValidationError(reason, context={"tool": self.name})
        return value


def _string(name: str, description: str = "", *, max_length: int | None = None, required: bool = True) -> ToolField:
    return ToolField(name=name, type="string", description=description, max_length=max_length, required=required)


def _number(name: str, description: str, minimum: float, maximum: float, *, integer: bool = True) -> ToolField:
    return ToolField(
        name=name,
        type="integer" if integer else "number",
        description=description,
        minimum=minimum,
        maximum=maximum,
        required=False,
    )


def _flag(name: str, description: str) -> ToolField:
    return ToolField(name=name, type="boolean", description=description, required=False)


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """Herramienta: nombre, descripción para el modelo y campos admitidos."""

    name: str
    description: str
    fields: tuple[ToolField, ...]
    local: bool = False

    @property
    def by_field(self) -> Mapping[str, ToolField]:
        return _fields_index(self.fields)

    @property
    def required(self) -> tuple[str, ...]:
        return tuple(field.name for field in self.fields if field.required)

    def as_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {field.name: field.as_schema() for field in self.fields},
                    "required": list(self.required),
                    "additionalProperties": False,
                },
            },
        }

    def validate(self, args: Mapping[str, Any]) -> dict[str, Any]:
        """Rechaza de más, de menos y de tipo equivocado; devuelve un dict nuevo."""
        index = self.by_field
        unknown = sorted(set(args) - set(index))
        if unknown:
            raise ToolValidationError("Parámetros desconocidos", context={"tool": self.name, "unknown": ",".join(unknown)})
        missing = sorted(set(self.required) - set(args))
        if missing:
            raise ToolValidationError("Faltan parámetros obligatorios", context={"tool": self.name, "missing": ",".join(missing)})
        clean: dict[str, Any] = {}
        for key, value in args.items():
            field = index[key]
            reason = field.reject_reason(value)
            if reason is not None:
                raise ToolValidationError(reason, context={"tool": self.name, "argument": key})
            clean[key] = value
        return clean


_field_indexes: dict[tuple[tuple[str, str], ...], Mapping[str, ToolField]] = {}


def _fields_index(fields: tuple[ToolField, ...]) -> Mapping[str, ToolField]:
    """Índice memoizado por firma de campos: evita reconstruir el `dict` por llamada."""
    signature = tuple((field.name, field.type) for field in fields)
    cached = _field_indexes.get(signature)
    if cached is None:
        cached = MappingProxyType({field.name: field for field in fields})
        _field_indexes[signature] = cached
    return cached


_TIMEOUT = _number("timeout", "Segundos máximos", 0.01, 300.0, integer=False)

TOOL_SPECS: Final[tuple[ToolSpec, ...]] = (
    ToolSpec(
        name="web_search",
        description="Busca en la web (Brave) y extrae contenido de las páginas top. Guarda un .md en knowledge_base.",
        fields=(
            _string("query", "Consulta de búsqueda"),
            _number("fetch_pages", "Cuántas páginas extraer (0-5). Por defecto 3.", 0, 5),
        ),
    ),
    ToolSpec(name="fetch_url", description="Descarga y extrae el texto visible de una URL concreta.",
             fields=(_string("url"),)),
    ToolSpec(name="save_knowledge", description="Guarda una nota en ./knowledge_base/ como Markdown.",
             fields=(_string("title"), _string("content", max_length=_MAX_CONTENT_CHARS))),
    ToolSpec(name="read_knowledge", description="Lee o busca notas ya guardadas en knowledge_base.",
             fields=(_string("query", "Nombre de archivo o texto a buscar"),)),
    ToolSpec(name="list_knowledge", description="Lista las notas recientes de knowledge_base.", fields=()),
    ToolSpec(name="read_file", description="Lee un archivo de texto del disco.", fields=(_string("path"),), local=True),
    ToolSpec(
        name="write_file",
        description="Crea o sobrescribe un archivo de texto. Crea directorios padre si hace falta.",
        fields=(
            _string("path"),
            _string("content", max_length=_MAX_CONTENT_CHARS),
            _flag("force", "Compatibilidad; no permite escribir en rutas protegidas"),
        ),
        local=True,
    ),
    ToolSpec(name="copy_path", description="Copia un archivo o carpeta.",
             fields=(_string("src"), _string("dst"), _flag("force", "")), local=True),
    ToolSpec(name="move_path", description="Mueve o renombra un archivo o carpeta.",
             fields=(_string("src"), _string("dst"), _flag("force", "")), local=True),
    ToolSpec(name="delete_path", description="Elimina un archivo o carpeta. Siempre bloqueado en rutas críticas del SO.",
             fields=(_string("path"), _flag("force", "")), local=True),
    ToolSpec(name="list_dir", description="Lista el contenido de un directorio.", fields=(_string("path"),), local=True),
    ToolSpec(name="mkdir", description="Crea un directorio (y padres).", fields=(_string("path"),), local=True),
    ToolSpec(
        name="run_command",
        description="Ejecuta un programa o comando de shell en la máquina local y devuelve stdout/stderr.",
        fields=(_string("command"), _string("cwd", "Directorio de trabajo opcional", required=False), _TIMEOUT),
        local=True,
    ),
)

_SPEC_BY_NAME: Final[Mapping[str, ToolSpec]] = MappingProxyType({spec.name: spec for spec in TOOL_SPECS})
_CACHED_SCHEMAS: Final[list[dict[str, Any]]] = [spec.as_schema() for spec in TOOL_SPECS]
_LOCAL_SCHEMA_TOOLS: Final[frozenset[str]] = frozenset(spec.name for spec in TOOL_SPECS if spec.local)


def tool_names() -> tuple[str, ...]:
    return tuple(_SPEC_BY_NAME)


def spec_for(name: str) -> ToolSpec:
    """Búsqueda O(1) en la tabla cacheada; error tipado si la herramienta no existe."""
    spec = _SPEC_BY_NAME.get(name)
    if spec is None:
        raise ToolValidationError("Herramienta desconocida", context={"tool": name or "<vacío>"})
    return spec


def tool_schemas() -> list[dict[str, Any]]:
    """Esquema enviado al modelo: lista precomputada, copia superficial defensiva."""
    return [dict(schema) for schema in _CACHED_SCHEMAS]


def schemas_payload() -> list[dict[str, Any]]:
    """Versión para serializar una sola vez por turno (sin copias)."""
    return _CACHED_SCHEMAS


def parse_arguments(raw: Any) -> dict[str, Any]:
    """JSON estricto: sin claves duplicadas silenciosas y sin objetos no dict."""
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str):
        if len(raw) > _MAX_ARGUMENTS_CHARS:
            raise ToolValidationError("Argumentos demasiado grandes")
        try:
            decoded = json.loads(raw)
        except (ValueError, RecursionError):
            raise ToolValidationError("Los argumentos deben ser JSON válido") from None
        if not isinstance(decoded, dict):
            raise ToolValidationError("Los argumentos deben ser un objeto JSON")
        return dict(decoded)
    raise ToolValidationError("Los argumentos deben ser un objeto JSON")


def validate_arguments(name: str, raw: Any) -> dict[str, Any]:
    """Valores ya tipados y acotados, listos para ejecutar sin segundas conversiones."""
    return spec_for(name).validate(parse_arguments(raw))
