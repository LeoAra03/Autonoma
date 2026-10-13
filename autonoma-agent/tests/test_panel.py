"""El panel local: ver, dirigir y parar el lazo desde otra pantalla (p. ej. el celular)."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from autonoma.cli import _abrir_panel, parse_args
from autonoma.panel import Panel, PanelConfig, direcciones_lan, iniciar_panel

_COMODIN = "0.0.0.0"  # noqa: S104


class ConsolaFalsa:
    def __init__(self) -> None:
        self.lineas: list[str] = []

    def print(self, *objects: object, **kwargs: object) -> None:
        self.lineas.append(" ".join(str(o) for o in objects))

    @property
    def texto(self) -> str:
        return "\n".join(self.lineas)


@pytest.fixture
def abierto(tmp_path: Path):
    """Abre paneles en un puerto libre y los cierra al acabar, sin dejar nada escuchando."""
    paneles: list[Panel] = []

    def _abrir(directorio: Path | None = None, **kwargs: Any) -> Panel:
        panel = iniciar_panel(directorio or tmp_path, puerto=0, **kwargs)
        paneles.append(panel)
        return panel

    yield _abrir
    for panel in paneles:
        panel.stop()


def _pedir(panel: Panel, ruta: str, datos: dict[str, str] | None = None) -> tuple[int, str]:
    """Petición sin proxy de por medio: el panel es local y un proxy lo enviaría a internet."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    url = f"http://127.0.0.1:{panel.puerto}{ruta}"
    cuerpo = urllib.parse.urlencode(datos).encode() if datos else None
    solicitud = urllib.request.Request(url, data=cuerpo)
    try:
        with opener.open(solicitud, timeout=10) as respuesta:
            return respuesta.status, respuesta.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")


def _token(panel: Panel) -> str:
    return panel.urls()[0].split("token=")[1]


# ─────────────────────────────────────────────────────────────── el token
def test_sin_token_no_se_ve_nada(abierto, tmp_path: Path) -> None:
    panel = abierto()
    assert _pedir(panel, "/")[0] == 401
    assert _pedir(panel, "/estado.json")[0] == 401


def test_token_equivocado_no_cuela(abierto, tmp_path: Path) -> None:
    panel = abierto()
    assert _pedir(panel, "/?token=maligno")[0] == 401
    assert _pedir(panel, "/parar", {"token": "maligno", "texto": "x"})[0] == 401


def test_con_token_se_ve_la_pagina(abierto, tmp_path: Path) -> None:
    panel = abierto()
    estado, cuerpo = _pedir(panel, f"/?token={_token(panel)}")
    assert estado == 200
    assert "Autonoma" in cuerpo
    assert "Parar el lazo" in cuerpo


# ─────────────────────────────────────────────────────────────── el estado
def test_estado_dice_si_esta_trabajando_o_no(abierto, tmp_path: Path) -> None:
    panel = abierto()
    assert _estado(panel)["state"] == "sin corridas"  # ni informe ni estado: no ha empezado

    (tmp_path / "estado.json").write_text(
        json.dumps({"goal": "clasificar facturas", "turno": 7, "continuation": "lote 3"}), encoding="utf-8"
    )
    assert _estado(panel)["state"] == "trabajando"
    assert _estado(panel)["goal"] == "clasificar facturas"
    assert _estado(panel)["turno"] == 7

    # Mientras corre, el informe lleva el estado del ÚLTIMO TURNO, no el del lazo.
    (tmp_path / "turns.jsonl").write_text(
        json.dumps({"goal": "clasificar facturas", "state": "ok", "summary": "", "turns": [{"index": 7}]}) + "\n",
        encoding="utf-8",
    )
    assert _estado(panel)["state"] == "trabajando"

    (tmp_path / "turns.jsonl").write_text(
        json.dumps({"goal": "clasificar facturas", "state": "done", "summary": "200 listas", "turns": []}) + "\n",
        encoding="utf-8",
    )
    assert _estado(panel)["state"] == "done"
    assert _estado(panel)["summary"] == "200 listas"


def test_estado_refleja_la_parada_pedida(abierto, tmp_path: Path) -> None:
    panel = abierto()
    (tmp_path / "estado.json").write_text(json.dumps({"goal": "x", "turno": 2}), encoding="utf-8")
    (tmp_path / "STOP").write_text("para\n", encoding="utf-8")
    assert _estado(panel)["state"] == "parada pedida"
    assert _estado(panel)["parada_pedida"] is True


def _estado(panel: Panel) -> dict[str, Any]:
    estado, cuerpo = _pedir(panel, f"/estado.json?token={_token(panel)}")
    assert estado == 200
    carga: dict[str, Any] = json.loads(cuerpo)
    return carga


# ──────────────────────────────────────────────────────── las dos acciones
def test_escribir_una_instruccion(abierto, tmp_path: Path) -> None:
    panel = abierto()
    estado, _ = _pedir(panel, "/orden", {"token": _token(panel), "texto": "revisa los tests de pagos"})
    assert estado == 200
    assert (tmp_path / "steer.txt").read_text(encoding="utf-8").strip() == "revisa los tests de pagos"


def test_una_instruccion_vacia_no_escribe_nada(abierto, tmp_path: Path) -> None:
    panel = abierto()
    _pedir(panel, "/orden", {"token": _token(panel), "texto": "   "})
    assert not (tmp_path / "steer.txt").exists()


def test_pedir_parada_crea_el_stop(abierto, tmp_path: Path) -> None:
    panel = abierto()
    estado, _ = _pedir(panel, "/parar", {"token": _token(panel)})
    assert estado == 200
    assert (tmp_path / "STOP").exists()


# ──────────────────────────────────────────────────────── lo que se muestra
def test_lo_que_viene_del_modelo_se_escapa(abierto, tmp_path: Path) -> None:
    """Un turno puede contener cualquier cosa: se imprime escapado o no se imprime."""
    panel = abierto()
    (tmp_path / "turns.jsonl").write_text(
        json.dumps(
            {
                "goal": "objetivo",
                "state": "",
                "turns": [{"index": 1, "instruction": "<script>alert('x')</script>", "status": "ok"}],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    _, cuerpo = _pedir(panel, f"/?token={_token(panel)}")
    assert "<script>" not in cuerpo
    assert "&lt;script&gt;" in cuerpo


def test_los_secretos_conocidos_no_se_leen_en_la_pagina(abierto, tmp_path: Path) -> None:
    """La página se mira desde un celular: una clave escrita en un turno no debe viajar."""
    clave = "sk-probe-123456"
    panel = abierto(secretos=(clave,))
    (tmp_path / "turns.jsonl").write_text(
        json.dumps({"goal": "usa la clave " + clave, "state": "", "turns": []}) + "\n", encoding="utf-8"
    )
    _, cuerpo = _pedir(panel, f"/?token={_token(panel)}")
    assert clave not in cuerpo
    assert "••••••••" in cuerpo


def test_una_ruta_que_no_existe_devuelve_404(abierto, tmp_path: Path) -> None:
    panel = abierto()
    assert _pedir(panel, f"/etc/passwd?token={_token(panel)}")[0] == 404


# ───────────────────────────────────────────────────────── arranque y cierre
def test_urls_locales_y_de_lan(abierto, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("autonoma.panel.direcciones_lan", lambda: ("192.168.1.5",))
    local = abierto()
    assert local.urls() == (f"http://127.0.0.1:{local.puerto}/?token={_token(local)}",)

    abierto_panel = Panel(
        PanelConfig(directorio=tmp_path, token="tok", direccion="0.0.0.0", puerto=0)  # noqa: S104
    )
    abierto_panel.start()
    try:
        urls = abierto_panel.urls()
        assert any("192.168.1.5" in url for url in urls)  # accesible desde el celular
        assert all("token=tok" in url for url in urls)
        assert not any(_COMODIN in url for url in urls)  # el comodín no sirve para abrir
    finally:
        abierto_panel.stop()


def test_stop_es_idempotente(abierto, tmp_path: Path) -> None:
    panel = abierto()
    panel.stop()
    panel.stop()  # parar dos veces no puede fallar: se llama en un `finally`


def test_se_puede_usar_como_contexto(tmp_path: Path) -> None:
    with Panel(PanelConfig(directorio=tmp_path, token="tok", puerto=0)) as panel:
        assert panel.puerto > 0
        assert _pedir(panel, "/?token=tok")[0] == 200
    with pytest.raises(urllib.error.URLError):  # al salir del bloque deja de escuchar
        _pedir(panel, "/?token=tok")


def test_direcciones_lan_no_falla() -> None:
    """Sin red también tiene que devolver algo: una tupla, no una excepción."""
    assert isinstance(direcciones_lan(), tuple)


# ─────────────────────────────────────────────────────────────────── la CLI
def test_los_flags_del_panel_se_parsean() -> None:
    # `--panel` sin valor es "sólo este equipo"; para la red hay que dar la dirección.
    assert parse_args(["--panel"]).panel == "127.0.0.1"
    assert parse_args(["--panel", "0.0.0.0"]).panel == "0.0.0.0"  # noqa: S104
    assert parse_args(["--panel", "--panel-port", "9001"]).panel_port == 9001
    # Con prompt: el flag va al final para que no se coma el objetivo.
    args = parse_args(["clasifica las facturas", "--panel"])
    assert args.panel == "127.0.0.1"
    assert " ".join(args.prompt) == "clasifica las facturas"


def test_abrir_panel_imprime_la_url(tmp_path: Path) -> None:
    """En local abre y lo dice, sin sustos."""
    contexto = SimpleNamespace(root=tmp_path, data_root=tmp_path)
    consola = ConsolaFalsa()
    panel = _abrir_panel(parse_args(["--panel", "--panel-port", "0", "x"]), contexto, consola)
    try:
        assert panel.puerto > 0
        assert "panel: http://127.0.0.1:" in consola.texto
        assert "fuera de este equipo" not in consola.texto  # en local no hace falta avisar
    finally:
        panel.stop()


def test_abrir_panel_avisa_si_escucha_en_toda_la_red(tmp_path: Path) -> None:
    """Escuchar en 0.0.0.0 es lo que permite verlo desde el celular: hay que decirlo."""
    contexto = SimpleNamespace(root=tmp_path, data_root=tmp_path)
    consola = ConsolaFalsa()
    panel = _abrir_panel(
        parse_args(["--panel", "0.0.0.0", "--panel-port", "0", "x"]),  # noqa: S104
        contexto,
        consola,
    )
    try:
        assert "fuera de este equipo" in consola.texto
        assert "token" in consola.texto
    finally:
        panel.stop()
