"""Pruebas del sondeo sin navegador.

No hay Playwright aquí, y es a propósito: la lógica es nuestra, el DOM es de
arena.ai y cambia sin avisar. Lo que se prueba es que una estrategia se traduce
bien, que un selector roto se reporta en vez de tumbar nada, y que el TOML que
se entrega tiene la forma que prometemos.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import probe
import probing
import pytest

AQUI = Path(__file__).resolve().parents[1]


class LocalizadorFalso:
    """Un `Locator` de juguete: sabe cuántos elementos hay y si se ven."""

    def __init__(self, encontrados: int = 0, visible: bool = False) -> None:
        self._encontrados = encontrados
        self._visible = visible

    def count(self) -> int:
        return self._encontrados

    @property
    def first(self) -> LocalizadorFalso:
        return self

    def is_visible(self) -> bool:
        return self._visible


class PaginaFalsa:
    """Una `Page` de juguete: responde según un mapa y anota lo que se le pide."""

    def __init__(
        self,
        respuestas: dict[tuple[str, str], int] | None = None,
        *,
        visibles: set[tuple[str, str]] | None = None,
        inventario: list[dict[str, Any]] | None = None,
        falla_evaluate: bool = False,
    ) -> None:
        self.respuestas = respuestas or {}
        self.visibles = visibles or set()
        self.inventario = inventario or []
        self.falla_evaluate = falla_evaluate
        self.preguntas: list[tuple[str, str]] = []

    def _localizar(self, kind: str, valor: str) -> LocalizadorFalso:
        self.preguntas.append((kind, valor))
        return LocalizadorFalso(self.respuestas.get((kind, valor), 0), (kind, valor) in self.visibles)

    def get_by_role(self, role: str, name: str = "") -> LocalizadorFalso:
        return self._localizar("role", f"{role}:{name}")

    def get_by_label(self, text: str) -> LocalizadorFalso:
        return self._localizar("aria", text)

    def get_by_placeholder(self, text: str) -> LocalizadorFalso:
        return self._localizar("placeholder", text)

    def get_by_text(self, text: str) -> LocalizadorFalso:
        return self._localizar("text", text)

    def get_by_test_id(self, test_id: str) -> LocalizadorFalso:
        return self._localizar("testid", test_id)

    def locator(self, selector: str) -> LocalizadorFalso:
        return self._localizar("css", selector)

    def evaluate(self, script: str) -> Any:
        if self.falla_evaluate:
            raise RuntimeError("sin permiso")
        return self.inventario


def _grupo(**estrategias: dict[str, Any]) -> dict[str, Any]:
    return {"descripcion": "grupo de prueba", "estrategias": list(estrategias.values())}


# ─────────────────────────────────────────────────────────────── la estrategia
def test_estrategia_traduce_los_alias_del_toml() -> None:
    estrategia = probing.Estrategia.desde({"kind": "role", "role": "button", "name": "New chat"})
    assert (estrategia.kind, estrategia.rol, estrategia.nombre) == ("role", "button", "New chat")
    assert "button" in estrategia.descripcion() and "New chat" in estrategia.descripcion()

    otra = probing.Estrategia.desde({"kind": "aria", "label": "Send"})
    assert otra.etiqueta == "Send"
    assert probing.Estrategia.desde({"kind": "placeholder", "placeholder": "Ask"}).texto == "Ask"
    assert probing.Estrategia.desde({"kind": "testid", "id": "x"}).selector == "x"


def test_cada_kind_tiene_su_localizador() -> None:
    pagina = PaginaFalsa()
    casos = [
        ({"kind": "role", "role": "button", "name": "b"}, "role", "button:b"),
        ({"kind": "aria", "label": "l"}, "aria", "l"),
        ({"kind": "placeholder", "placeholder": "p"}, "placeholder", "p"),
        ({"kind": "text", "text": "t"}, "text", "t"),
        ({"kind": "testid", "id": "i"}, "testid", "i"),
        ({"kind": "css", "selector": "main"}, "css", "main"),
    ]
    for datos, kind, valor in casos:
        probing.Estrategia.desde(datos).localizador(pagina)
        assert pagina.preguntas[-1] == (kind, valor)


def test_un_kind_desconocido_se_reporta_sin_explotar() -> None:
    pagina = PaginaFalsa()
    with pytest.raises(ValueError, match="no soportado"):
        probing.Estrategia.desde({"kind": "magia"}).localizador(pagina)


# ──────────────────────────────────────────────────────────────── la resolución
def test_resolver_marca_lo_que_encuentra() -> None:
    pagina = PaginaFalsa({("role", "button:New chat"): 1}, visibles={("role", "button:New chat")})
    resultado = probing.resolver(
        pagina,
        "chat_nuevo",
        "abrir chat",
        probing.Estrategia.desde({"kind": "role", "role": "button", "name": "New chat"}),
    )
    assert resultado.ok and resultado.encontrados == 1 and resultado.visible


def test_resolver_reporta_lo_que_no_existe() -> None:
    pagina = PaginaFalsa()  # no responde nada: todo devuelve 0
    resultado = probing.resolver(
        pagina, "chat_nuevo", "abrir chat", probing.Estrategia.desde({"kind": "text", "text": "New chat"})
    )
    assert not resultado.resuelve and not resultado.ok and resultado.encontrados == 0


def test_resolver_no_explota_si_la_pagina_falla() -> None:
    class Rota:
        def get_by_role(self, *args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("pestaña cerrada")

    resultado = probing.resolver(
        Rota(),  # type: ignore[arg-type]
        "chat_nuevo",
        "abrir chat",
        probing.Estrategia.desde({"kind": "role", "role": "button"}),
    )
    assert not resultado.ok and "RuntimeError" in resultado.error


def test_elegir_estrategia_se_queda_con_la_primera_y_no_sigue() -> None:
    """El bot usa la primera que resuelve: ni una pregunta de más."""
    pagina = PaginaFalsa(
        {("aria", "New chat"): 1, ("text", "New chat"): 3},
        visibles={("aria", "New chat")},
    )
    grupo = _grupo(
        role={"kind": "role", "role": "button", "name": "New chat"},
        aria={"kind": "aria", "label": "New chat"},
        texto={"kind": "text", "text": "New chat"},
    )
    elegida = probing.elegir_estrategia(pagina, "chat_nuevo", grupo)
    assert elegida is not None and elegida.estrategia.kind == "aria"
    assert pagina.preguntas == [("role", "button:New chat"), ("aria", "New chat")]


def test_elegir_estrategia_devuelve_none_si_ninguna_resuelve() -> None:
    assert probing.elegir_estrategia(PaginaFalsa(), "x", _grupo(a={"kind": "text", "text": "y"})) is None


def test_probar_grupo_las_prueba_todas() -> None:
    pagina = PaginaFalsa({("text", "New chat"): 1})
    grupo = _grupo(
        role={"kind": "role", "role": "button", "name": "New chat"},
        aria={"kind": "aria", "label": "New chat"},
        texto={"kind": "text", "text": "New chat"},
    )
    resultados = probing.probar_grupo(pagina, "chat_nuevo", grupo)
    assert len(resultados) == 3
    assert [r.ok for r in resultados] == [False, False, True]


# ──────────────────────────────────────────────────────────────── el inventario
def test_inventario_normaliza_y_perdona() -> None:
    pagina = PaginaFalsa(inventario=[{"tag": "button", "role": "", "name": "  New   chat ", "visible": True}])
    items = probing.inventario(pagina)
    assert items[0]["name"] == "New chat" and items[0]["visible"] is True

    assert probing.inventario(PaginaFalsa(falla_evaluate=True)) == []
    assert probing.inventario(PaginaFalsa(inventario=["no soy un dict"])) == []  # type: ignore[list-item]


def test_formatear_tabla_dice_grupo_y_resultado() -> None:
    pagina = PaginaFalsa({("text", "New chat"): 1})
    grupo = _grupo(texto={"kind": "text", "text": "New chat"}, css={"kind": "css", "selector": "main"})
    tabla = probing.formatear_tabla(probing.probar_grupo(pagina, "chat_nuevo", grupo))
    assert "chat_nuevo" in tabla
    assert tabla.count("sí") >= 1 and "no" in tabla


# ──────────────────────────────────────────── los archivos que se entregan
def test_selectors_toml_tiene_la_forma_prometida() -> None:
    grupos = probing.cargar_grupos(probe.cargar_toml(AQUI / "selectors.toml"))
    assert len(grupos) >= 7
    for nombre, datos in grupos.items():
        estrategias = datos["estrategias"]
        assert len(estrategias) >= 2, f"{nombre} necesita al menos una alternativa"
        for estrategia in estrategias:
            assert probing.Estrategia.desde(estrategia).kind in ("role", "aria", "placeholder", "text", "testid", "css")
        # Nada de clases hasheadas: el CSS, si hay, es estructural.
        for estrategia in estrategias:
            if estrategia.get("kind") == "css":
                assert "." not in estrategia.get("selector", "")


def test_config_toml_lleva_el_ritmo_y_el_timeout() -> None:
    """El pacing no es una nota al pie: va en la configuración y se comprueba."""
    config = probe.cargar_toml(AQUI / "config.toml")
    ritmo = config["ritmo"]
    assert ritmo["min_espera"] >= 5 and ritmo["max_espera"] > ritmo["min_espera"]
    assert ritmo["pausa_cada"] >= 1 and ritmo["pausa_larga"] > ritmo["max_espera"]
    assert config["tiempos"]["turno_segundos"] > 60
    assert config["cdp"]["traer_al_frente"] is False  # el bot no roba el foco


def test_registro_escribe_jsonl_y_no_explota() -> None:
    ruta = Path(__file__).parent / "logs-probe" / "probe.jsonl"
    registro = __import__("registro")
    registro.registrar(ruta, "prueba", url="http://x", n=1)
    assert ruta.exists()
    assert '"evento": "prueba"' in ruta.read_text(encoding="utf-8")
    ruta.unlink()
    ruta.parent.rmdir()
