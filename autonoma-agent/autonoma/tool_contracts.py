"""Contrato único de herramientas: esquema enviado al modelo y validación local."""
from __future__ import annotations
import json
import math
from typing import Any

LOCAL_TOOLS = frozenset({"read_file", "write_file", "copy_path", "move_path",
                         "delete_path", "list_dir", "mkdir", "run_command"})


class ToolValidationError(ValueError):
    """Argumentos no conformes; ninguna herramienta debe ejecutarse."""

def tool_schemas() -> list[dict[str, Any]]:
    def fn(name: str, description: str, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
        for key, prop in properties.items():
            if prop['type'] == 'string':
                prop['maxLength'] = 80_000 if key == 'content' else 4096
                if key != 'content':
                    prop['minLength'] = 1
            if prop['type'] in {'integer', 'number'}:
                prop['minimum'], prop['maximum'] = (0, 5) if key == 'fetch_pages' else (0.01, 300)
        return {
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                    "additionalProperties": False,
                },
            },
        }

    return [
        fn(
            "web_search",
            "Busca en la web (Brave) y extrae contenido de las páginas top. Guarda un .md en knowledge_base.",
            {
                "query": {"type": "string", "description": "Consulta de búsqueda"},
                "fetch_pages": {
                    "type": "integer",
                    "description": "Cuántas páginas extraer (0-5). Por defecto 3.",
                },
            },
            ["query"],
        ),
        fn(
            "fetch_url",
            "Descarga y extrae el texto visible de una URL concreta.",
            {"url": {"type": "string"}},
            ["url"],
        ),
        fn(
            "save_knowledge",
            "Guarda una nota en ./knowledge_base/ como Markdown.",
            {
                "title": {"type": "string"},
                "content": {"type": "string"},
            },
            ["title", "content"],
        ),
        fn(
            "read_knowledge",
            "Lee o busca notas ya guardadas en knowledge_base.",
            {
                "query": {
                    "type": "string",
                    "description": "Nombre de archivo o texto a buscar",
                }
            },
            ["query"],
        ),
        fn(
            "list_knowledge",
            "Lista las notas recientes de knowledge_base.",
            {},
            [],
        ),
        fn(
            "read_file",
            "Lee un archivo de texto del disco.",
            {"path": {"type": "string"}},
            ["path"],
        ),
        fn(
            "write_file",
            "Crea o sobrescribe un archivo de texto. Crea directorios padre si hace falta.",
            {
                "path": {"type": "string"},
                "content": {"type": "string"},
                "force": {
                    "type": "boolean",
                    "description": "Compatibilidad; no permite escribir en rutas protegidas",
                },
            },
            ["path", "content"],
        ),
        fn(
            "copy_path",
            "Copia un archivo o carpeta.",
            {
                "src": {"type": "string"},
                "dst": {"type": "string"},
                "force": {"type": "boolean"},
            },
            ["src", "dst"],
        ),
        fn(
            "move_path",
            "Mueve o renombra un archivo o carpeta.",
            {
                "src": {"type": "string"},
                "dst": {"type": "string"},
                "force": {"type": "boolean"},
            },
            ["src", "dst"],
        ),
        fn(
            "delete_path",
            "Elimina un archivo o carpeta. Siempre bloqueado en rutas críticas del SO.",
            {
                "path": {"type": "string"},
                "force": {"type": "boolean"},
            },
            ["path"],
        ),
        fn(
            "list_dir",
            "Lista el contenido de un directorio.",
            {"path": {"type": "string"}},
            ["path"],
        ),
        fn(
            "mkdir",
            "Crea un directorio (y padres).",
            {"path": {"type": "string"}},
            ["path"],
        ),
        fn(
            "run_command",
            "Ejecuta un programa o comando de shell en la máquina local y devuelve stdout/stderr.",
            {
                "command": {"type": "string"},
                "cwd": {"type": "string", "description": "Directorio de trabajo opcional"},
                "timeout": {"type": "number", "description": "Segundos máximos"},
            },
            ["command"],
        ),
    ]


def parse_arguments(raw: Any) -> dict[str, Any]:
    if isinstance(raw, str):
        if len(raw) > 100_000:
            raise ToolValidationError("Argumentos demasiado grandes")
        try:
            raw = json.loads(raw)
        except (ValueError, RecursionError):
            raise ToolValidationError("Los argumentos deben ser JSON válido") from None
    if not isinstance(raw, dict):
        raise ToolValidationError("Los argumentos deben ser un objeto JSON")
    return dict(raw)


def validate_arguments(name: str, raw: Any) -> dict[str, Any]:
    schema = next((t["function"]["parameters"] for t in tool_schemas()
                   if t["function"]["name"] == name), None)
    if schema is None:
        raise ToolValidationError("Herramienta desconocida")
    args = parse_arguments(raw)
    properties = schema["properties"]
    if set(args) - set(properties):
        raise ToolValidationError("Parámetros desconocidos")
    if set(schema["required"]) - set(args):
        raise ToolValidationError("Faltan parámetros obligatorios")
    types = {"string": (str,), "boolean": (bool,), "integer": (int,), "number": (int, float)}
    for key, value in args.items():
        kind = properties[key]["type"]
        if type(value) not in types[kind]:
            raise ToolValidationError(f"Tipo inválido para {key}: se requiere {kind}")
        if isinstance(value, str):
            maximum = properties[key]["maxLength"]
            if len(value) > maximum or "\x00" in value:
                raise ToolValidationError(f"Tamaño o contenido inválido para {key}")
            if key != "content" and not value.strip():
                raise ToolValidationError(f"{key} no puede estar vacío")
        if kind in {"integer", "number"}:
            if not math.isfinite(value):
                raise ToolValidationError(f"{key} debe ser finito")
            low, high = properties[key]["minimum"], properties[key]["maximum"]
            if not low <= value <= high:
                raise ToolValidationError(f"{key} debe estar entre {low} y {high}")
    return args
