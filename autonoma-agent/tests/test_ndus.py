"""Recuperar la cookie NDUS: extracción, importación desde navegador/archivo y guardado.

El valor de la cookie es un secreto: las pruebas comprueban que nunca se imprime
completo (sólo enmascarado) y que cada fuente falla con una nota accionable en
lugar de en silencio. Los almacenes de navegador se fabrican en `tmp_path`: nunca
se toca el perfil real del equipo donde corren las pruebas.
"""

from __future__ import annotations

import os
import sqlite3
from argparse import Namespace
from pathlib import Path

import pytest

from autonoma.cli import _import_ndus_from_args
from autonoma.config import Settings, clear_ndus, load_settings, save_ndus
from autonoma.errors import ConfigurationError
from autonoma.ndus import (
    coerce_ndus,
    collect_browser_hits,
    discover_browser_profiles,
    extract_ndus,
    find_cookies_txt,
    import_from_browsers,
    import_from_cookies_txt,
    import_ndus,
    is_real_ndus,
    mask,
    parse_netscape_cookies,
    probe_sources,
)
from autonoma.runtime import DataOrigin, DataRoot, RenderPreferences, RuntimeContext

VALUE = "abc123XYZ789prueba-ndus-000111222333"  # 36 caracteres, sin espacios: como una cookie real
COOKIE_HEADER = f"Cookie: ubid-acb=abc123; ndus={VALUE}; session-token=zzz"
NETSCAPE = (
    "# Netscape HTTP Cookie File\n"
    f".example.com\tTRUE\t/\tFALSE\t1767225600\tndus\t{VALUE}\n"
    ".example.com\tTRUE\t/\tFALSE\t1767225600\tubid-acb\tabc123\n"
)


@pytest.fixture(autouse=True)
def _disco_aislado(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Ninguna prueba toca el `credenciales.txt` real ni el HOME de quien las ejecuta.

    `find_personal_file` mira el directorio actual y `~/.config/autonoma`: sin esto,
    una prueba podría escribir en las credenciales de verdad del desarrollador.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.delenv("AUTONOMA_CREDENTIALS_FILE", raising=False)
    monkeypatch.delenv("AUTONOMA_COOKIES_FILE", raising=False)


def _firefox_db(path: Path, rows: list[tuple[str, str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE moz_cookies (name TEXT, value TEXT, host TEXT)")
        connection.executemany("INSERT INTO moz_cookies VALUES (?, ?, ?)", rows)
        connection.commit()
    finally:
        connection.close()


def _chromium_db(path: Path, rows: list[tuple[str, str, str, bytes]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE cookies (name TEXT, value TEXT, host_key TEXT, encrypted_value BLOB)")
        connection.executemany("INSERT INTO cookies VALUES (?, ?, ?, ?)", rows)
        connection.commit()
    finally:
        connection.close()


# --------------------------------------------------------------------- extracción
def test_extrae_de_una_cabecera_cookie_completa() -> None:
    assert extract_ndus(COOKIE_HEADER) == VALUE


def test_extrae_con_comillas_espacios_y_mayusculas() -> None:
    assert extract_ndus('NDUS = "abc1234567890"') == "abc1234567890"
    assert extract_ndus("Ndus: abc1234567890") == "abc1234567890"


def test_extrae_del_json_del_inspector() -> None:
    assert extract_ndus('{"ndus": "abc1234567890"}') == "abc1234567890"
    assert extract_ndus('{"name":"ndus","value":"abc1234567890"}') == "abc1234567890"


def test_extract_es_estricto_para_no_confundir_una_clave_api() -> None:
    """Sin marca `ndus` no se acepta: pegar una clave API por descuido no debe colarse."""
    assert extract_ndus("sk-notrack-abc1234567890") is None
    assert extract_ndus("") is None


def test_coerce_acepta_el_valor_suelto_pero_rechaza_plantillas() -> None:
    assert coerce_ndus(VALUE) == VALUE
    assert coerce_ndus(COOKIE_HEADER) == VALUE
    assert coerce_ndus("pega-aqui-tu-cookie-ndus-si-la-usas") is None
    assert coerce_ndus("corto") is None
    assert coerce_ndus("dos palabras aqui") is None


def test_is_real_ndus_descarta_placeholders() -> None:
    assert is_real_ndus(VALUE)
    assert not is_real_ndus("tu-ndus")
    assert not is_real_ndus("")
    assert not is_real_ndus("pega-aqui-tu-cookie-ndus-si-la-usas")


def test_un_valor_real_que_contiene_una_palabra_de_plantilla_se_acepta() -> None:
    """Regresión: filtrar por subcadenas genéricas ("valor", "example") bloquea cookies reales."""
    assert is_real_ndus("VALORDEPRUEBA1234567890")
    assert is_real_ndus("aBcD1234ExampleToken--9876543210")
    assert coerce_ndus("VALORDEPRUEBA1234567890") == "VALORDEPRUEBA1234567890"


def test_mask_nunca_revela_el_valor_completo() -> None:
    shown = mask(VALUE)
    assert VALUE not in shown
    assert str(len(VALUE)) in shown


# ---------------------------------------------------------------------- archivos
def test_parsea_cookies_txt_formato_netscape(tmp_path: Path) -> None:
    pairs = parse_netscape_cookies(NETSCAPE)
    assert ("ndus", VALUE) in pairs
    assert ("ubid-acb", "abc123") in pairs


def test_importa_desde_cookies_txt_exportado(tmp_path: Path) -> None:
    target = tmp_path / "cookies.txt"
    target.write_text(NETSCAPE, encoding="utf-8")
    result = import_from_cookies_txt(target)
    assert result.ok
    assert result.value == VALUE
    assert result.origin == "cookies.txt"


def test_importa_desde_un_archivo_con_solo_la_cabecera(tmp_path: Path) -> None:
    target = tmp_path / "cabecera.txt"
    target.write_text(COOKIE_HEADER, encoding="utf-8")
    result = import_ndus(file=target)
    assert result.ok and result.value == VALUE


def test_un_archivo_sin_la_cookie_explica_el_motivo(tmp_path: Path) -> None:
    target = tmp_path / "vacio.txt"
    target.write_text("# nada util\notra=cosa\n", encoding="utf-8")
    result = import_ndus(file=target)
    assert not result.ok
    assert "ndus" in result.detail


def test_archivo_inexistente_no_lanza(tmp_path: Path) -> None:
    result = import_ndus(file=tmp_path / "no-existe.txt")
    assert not result.ok
    assert "no se pudo leer" in result.detail


# --------------------------------------------------------------------- navegador
def test_firefox_se_lee_en_claro(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _firefox_db(home / ".mozilla" / "firefox" / "abc.default" / "cookies.sqlite", [("ndus", VALUE, ".example.com")])
    profiles = discover_browser_profiles(home=home)
    assert profiles and profiles[0].browser == "Firefox"
    result = import_from_browsers(home=home)
    assert result.ok and result.value == VALUE
    assert "example.com" in result.detail


def test_chromium_cifrado_informa_en_vez_de_desaparecer(tmp_path: Path) -> None:
    home = tmp_path / "home"
    profile = home / ".config" / "google-chrome" / "Default"
    _chromium_db(profile / "Cookies", [("ndus", "", ".example.com", b"v10" + bytes(48))])
    (home / ".config" / "google-chrome" / "Local State").write_text("{}", encoding="utf-8")
    hits, warnings = collect_browser_hits(home=home)
    assert hits and hits[0].encrypted
    assert warnings == []
    result = import_from_browsers(home=home)
    assert not result.ok
    assert "cifrada" in result.detail


def test_chromium_en_claro_tambien_sirve(tmp_path: Path) -> None:
    home = tmp_path / "home"
    profile = home / ".config" / "microsoft-edge" / "Default"
    _chromium_db(profile / "Cookies", [("ndus", VALUE, ".example.com", b"")])
    result = import_ndus(auto=True, home=home)
    assert result.ok and result.value == VALUE


def test_sin_navegadores_el_auto_dice_lo_que_miro(tmp_path: Path) -> None:
    """Sin perfiles la respuesta no es un `None` mudo: explica que no había nada que leer."""
    result = import_ndus(auto=True, home=tmp_path / "vacio")
    assert not result.ok
    assert "navegador" in result.detail
    assert probe_sources(home=tmp_path / "vacio") == []


def test_un_almacen_corrupto_no_tumba_la_busqueda(tmp_path: Path) -> None:
    home = tmp_path / "home"
    broken = home / ".mozilla" / "firefox" / "roto.default" / "cookies.sqlite"
    broken.parent.mkdir(parents=True)
    broken.write_bytes(b"esto no es sqlite")
    _firefox_db(home / ".mozilla" / "firefox" / "ok.default" / "cookies.sqlite", [("ndus", VALUE, ".example.com")])
    result = import_ndus(auto=True, home=home)
    assert result.ok and result.value == VALUE


def test_auto_tambien_mira_el_cookies_txt_descargado(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / "cookies.txt").write_text(NETSCAPE, encoding="utf-8")
    assert find_cookies_txt(home=home, cwd=home)
    result = import_ndus(auto=True, home=home)
    assert result.ok and result.value == VALUE


def test_el_texto_pegado_gana_a_la_autodeteccion(tmp_path: Path) -> None:
    """Lo que el usuario acaba de pegar manda: no se adivina por encima de su intención."""
    home = tmp_path / "home"
    _firefox_db(home / ".mozilla" / "firefox" / "abc.default" / "cookies.sqlite", [("ndus", "del-navegador-1234", ".e.com")])
    pegado = "pegada-por-el-usuario-9876543210"
    result = import_ndus(text=pegado, auto=True, home=home)
    assert result.value == pegado
    assert result.origin == "texto"


# ------------------------------------------------------------------ persistencia
def test_guarda_en_el_txt_personal_y_preserva_el_resto(tmp_path: Path) -> None:
    personal = tmp_path / "credenciales.txt"
    personal.write_text(
        "# mis claves\nNOTRACK_API_KEY=sk-notrack-abcdef123456\nNDUS=pega-aqui-tu-ndus\nOTRA=se-queda\n",
        encoding="utf-8",
    )
    path = save_ndus(VALUE, personal_path=personal)
    assert path == personal
    text = personal.read_text(encoding="utf-8")
    assert f"NDUS={VALUE}" in text
    assert "sk-notrack-abcdef123456" in text
    assert "OTRA=se-queda" in text
    assert "pega-aqui" not in text


def test_guarda_respetando_una_linea_tipo_cookie_header(tmp_path: Path) -> None:
    personal = tmp_path / "credenciales.txt"
    personal.write_text(f"{COOKIE_HEADER}\n", encoding="utf-8")
    save_ndus(VALUE, personal_path=personal)
    text = personal.read_text(encoding="utf-8")
    assert f"ndus={VALUE}" in text
    assert "session-token=zzz" in text


def test_crea_el_txt_personal_si_no_existia(tmp_path: Path) -> None:
    path = save_ndus(VALUE, root=tmp_path)
    assert path == tmp_path / "credenciales.txt"
    assert path.read_text(encoding="utf-8").count("NDUS=") == 1
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600


def test_rechaza_plantillas_y_valores_imposibles(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError):
        save_ndus("pega-aqui-tu-ndus", root=tmp_path)
    with pytest.raises(ConfigurationError):
        save_ndus("abc\nNDUS=otra", root=tmp_path)
    with pytest.raises(ConfigurationError):
        save_ndus("", root=tmp_path)


def test_borrar_quita_solo_la_cookie(tmp_path: Path) -> None:
    personal = tmp_path / "credenciales.txt"
    personal.write_text(f"NOTRACK_API_KEY=sk-notrack-abcdef123456\nNDUS={VALUE}\n", encoding="utf-8")
    clear_ndus(personal_path=personal)
    text = personal.read_text(encoding="utf-8")
    assert "NDUS" not in text
    assert "sk-notrack-abcdef123456" in text


def test_settings_refleja_el_cambio_sin_releer_disco(tmp_path: Path) -> None:
    settings = Settings(root_dir=tmp_path)
    assert not settings.has_ndus
    updated = settings.with_ndus(VALUE)
    assert updated.has_ndus
    assert VALUE in updated.secret_values  # entra en el filtro de censura de logs


def test_la_configuracion_la_carga_del_txt_personal(tmp_path: Path) -> None:
    """Camino completo: se guarda con un flag y la carga posterior la ve."""
    save_ndus(VALUE, root=tmp_path)
    loaded = load_settings(env={}, data_root=tmp_path, personal_path=tmp_path / "credenciales.txt")
    assert loaded.ndus == VALUE
    assert loaded.has_ndus


# ------------------------------------------------------------------------- CLI
def _args(**kwargs: object) -> Namespace:
    base = {"ndus": None, "ndus_file": None, "ndus_auto": False}
    base.update(kwargs)
    return Namespace(**base)


def _context(root: Path) -> RuntimeContext:
    return RuntimeContext(data_root=DataRoot(root, DataOrigin.FLAG), render=RenderPreferences(plain=True))


def test_flag_ndus_importa_y_sale_sin_mostrar_el_valor(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _import_ndus_from_args(_args(ndus=COOKIE_HEADER), _context(tmp_path)) is True
    out = capsys.readouterr().out
    assert VALUE not in out
    assert "importada" in out
    assert (tmp_path / "credenciales.txt").read_text(encoding="utf-8").strip().endswith(f"NDUS={VALUE}")


def test_flag_ndus_file_importa_desde_cookies_txt(tmp_path: Path) -> None:
    source = tmp_path / "cookies.txt"
    source.write_text(NETSCAPE, encoding="utf-8")
    root = tmp_path / "datos"
    root.mkdir()
    assert _import_ndus_from_args(_args(ndus_file=source), _context(root)) is True
    assert VALUE in (root / "credenciales.txt").read_text(encoding="utf-8")


def test_flag_ndus_auto_sin_navegador_explica_como_seguir(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """En un sandbox no hay navegador: el fallo debe decir cómo pegarla a mano."""
    assert _import_ndus_from_args(_args(ndus_auto=True), _context(tmp_path / "datos")) is False
    err = capsys.readouterr().err
    assert "--ndus" in err or "/ndus" in err


def test_flag_ndus_con_plantilla_falla_sin_escribir(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Copiar la línea de la plantilla no debe colarse: se rechaza y no se toca el disco."""
    assert _import_ndus_from_args(_args(ndus="Cookie: ndus=pega-aqui-tu-ndus"), _context(tmp_path)) is False
    assert not (tmp_path / "credenciales.txt").exists()
    assert "plantilla" in capsys.readouterr().err


def test_los_flags_de_ndus_no_se_mezclan_con_doctor() -> None:
    from autonoma.cli import parse_args

    with pytest.raises(SystemExit):
        parse_args(["--doctor", "--ndus", VALUE])
    with pytest.raises(SystemExit):
        parse_args(["--selftest", "--ndus-auto"])


def test_sin_fuente_alguna_no_se_inventa_nada() -> None:
    """Sin texto, archivo ni auto no se adivina: el resultado dice que no había fuente."""
    result = import_ndus()
    assert not result.ok
    assert "fuente" in result.detail


def test_mask_de_un_valor_corto_no_deja_nada_al_aire() -> None:
    assert mask("") == "(vacío)"
    assert mask("abc") == "***"


def test_probe_sources_explica_que_encontro(tmp_path: Path) -> None:
    """El informe de `--ndus-auto` es lo que permite saber por qué no apareció nada."""
    home = tmp_path / "home"
    _firefox_db(home / ".mozilla" / "firefox" / "abc.default" / "cookies.sqlite", [("ndus", VALUE, ".example.com")])
    probes = probe_sources(home=home)
    assert len(probes) == 1
    assert probes[0].present
    assert "legible" in probes[0].describe()


def test_una_cookie_ilegible_no_cuenta_como_hallazgo(tmp_path: Path) -> None:
    """Hay fila pero no se puede abrir: se reporta como cifrada, no como cookie válida."""
    home = tmp_path / "home"
    profile = home / ".config" / "google-chrome" / "Default"
    _chromium_db(profile / "Cookies", [("ndus", "", ".example.com", b"blob-sin-prefijo")])
    hits, _warnings = collect_browser_hits(home=home)
    assert hits and not hits[0].usable and hits[0].encrypted
    assert not import_from_browsers(home=home).ok


def test_una_fila_vacia_y_sin_cifrar_se_ignora(tmp_path: Path) -> None:
    """Ni valor ni blob: no hay cookie que importar, así que no se genera hallazgo."""
    home = tmp_path / "home"
    profile = home / ".config" / "google-chrome" / "Default"
    _chromium_db(profile / "Cookies", [("ndus", "", ".example.com", b"")])
    assert collect_browser_hits(home=home) == ([], [])


def test_un_blob_sin_cifrar_no_se_descifra_en_linux(tmp_path: Path) -> None:
    """En Linux la vía DPAPI no aplica: se dice que está cifrada, no que no existe."""
    from autonoma.ndus import BrowserProfile, _decrypt_chromium

    profile = BrowserProfile("Chrome", "chromium", tmp_path / "Cookies")
    value, note = _decrypt_chromium(b"blob-sin-prefijo", profile)
    assert value == ""
    assert "DPAPI" in note or "cifrada" in note


def test_extra_roots_permite_apuntar_a_un_perfil_concreto(tmp_path: Path) -> None:
    """Si el perfil no está donde se espera, se puede señalar a mano (y no se duplica)."""
    profile = tmp_path / "perfil-raro"
    _chromium_db(profile / "Cookies", [("ndus", VALUE, ".example.com", b"")])
    found = discover_browser_profiles(home=tmp_path / "vacio", extra_roots=[profile, profile])
    assert len(found) == 1
    assert import_ndus(auto=True, home=tmp_path / "vacio", extra_roots=[profile]).value == VALUE


def test_la_ayuda_del_repl_documenta_el_atajo() -> None:
    from autonoma.cli import HELP_TEXT

    assert "/ndus" in HELP_TEXT
    assert "auto" in HELP_TEXT
