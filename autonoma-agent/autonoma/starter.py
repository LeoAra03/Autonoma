"""Arranque asistido: en qué repositorio y con qué chat, antes de irse a segundo plano.

Si vas a dejar al lazo trabajando solo, al menos que arranque en el sitio correcto.
Dos preguntas —dónde trabaja y con qué conversación— y después ya no molesta más:
a partir de ahí manda `--supervise` (con `--hasta-que-pare` si no quieres topes).

Aquí sólo hay **descubrimiento y preguntas**. Lo único que se escribe en disco es
`repos.json`, y únicamente si el usuario pide recordar una ruta nueva: un fallo ahí
nunca puede tumbar el arranque.

Los repositorios salen de tres sitios, en este orden:

1. la carpeta actual (lo de siempre, sin preguntar nada);
2. el registro `repos.json` de la raíz de datos (nombre → carpeta, escrito a mano);
3. un escaneo de carpetas con `.git` bajo las rutas de `AUTONOMA_REPOS` / `--repos-dir`.

El escaneo es deliberadamente corto (dos niveles, tope de directorios) porque está
pensado para una carpeta madre de proyectos, no para recorrer un disco entero.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterable, Sequence
from contextlib import suppress
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Final, Protocol

from autonoma.sessions import SessionInfo

__all__ = [
    "ARRANQUE_ENVVAR",
    "REGISTRY_FILENAME",
    "WORKSPACE_ENVVAR",
    "Arranque",
    "OpcionRepo",
    "aplicar_arranque",
    "bases_del_entorno",
    "cargar_registro",
    "descubrir_repos",
    "escanear_repos",
    "formatear_chat",
    "preguntar_chat",
    "preguntar_repo",
    "recordar_repo",
    "resolver_arranque",
]

REGISTRY_FILENAME: Final[str] = "repos.json"
#: Carpetas madre donde buscar repos, separadas por el separador del sistema.
ARRANQUE_ENVVAR: Final[str] = "AUTONOMA_REPOS"
#: Ajuste que apunta el trabajo al repositorio elegido (lo aplica la CLI).
WORKSPACE_ENVVAR: Final[str] = "WORKSPACE_DIR"

_MAX_OPCIONES: Final[int] = 9
_MAX_REPOS: Final[int] = 40
_MAX_DIRECTORIOS: Final[int] = 2_000
_PROFUNDIDAD: Final[int] = 2
_ANCHO: Final[int] = 22


# El puerto de consola tal y como lo usa la CLI: basta con poder escribir y leer.
class _Consola(Protocol):
    def print(self, *objects: Any, **kwargs: Any) -> None: ...

    def input(self, prompt: str = "") -> str: ...


@dataclass(frozen=True, slots=True)
class OpcionRepo:
    """Una carpeta donde el lazo puede trabajar."""

    nombre: str
    ruta: Path
    origen: str  # "registro", "git" o "actual"

    def linea(self) -> str:
        return f"{self.nombre[:_ANCHO]:<{_ANCHO}} {_acortar(str(self.ruta))}  ({self.origen})"


@dataclass(frozen=True, slots=True)
class Arranque:
    """Resultado del asistente: dónde trabaja el lazo y con qué conversación."""

    repo: Path | None = None  # `None` = déjalo en la carpeta actual
    chat: str | None = None  # `None` = chat nuevo
    preguntado: bool = False

    def resumen(self) -> str:
        repo = str(self.repo) if self.repo is not None else "la carpeta actual"
        chat = f"chat {self.chat}" if self.chat is not None else "chat nuevo"
        return f"repositorio: {repo} · {chat}"


def _acortar(texto: str, limite: int = 48) -> str:
    return texto if len(texto) <= limite else f"…{texto[-(limite - 1):]}"


def cargar_registro(path: Path) -> tuple[OpcionRepo, ...]:
    """Lee `repos.json`. Acepta `{"repos": [...]}` o una lista directa; nunca falla.

    Un archivo escrito a mano se equivoca, y equivocarse no puede impedir arrancar:
    lo que no se entiende se salta.
    """
    try:
        crudo = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ()
    items = crudo.get("repos", []) if isinstance(crudo, dict) else crudo
    if not isinstance(items, list):
        return ()
    opciones: list[OpcionRepo] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        ruta = item.get("ruta") or item.get("path") or ""
        nombre = item.get("nombre") or item.get("name") or ""
        if not isinstance(ruta, str) or not ruta.strip():
            continue
        expandida = Path(ruta.strip()).expanduser()
        opciones.append(
            OpcionRepo(
                nombre=str(nombre).strip() or expandida.name or str(expandida),
                ruta=expandida,
                origen="registro",
            )
        )
    return tuple(opciones)


def escanear_repos(
    base: Path, *, profundidad: int = _PROFUNDIDAD, tope: int = _MAX_REPOS
) -> tuple[OpcionRepo, ...]:
    """Carpetas con `.git` bajo `base`. Corto y silencioso: un disco ajeno no es asunto nuestro."""
    if not base.is_dir():
        return ()
    encontrados: list[OpcionRepo] = []
    vista = 0
    frontera: list[tuple[Path, int]] = [(base, 0)]
    while frontera and len(encontrados) < tope and vista < _MAX_DIRECTORIOS:
        actual, nivel = frontera.pop(0)
        try:
            hijos = sorted(actual.iterdir())
        except OSError:
            continue
        for hijo in hijos:
            vista += 1
            if not hijo.is_dir() or hijo.name.startswith("."):
                continue
            if (hijo / ".git").exists():
                encontrados.append(OpcionRepo(nombre=hijo.name, ruta=hijo, origen="git"))
                continue
            if nivel + 1 < profundidad and vista < _MAX_DIRECTORIOS:
                frontera.append((hijo, nivel + 1))
    return tuple(encontrados[:tope])


def descubrir_repos(
    *,
    registro: Path | None = None,
    bases: Iterable[Path] = (),
    actual: Path | None = None,
    tope: int = _MAX_OPCIONES,
) -> tuple[OpcionRepo, ...]:
    """Repos actuales, del registro y escaneados, sin repetidos y en ese orden."""
    aqui = (actual or Path.cwd()).resolve()
    opciones: list[OpcionRepo] = [
        OpcionRepo(nombre=aqui.name or str(aqui), ruta=aqui, origen="actual")
    ]
    vistos: set[Path] = {aqui}

    candidatos: list[OpcionRepo] = []
    if registro is not None:
        candidatos.extend(cargar_registro(registro))
    for base in bases:
        candidatos.extend(escanear_repos(base))

    for candidato in candidatos:
        try:
            resuelta = candidato.ruta.expanduser().resolve()
        except OSError:
            continue
        if resuelta in vistos:
            continue
        vistos.add(resuelta)
        opciones.append(replace(candidato, ruta=resuelta))
    return tuple(opciones[:tope])


def _elegir(consola: _Consola, titulo: str, lineas: Sequence[str], defecto: int = 0) -> int:
    """Pregunta numerada: devuelve el índice elegido (0-based). Entrada vacía = defecto."""
    consola.print(titulo)
    for numero, linea in enumerate(lineas, start=1):
        consola.print(f"  {numero}  {linea}")
    raw = ""
    with suppress(EOFError):
        raw = (consola.input(f"Elige [{defecto + 1}]: ") or "").strip()
    if not raw:
        return defecto
    if raw.isdigit() and 1 <= int(raw) <= len(lineas):
        return int(raw) - 1
    consola.print(f"'{raw}' no está en la lista: uso la opción {defecto + 1}.")
    return defecto


def preguntar_repo(consola: _Consola, opciones: Sequence[OpcionRepo]) -> Path | None:
    """¿Dónde trabajo? Devuelve la carpeta elegida, o `None` para quedarse donde está."""
    if not opciones:
        return None
    lineas = [opcion.linea() for opcion in opciones] + ["otra ruta…"]
    indice = _elegir(consola, "¿Sobre qué repositorio trabajo?", lineas)
    if indice < len(opciones):
        return opciones[indice].ruta
    raw = ""
    with suppress(EOFError):
        raw = (consola.input("Ruta: ") or "").strip()
    if not raw:
        return None
    destino = Path(raw).expanduser()
    try:
        destino = destino.resolve()
    except OSError:
        consola.print(f"No puedo resolver '{raw}': me quedo en la carpeta actual.")
        return None
    if not destino.is_dir():
        consola.print(f"'{destino}' no es una carpeta: me quedo en la carpeta actual.")
        return None
    return destino


def formatear_chat(info: SessionInfo) -> str:
    """Una línea por chat: id, cuánto pesa, cuándo fue y por dónde iba."""
    preview = _acortar(info.preview.strip() or "(sin texto)", 44)
    return f"{info.session_id} · {info.turns} turnos · {_cuando(info.updated_at)} · {preview}"


def _cuando(momento: float) -> str:
    delta = max(0.0, time.time() - momento)
    if delta < 60:
        return "ahora mismo"
    if delta < 3600:
        return f"hace {int(delta // 60)} min"
    if delta < 86_400:
        return f"hace {int(delta // 3600)} h"
    return f"hace {int(delta // 86_400)} d"


def preguntar_chat(consola: _Consola, chats: Sequence[SessionInfo]) -> str | None:
    """¿Chat nuevo o uno existente? Devuelve el id a retomar, o `None` para empezar uno nuevo."""
    if not chats:
        return None
    lineas = ["chat nuevo", *[formatear_chat(info) for info in chats]]
    indice = _elegir(consola, "¿Chat nuevo o uno existente?", lineas, defecto=0)
    return None if indice == 0 else chats[indice - 1].session_id


def recordar_repo(registro: Path, opcion: OpcionRepo) -> Path | None:
    """Añade una carpeta al registro para que aparezca la próxima vez. Falla en silencio."""
    existentes = list(cargar_registro(registro))
    if any(item.ruta == opcion.ruta for item in existentes):
        return registro
    payload = [
        {"nombre": item.nombre, "ruta": str(item.ruta)}
        for item in [*existentes, replace(opcion, origen="registro")]
    ]
    try:
        registro.parent.mkdir(parents=True, exist_ok=True)
        registro.write_text(
            json.dumps({"repos": payload}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    except OSError:
        return None
    return registro


def resolver_arranque(
    consola: _Consola,
    *,
    registro: Path | None = None,
    bases: Iterable[Path] = (),
    chats: Sequence[SessionInfo] = (),
    interactivo: bool = True,
) -> Arranque:
    """Las dos preguntas, de una vez. Sin terminal interactiva no pregunta: no adivina a ciegas."""
    if not interactivo:
        return Arranque()
    repos = descubrir_repos(registro=registro, bases=bases)
    repo = preguntar_repo(consola, repos)
    if repo is not None and registro is not None and all(item.ruta != repo for item in repos):
        with suppress(EOFError):
            raw = (consola.input(f"¿Recordar '{repo.name}' para la próxima? (s/N): ") or "").strip()
            if raw.lower().startswith("s"):
                guardado = recordar_repo(
                    registro, OpcionRepo(nombre=repo.name, ruta=repo, origen="registro")
                )
                if guardado is not None:
                    consola.print(f"guardado en {guardado.name}")
    return Arranque(repo=repo, chat=preguntar_chat(consola, chats), preguntado=True)


def aplicar_arranque(arranque: Arranque, overrides: dict[str, str], consola: _Consola | None = None) -> dict[str, str]:
    """Lleva el proceso al repositorio elegido y lo anota en los ajustes.

    `chdir` es la parte importante: es lo que hace que las rutas relativas del agente,
    los comandos y las raíces de la política se refieran al repo elegido y no a otra
    carpeta. Las claves y los informes siguen en la raíz de datos, que es absoluta.
    """
    if arranque.repo is None:
        return overrides
    destino = arranque.repo.expanduser().resolve()
    os.chdir(destino)
    overrides[WORKSPACE_ENVVAR] = str(destino)
    if consola is not None:
        consola.print(
            f"trabajando en {destino}" + (f" · retomando {arranque.chat}" if arranque.chat else "")
        )
    return overrides


def bases_del_entorno(env: dict[str, str] | None = None, *, extra: Iterable[Path] = ()) -> tuple[Path, ...]:
    """Carpetas madre a escanear: las del entorno (`AUTONOMA_REPOS`) más las que pases."""
    valores = (env if env is not None else os.environ).get(ARRANQUE_ENVVAR, "")
    rutas = [Path(parte).expanduser() for parte in valores.split(os.pathsep) if parte.strip()]
    return (*rutas, *extra)
