"""El arranque asistido: descubrir repos, listar chats, preguntar y aplicar."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from autonoma.config import load_settings
from autonoma.sessions import SessionInfo
from autonoma.starter import (
    ARRANQUE_ENVVAR,
    REGISTRY_FILENAME,
    WORKSPACE_ENVVAR,
    Arranque,
    OpcionRepo,
    aplicar_arranque,
    bases_del_entorno,
    cargar_registro,
    descubrir_repos,
    escanear_repos,
    formatear_chat,
    preguntar_chat,
    preguntar_repo,
    recordar_repo,
    resolver_arranque,
)


class ConsolaFalsa:
    """Doble de consola: guarda lo escrito y responde con respuestas programadas."""

    def __init__(self, respuestas: list[str] | None = None) -> None:
        self.lineas: list[str] = []
        self.preguntas: list[str] = []
        self._respuestas = list(respuestas or [])

    def print(self, *objects: object, **kwargs: object) -> None:
        self.lineas.append(" ".join(str(o) for o in objects))

    def input(self, prompt: str = "") -> str:
        self.preguntas.append(prompt)
        self.lineas.append(prompt)
        return self._respuestas.pop(0) if self._respuestas else ""

    @property
    def texto(self) -> str:
        return "\n".join(self.lineas)


def _repo(ruta: Path, *, nombre: str = "") -> None:
    ruta.mkdir(parents=True, exist_ok=True)
    (ruta / ".git").mkdir(exist_ok=True)


# ──────────────────────────────────────────────────────────────── registro
def test_registro_inexistente_no_es_un_error(tmp_path: Path) -> None:
    assert cargar_registro(tmp_path / REGISTRY_FILENAME) == ()


def test_registro_roto_no_tira_el_arranque(tmp_path: Path) -> None:
    (tmp_path / REGISTRY_FILENAME).write_text("{ esto no es json", encoding="utf-8")
    assert cargar_registro(tmp_path / REGISTRY_FILENAME) == ()


def test_registro_acepta_las_dos_formas(tmp_path: Path) -> None:
    archivo = tmp_path / REGISTRY_FILENAME
    archivo.write_text(
        json.dumps({"repos": [{"nombre": "Autonoma", "ruta": "~/Autonoma"}]}), encoding="utf-8"
    )
    con_repos = cargar_registro(archivo)
    assert len(con_repos) == 1
    assert con_repos[0].nombre == "Autonoma"
    assert con_repos[0].origen == "registro"

    archivo.write_text(json.dumps([{"name": "web", "path": "/tmp/web"}]), encoding="utf-8")
    sin_repos = cargar_registro(archivo)
    assert [item.nombre for item in sin_repos] == ["web"]


def test_registro_salta_lo_que_no_entiende(tmp_path: Path) -> None:
    archivo = tmp_path / REGISTRY_FILENAME
    archivo.write_text(
        json.dumps({"repos": [{"nombre": "sin ruta"}, {"ruta": "~/vale"}, "no soy un dict", 7]}),
        encoding="utf-8",
    )
    opciones = cargar_registro(archivo)
    assert [item.ruta.name for item in opciones] == ["vale"]


# ──────────────────────────────────────────────────────────────── escaneo
def test_el_escaneo_encuentra_repos_y_ignora_lo_demas(tmp_path: Path) -> None:
    _repo(tmp_path / "alpha")
    _repo(tmp_path / "beta")
    (tmp_path / "documentos").mkdir()
    (tmp_path / ".oculto").mkdir()
    _repo(tmp_path / ".oculto" / "gamma")
    nombres = {opcion.nombre for opcion in escanear_repos(tmp_path)}
    assert nombres == {"alpha", "beta"}
    assert all(opcion.origen == "git" for opcion in escanear_repos(tmp_path))


def test_el_escaneo_de_una_carpeta_que_no_existe_devuelve_nada(tmp_path: Path) -> None:
    assert escanear_repos(tmp_path / "no-está") == ()


def test_el_escaneo_es_corto(tmp_path: Path) -> None:
    """Dos niveles y un tope: no es un buscador de discos, es una carpeta de proyectos."""
    profundo = tmp_path / "a" / "b" / "c" / "d"
    _repo(profundo)
    assert escanear_repos(tmp_path) == ()


# ───────────────────────────────────────────────────────── descubrimiento
def test_descubrir_orden_y_sin_repetidos(tmp_path: Path) -> None:
    actual = tmp_path / "aqui"
    actual.mkdir()
    _repo(tmp_path / "madre" / "alpha")
    registro = tmp_path / REGISTRY_FILENAME
    registro.write_text(
        json.dumps({"repos": [{"nombre": "Alpha", "ruta": str(tmp_path / "madre" / "alpha")}]}),
        encoding="utf-8",
    )
    opciones = descubrir_repos(registro=registro, bases=[tmp_path / "madre"], actual=actual)
    assert [opcion.origen for opcion in opciones] == ["actual", "registro"]
    assert len({opcion.ruta for opcion in opciones}) == len(opciones)  # sin repetidos


# ──────────────────────────────────────────────────────────── las preguntas
def test_preguntar_repo_devuelve_lo_elegido(tmp_path: Path) -> None:
    _repo(tmp_path / "alpha")
    _repo(tmp_path / "beta")
    consola = ConsolaFalsa(["2"])
    elegido = preguntar_repo(consola, descubrir_repos(bases=[tmp_path], actual=tmp_path))
    assert elegido == (tmp_path / "alpha").resolve()
    assert "¿Sobre qué repositorio trabajo?" in consola.texto


def test_entrada_vacia_usa_la_opcion_por_defecto(tmp_path: Path) -> None:
    consola = ConsolaFalsa([])
    elegido = preguntar_repo(consola, descubrir_repos(actual=tmp_path))
    assert elegido == tmp_path.resolve()
    assert "Elige [1]" in consola.texto


def test_una_respuesta_fuera_de_lista_no_rompe(tmp_path: Path) -> None:
    consola = ConsolaFalsa(["99", ""])
    elegido = preguntar_repo(consola, descubrir_repos(actual=tmp_path))
    assert elegido == tmp_path.resolve()
    assert "no está en la lista" in consola.texto


def test_otra_ruta_acepta_una_carpeta_nueva(tmp_path: Path) -> None:
    destino = tmp_path / "nuevo"
    destino.mkdir()
    consola = ConsolaFalsa(["2", str(destino)])  # 2 = "otra ruta…"
    assert preguntar_repo(consola, descubrir_repos(actual=tmp_path)) == destino.resolve()


def test_otra_ruta_inexistente_se_queda_donde_esta(tmp_path: Path) -> None:
    consola = ConsolaFalsa(["2", str(tmp_path / "no-existe")])
    assert preguntar_repo(consola, descubrir_repos(actual=tmp_path)) is None
    assert "no es una carpeta" in consola.texto


def test_preguntar_chat_sin_chats_no_pregunta() -> None:
    consola = ConsolaFalsa([])
    assert preguntar_chat(consola, []) is None
    assert consola.lineas == []


def test_preguntar_chat_nuevo_por_defecto() -> None:
    chats = [SessionInfo(session_id="20261012-010101-0001", turns=2, updated_at=time.time(), preview="hola")]
    consola = ConsolaFalsa([""])
    assert preguntar_chat(consola, chats) is None  # opción 1 = chat nuevo
    assert "chat nuevo" in consola.texto


def test_preguntar_chat_existente_devuelve_el_id() -> None:
    chats = [SessionInfo(session_id="20261012-010101-0001", turns=2, updated_at=time.time(), preview="hola")]
    consola = ConsolaFalsa(["2"])
    assert preguntar_chat(consola, chats) == "20261012-010101-0001"


def test_formatear_chat_dice_cuanto_y_cuando() -> None:
    ahora = time.time()
    assert "ahora mismo" in formatear_chat(SessionInfo("s1", 3, ahora, "clasificar"))
    assert "hace 30 min" in formatear_chat(SessionInfo("s1", 3, ahora - 1800, "clasificar"))
    assert "hace 2 h" in formatear_chat(SessionInfo("s1", 3, ahora - 7200, "clasificar"))
    assert "hace 3 d" in formatear_chat(SessionInfo("s1", 3, ahora - 259_200, "clasificar"))
    assert "3 turnos" in formatear_chat(SessionInfo("s1", 3, ahora, "clasificar"))
    assert "(sin texto)" in formatear_chat(SessionInfo("s1", 3, ahora, "   "))


# ──────────────────────────────────────────────────────────────── recordar
def test_recordar_escribe_y_no_repite(tmp_path: Path) -> None:
    registro = tmp_path / REGISTRY_FILENAME
    opcion = OpcionRepo(nombre="alpha", ruta=tmp_path / "alpha", origen="git")
    assert recordar_repo(registro, opcion) == registro
    assert [item.nombre for item in cargar_registro(registro)] == ["alpha"]
    recordar_repo(registro, opcion)  # segunda vez: no duplica
    assert len(cargar_registro(registro)) == 1


def test_recordar_falla_en_silencio(tmp_path: Path) -> None:
    """Si no se puede escribir el registro, se sigue: lo importante era arrancar."""
    tapon = tmp_path / "tapon"  # un directorio donde iría el archivo
    tapon.mkdir()
    assert recordar_repo(tapon, OpcionRepo(nombre="x", ruta=tmp_path, origen="git")) is None


# ────────────────────────────────────────────────────── asistente completo
def test_sin_interactividad_no_pregunta_nada(tmp_path: Path) -> None:
    consola = ConsolaFalsa(["1", "1"])
    arranque = resolver_arranque(consola, registro=tmp_path / REGISTRY_FILENAME, interactivo=False)
    assert arranque == Arranque()
    assert consola.lineas == []  # ni una pregunta


def test_resolver_arranque_devuelve_repo_y_chat(tmp_path: Path) -> None:
    _repo(tmp_path / "alpha")
    chats = [SessionInfo(session_id="s1", turns=2, updated_at=time.time(), preview="hola")]
    consola = ConsolaFalsa(["2", "2"])  # repo alpha, chat s1
    arranque = resolver_arranque(consola, registro=tmp_path / REGISTRY_FILENAME, bases=[tmp_path], chats=chats)
    assert arranque.repo == (tmp_path / "alpha").resolve()
    assert arranque.chat == "s1"
    assert arranque.preguntado


def test_resolver_arranque_ofrece_recordar_una_ruta_nueva(tmp_path: Path) -> None:
    """Escribir una ruta nueva y decir que sí la deja anotada para la próxima."""
    registro = tmp_path / REGISTRY_FILENAME
    nuevo = tmp_path / "nuevo"
    nuevo.mkdir()
    consola = ConsolaFalsa(["2", str(nuevo), "s", "1"])  # otra ruta, sí recordar, chat nuevo
    arranque = resolver_arranque(consola, registro=registro, chats=[])
    assert arranque.repo == nuevo.resolve()
    assert arranque.chat is None
    assert [item.nombre for item in cargar_registro(registro)] == ["nuevo"]


# ─────────────────────────────────────────────────────────── aplicar
def test_aplicar_arranque_mueve_el_proceso_y_anota_el_ajuste(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "alpha"
    repo.mkdir()
    monkeypatch.chdir(tmp_path)
    overrides: dict[str, str] = {}
    consola = ConsolaFalsa()
    aplicar_arranque(Arranque(repo=repo, chat="s1"), overrides, consola)
    assert Path.cwd().resolve() == repo.resolve()
    assert overrides[WORKSPACE_ENVVAR] == str(repo.resolve())
    assert f"trabajando en {repo.resolve()}" in consola.texto
    assert "retomando s1" in consola.texto


def test_aplicar_arranque_sin_repo_no_toca_nada(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    overrides: dict[str, str] = {}
    assert aplicar_arranque(Arranque(), overrides, ConsolaFalsa()) == {}
    assert Path.cwd().resolve() == tmp_path.resolve()


# ────────────────────────────────────────────────────── entorno
def test_bases_del_entorno() -> None:
    assert bases_del_entorno({ARRANQUE_ENVVAR: "~/a:/tmp/b"}) == (Path.home() / "a", Path("/tmp/b"))
    assert bases_del_entorno({}) == ()
    assert bases_del_entorno({}, extra=[Path("/tmp/c")]) == (Path("/tmp/c"),)


# ──────────────────────────────────────────── raíz de datos ≠ espacio de trabajo
def test_el_espacio_de_trabajo_es_independiente_de_la_raiz_de_datos(tmp_path: Path) -> None:
    """Trabajar en un repo no mueve las claves ni los informes: eso se queda en la raíz de datos."""
    repo = tmp_path / "alpha"
    repo.mkdir()
    datos = tmp_path / "datos"
    datos.mkdir()
    settings = load_settings(data_root=datos, overrides={WORKSPACE_ENVVAR: str(repo)})
    assert settings.workspace.resolve() == repo.resolve()
    assert settings.root.resolve() == datos.resolve()
    assert settings.sessions_path().resolve() == (datos / "sessions").resolve()
    assert settings.supervisor_path().resolve() == (datos / "supervisor").resolve()
    por_defecto = load_settings(data_root=datos)
    assert por_defecto.workspace.resolve() == por_defecto.root.resolve()
