"""Resolución de selectores e inventario del DOM, sin Playwright.

Este módulo **no** importa Playwright: sólo necesita objetos que se parezcan a una
`Page` (duck typing). Es a propósito, por dos razones:

1. Se puede probar la lógica sin navegador — justo lo que no tengo aquí.
2. Un selector no es código: vive en `selectors.toml` y se corrige con `./execute --probe`.

Cada grupo del TOML tiene varias estrategias ordenadas por preferencia. El bot usa
la primera que resuelve; el probe las prueba todas y te enseña cuál funcionó y cuáles
sobraron, más los candidatos reales de la página para que arregles el archivo a mano.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

__all__ = [
    "Estrategia",
    "Resultado",
    "cargar_grupos",
    "elegir_estrategia",
    "formatear_tabla",
    "inventario",
    "limpiar",
    "probar_grupo",
    "resolver",
]

#: Se recorre el DOM una sola vez y se devuelven los elementos "interesantes".
_INVENTARIO_JS = """() => {
  const salida = [];
  const nodos = document.querySelectorAll(
    "button, a, input, textarea, select, [role=button], [role=switch], " +
    "[role=combobox], [role=textbox], [contenteditable=true]"
  );
  for (const el of nodos) {
    const rect = el.getBoundingClientRect();
    const estilo = getComputedStyle(el);
    const nombre = (el.getAttribute("aria-label")
      || el.getAttribute("placeholder")
      || el.textContent
      || el.value || "");
    salida.push({
      tag: el.tagName.toLowerCase(),
      role: el.getAttribute("role") || (el.tagName === "INPUT" ? el.type : "") || "",
      name: String(nombre).trim().replace(/\\s+/g, " ").slice(0, 60),
      visible: !!(rect.width > 0 && rect.height > 0)
        && estilo.visibility !== "hidden" && estilo.display !== "none",
    });
  }
  return salida.slice(0, 80);
}"""

_KINDS = ("role", "aria", "placeholder", "text", "testid", "css")


class _Localizador(Protocol):
    """Lo mínimo que se usa de un localizador de Playwright."""

    def count(self) -> int: ...

    @property
    def first(self) -> Any: ...


class Page(Protocol):
    """La parte de `playwright.sync_api.Page` que este módulo necesita."""

    def get_by_role(self, role: str, name: str = "") -> Any: ...

    def get_by_label(self, text: str) -> Any: ...

    def get_by_placeholder(self, text: str) -> Any: ...

    def get_by_text(self, text: str) -> Any: ...

    def get_by_test_id(self, test_id: str) -> Any: ...

    def locator(self, selector: str) -> Any: ...

    def evaluate(self, script: str) -> Any: ...


def limpiar(texto: str, largo: int = 60) -> str:
    """Una línea, sin saltos ni espacios de más: para la tabla y el log."""
    return " ".join(str(texto).split())[:largo]


@dataclass(frozen=True, slots=True)
class Estrategia:
    """Una forma de encontrar un elemento. `kind` manda, el resto son sus argumentos."""

    kind: str = ""
    rol: str = ""
    nombre: str = ""
    etiqueta: str = ""
    texto: str = ""
    selector: str = ""

    @classmethod
    def desde(cls, datos: Mapping[str, Any]) -> Estrategia:
        return cls(
            kind=str(datos.get("kind") or "").strip().lower(),
            rol=str(datos.get("role") or "").strip(),
            nombre=str(datos.get("name") or "").strip(),
            etiqueta=str(datos.get("label") or "").strip(),
            texto=str(datos.get("text") or datos.get("placeholder") or "").strip(),
            selector=str(datos.get("selector") or datos.get("id") or "").strip(),
        )

    def descripcion(self) -> str:
        if self.kind == "role":
            base = f"role={self.rol or '?'}"
            return f'{base} name="{self.nombre}"' if self.nombre else base
        if self.kind == "aria":
            return f'aria-label="{self.etiqueta}"'
        if self.kind == "placeholder":
            return f'placeholder="{self.texto}"'
        if self.kind == "text":
            return f'texto="{self.texto}"'
        if self.kind == "testid":
            return f'testid="{self.selector}"'
        if self.kind == "css":
            return f"css={self.selector}"
        return f"(kind desconocido: {self.kind or 'vacío'})"

    def localizador(self, page: Page) -> Any:
        """Traduce la estrategia a un localizador. Falla claro si el kind no existe."""
        if self.kind == "role":
            if not self.rol:
                raise ValueError('estrategia "role" sin role')
            return page.get_by_role(self.rol, self.nombre) if self.nombre else page.get_by_role(self.rol)
        if self.kind == "aria":
            return page.get_by_label(self.etiqueta)
        if self.kind == "placeholder":
            return page.get_by_placeholder(self.texto)
        if self.kind == "text":
            return page.get_by_text(self.texto)
        if self.kind == "testid":
            return page.get_by_test_id(self.selector)
        if self.kind == "css":
            return page.locator(self.selector)
        raise ValueError(f"kind de selector no soportado: {self.kind!r} (usa {', '.join(_KINDS)})")


@dataclass(frozen=True, slots=True)
class Resultado:
    """Qué pasó al probar una estrategia contra la página real."""

    grupo: str
    descripcion: str
    estrategia: Estrategia
    resuelve: bool = False
    encontrados: int = 0
    visible: bool = False
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.resuelve and self.encontrados > 0


def resolver(page: Page, grupo: str, descripcion: str, estrategia: Estrategia) -> Resultado:
    """Prueba UNA estrategia. Nunca lanza: un selector roto se reporta, no tumba el probe."""
    try:
        localizador = estrategia.localizador(page)
        encontrados = int(localizador.count())
    except Exception as exc:  # noqa: BLE001 — aquí un fallo es un dato, no un error
        return Resultado(grupo, descripcion, estrategia, error=limpiar(f"{type(exc).__name__}: {exc}", 120))
    visible = False
    error = ""
    if encontrados:
        try:
            visible = bool(localizador.first.is_visible())
        except Exception as exc:  # noqa: BLE001 — medir visibilidad no puede tumbar el probe
            error = limpiar(f"visible?: {type(exc).__name__}: {exc}", 120)
    return Resultado(grupo, descripcion, estrategia, encontrados > 0, encontrados, visible, error)


def probar_grupo(page: Page, grupo: str, datos: Mapping[str, Any]) -> list[Resultado]:
    """Prueba TODAS las estrategias de un grupo (lo que hace el probe)."""
    descripcion = limpiar(str(datos.get("descripcion") or ""), 48)
    crudas = datos.get("estrategias") or []
    if not isinstance(crudas, list):
        return []
    return [
        resolver(page, grupo, descripcion, Estrategia.desde(item if isinstance(item, Mapping) else {}))
        for item in crudas
    ]


def elegir_estrategia(page: Page, grupo: str, datos: Mapping[str, Any]) -> Resultado | None:
    """La primera estrategia que resuelve (lo que usa el bot). `None` si ninguna.

    Se prueba de una en una y se corta en cuanto una funciona: el bot hace el mínimo
    trabajo sobre el DOM, no las ocho preguntas de golpe para quedarse con la primera.
    """
    descripcion = limpiar(str(datos.get("descripcion") or ""), 48)
    crudas = datos.get("estrategias") or []
    if not isinstance(crudas, list):
        return None
    for item in crudas:
        resultado = resolver(page, grupo, descripcion, Estrategia.desde(item if isinstance(item, Mapping) else {}))
        if resultado.ok:
            return resultado
    return None


def cargar_grupos(selectores: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    """Los grupos del TOML, en orden, ignorando lo que no sea un grupo."""
    return {
        str(nombre): datos
        for nombre, datos in selectores.items()
        if isinstance(datos, Mapping) and "estrategias" in datos
    }


def inventario(page: Page) -> list[dict[str, Any]]:
    """Lo que sí hay en la página: para corregir el TOML mirando, no adivinando."""
    try:
        crudo = page.evaluate(_INVENTARIO_JS)
    except Exception:  # noqa: BLE001 — sin inventario también se puede diagnosticar
        return []
    if not isinstance(crudo, list):
        return []
    salida: list[dict[str, Any]] = []
    for item in crudo:
        if not isinstance(item, Mapping):
            continue
        salida.append(
            {
                "tag": str(item.get("tag") or ""),
                "role": str(item.get("role") or ""),
                "name": limpiar(str(item.get("name") or ""), 60),
                "visible": bool(item.get("visible")),
            }
        )
    return salida


def formatear_tabla(resultados: Sequence[Resultado], *, ancho: int = 92) -> str:
    """La tabla del probe: grupo, estrategia, y si resolvió."""
    lineas: list[str] = []
    cabecera = f"{'GRUPO':<21}{'ESTRATEGIA':<42}{'RESUELVE':<10}{'N':<4}VIS"
    lineas.append(cabecera)
    lineas.append("-" * ancho)
    grupo_previo = ""
    for resultado in resultados:
        grupo = "" if resultado.grupo == grupo_previo else resultado.grupo
        grupo_previo = resultado.grupo
        marca = "sí" if resultado.ok else ("error" if resultado.error else "no")
        lineas.append(
            f"{limpiar(grupo, 20):<21}{limpiar(resultado.estrategia.descripcion(), 41):<42}"
            f"{marca:<10}{resultado.encontrados:<4}{'sí' if resultado.visible else ''}"
        )
        if resultado.error:
            lineas.append(f"{'':<21}↳ {limpiar(resultado.error, ancho - 23)}")
    return "\n".join(lineas)


def formatear_inventario(items: Sequence[Mapping[str, Any]], *, ancho: int = 92) -> str:
    """Los candidatos reales, para arreglar `selectors.toml` en dos minutos."""
    if not items:
        return "  (la página no devolvió elementos utilizables)"
    lineas = [f"{'TAG':<10}{'ROLE':<14}{'VIS':<5}NOMBRE", "-" * ancho]
    lineas.extend(
        f"{str(item.get('tag') or '')[:9]:<10}{str(item.get('role') or '')[:13]:<14}"
        f"{'sí' if item.get('visible') else 'no':<5}{limpiar(str(item.get('name') or ''), ancho - 30)}"
        for item in items
    )
    return "\n".join(lineas)
