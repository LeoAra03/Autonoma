"""El lazo supervisor: continuidad por línea de control, topes e interrupción humana.

Se prueba el lazo sin CLI ni proveedor: `run_turn` es un doble que devuelve los textos que un
modelo devolvería. Lo que importa aquí es el *contrato* del modo desatendido: cuándo sigue, cuándo
para, cuándo reintenta y qué deja escrito en disco para poder auditarse después.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from autonoma.errors import AutonomaError, CancelledByUserError, ConfigurationError, ExitCode, ToolContractError
from autonoma.supervisor import (
    STOP_FILENAME,
    TURNS_FILENAME,
    SupervisorBudget,
    SupervisorState,
    _Lazo,
    goal_from_file,
    parse_continuation,
    run_supervisor,
    task_prompt,
)


class Scripted:
    """Guion de respuestas: cada turno devuelve el siguiente texto de la lista y se cuenta."""

    def __init__(self, *answers: str) -> None:
        self.answers = list(answers)
        self.prompts: list[str] = []

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if not self.answers:
            raise AssertionError("el lazo pidió más turnos de los guionados")
        return self.answers.pop(0)


class Failing:
    """Todos los turnos fallan: el lazo debe reintentar un número acotado y rendirse con motivo."""

    def __init__(self, error: Exception) -> None:
        self.error = error
        self.prompts: list[str] = []

    def __call__(self, prompt: str) -> None:
        self.prompts.append(prompt)
        raise self.error


class FakeClock:
    def __init__(self, step: float = 0.0) -> None:
        self.t = 0.0
        self.step = step

    def __call__(self) -> float:
        current = self.t
        self.t += self.step
        return current


# ─────────────────────────────────────────────────────────línea de control
@pytest.mark.parametrize(
    ("answer", "kind", "instruction"),
    [
        ("Listo.\nDONE: archivos actualizados", "done", "archivos actualizados"),
        ("Sigo en el próximo paso.\nNEXT: correr la suite", "next", "correr la suite"),
        ("BLOCKED: falta la clave", "blocked", "falta la clave"),
        ("hecho\n\nnext:   revisar   el   diff", "next", "revisar el diff"),
        ("DONE - resumen final", "done", "resumen final"),
    ],
)
def test_se_lee_la_ultima_linea_de_control(answer: str, kind: str, instruction: str) -> None:
    signal = parse_continuation(answer)
    assert signal.kind == kind
    assert signal.instruction == instruction
    assert signal.actionable or kind == "done"


def test_sin_linea_de_control_no_hay_senal() -> None:
    assert parse_continuation("no sé qué responder").kind == "none"
    assert parse_continuation("").kind == "none"
    assert (
        parse_continuation(
            "DONE",
        ).instruction
        == ""
    )


def test_un_done_dentro_de_un_bloque_de_codigo_no_termina_el_lazo() -> None:
    answer = "Ejemplo del protocolo:\n```text\nDONE: esto es un ejemplo\n```\nNEXT: aplicar el parche real"
    assert parse_continuation(answer).kind == "next"


def test_el_prompt_del_lazo_lleva_objetivo_y_protocolo() -> None:
    prompt = task_prompt("terminar la tanda", continuation="correr mypy")
    assert "OBJETIVO SUPERVISADO" in prompt and "terminar la tanda" in prompt
    assert "NEXT:" in prompt and "BLOCKED:" in prompt
    assert "SIGUIENTE PASO DECLARADO" in prompt and "correr mypy" in prompt
    assert task_prompt("x", reinject=True, cut=True).count("tope de iteraciones") == 1


# ──────────────────────────────────────────────────────────────── el lazo
def test_el_lazo_encadena_hasta_done() -> None:
    turn = Scripted("paso 1\nNEXT: hacer el 2", "paso 2\nNEXT: hacer el 3", "todo listo\nDONE: tres pasos")
    outcome = run_supervisor("objetivo", turn, budget=SupervisorBudget(max_turns=10))
    assert outcome.state == "done"
    assert outcome.exit_code == int(ExitCode.SUCCESS)
    assert len(turn.prompts) == 3
    assert "hacer el 2" in turn.prompts[1]


def test_un_blocked_detiene_y_devuelve_codigo_no_cero() -> None:
    turn = Scripted("NEXT: sigue", "BLOCKED: hace falta aprobación humana")
    outcome = run_supervisor("objetivo", turn, budget=SupervisorBudget(max_turns=8))
    assert outcome.state == "blocked"
    assert "aprobación humana" in outcome.summary
    assert outcome.exit_code == int(ExitCode.INCOMPLETE)
    assert len(turn.prompts) == 2


def test_el_tope_de_turnos_no_es_un_done() -> None:
    turn = Scripted(*["sigue\nNEXT: paso siguiente" for _ in range(4)])
    outcome = run_supervisor("objetivo", turn, budget=SupervisorBudget(max_turns=3))
    assert outcome.state == "exhausted"
    assert len(turn.prompts) == 3
    assert outcome.exit_code == int(ExitCode.INCOMPLETE)


def test_el_corte_por_limite_se_reinyecta_como_trabajo_pendiente() -> None:
    cortado = "análisis parcial\n\nNEXT: retomar el objetivo donde quedó interrumpido por el tope de iteraciones."
    turn = Scripted(cortado, "fin\nDONE: completo tras el corte")
    outcome = run_supervisor("objetivo", turn, budget=SupervisorBudget(max_turns=5))
    assert outcome.state == "done"
    assert "Turno de continuación" not in turn.prompts[1]  # no se reinyecta a ciegas: hay paso declarado
    assert "SIGUIENTE PASO DECLARADO" in turn.prompts[1]


def test_un_giro_en_vacio_se_detecta_y_se_corta() -> None:
    turn = Scripted("NEXT: otra vez lo mismo", "NEXT: otra vez lo mismo", "NEXT: otra vez lo mismo")
    outcome = run_supervisor("objetivo", turn, budget=SupervisorBudget(max_turns=9, max_unparsable=1))
    assert outcome.state == "blocked"
    assert "en vacío" in outcome.summary


def test_respuestas_sin_senal_se_agotan_y_se_rinden() -> None:
    turn = Scripted("mmm", "no entiendo", "tampoco")
    outcome = run_supervisor("objetivo", turn, budget=SupervisorBudget(max_turns=9, max_unparsable=1))
    assert outcome.state == "failed"
    assert "línea de control" in outcome.summary


def test_un_turno_con_fallo_se_reintenta_y_luego_se_rinde() -> None:
    turn = Failing(ToolContractError("el proveedor respondió mal"))
    outcome = run_supervisor("objetivo", turn, budget=SupervisorBudget(max_turns=9, max_repairs=1))
    assert outcome.state == "failed"
    assert "el proveedor respondió mal" in outcome.summary
    assert len(turn.prompts) == 2


def test_la_cancelacion_humana_manda_sobre_el_lazo() -> None:
    turn = Scripted("NEXT: seguir")
    flags = {"cancelled": False}

    def cancel_after_first(prompt: str) -> str:
        answer = turn(prompt)
        flags["cancelled"] = True
        return answer

    outcome = run_supervisor(
        "objetivo", cancel_after_first, budget=SupervisorBudget(max_turns=5), is_cancelled=lambda: flags["cancelled"]
    )
    assert outcome.state == "cancelled"
    assert outcome.exit_code == int(ExitCode.CANCELLED)


def test_un_panic_dentro_del_turno_se_traduce_a_cancelado() -> None:
    def boom(_prompt: str) -> str:
        raise CancelledByUserError("P pulsada")

    outcome = run_supervisor("objetivo", boom, budget=SupervisorBudget(max_turns=4))
    assert outcome.state == "cancelled"


def test_el_lazo_mide_la_ventana_con_el_reloj_inyectado() -> None:
    """La ventana se mide con el reloj inyectado, no con el del sistema.

    Midiendo con `time.monotonic()` (el uptime de la máquina) el resultado cambiaba
    según cuánto llevara encendida: en un runner recién creado, con menos de un minuto
    de uptime, el lazo seguía en vez de agotarse y el test pasaba a `failed`.
    """
    clock = FakeClock(step=120.0)
    lazo = _Lazo(budget=SupervisorBudget(max_minutes=1.0), goal="objetivo", started=clock(), turns=[], clock=clock)
    assert lazo.elapsed() == 120.0
    assert not lazo.time_left()


def test_la_ventana_de_tiempo_corta_el_lazo() -> None:
    turn = Scripted("NEXT: uno", "NEXT: dos")
    outcome = run_supervisor(
        "objetivo",
        turn,
        budget=SupervisorBudget(max_turns=9, max_minutes=1.0),
        clock=FakeClock(step=120.0),
    )
    assert outcome.state == "exhausted"
    assert "ventana de" in outcome.summary


def test_block_en_modo_terco_sigue_hasta_el_tope_de_intentos() -> None:
    turn = Scripted("BLOCKED: falta permiso", "DONE: lo resolví por otra vía")
    outcome = run_supervisor(
        "objetivo",
        turn,
        budget=SupervisorBudget(max_turns=6, max_unparsable=2),
        stop_on_block=False,
    )
    assert outcome.state == "done"
    assert len(turn.prompts) == 2


# ──────────────────────────────────────────────────────── canal del humano
def test_steer_texto_del_sesgo_humano_se_consume_y_manda(tmp_path: Path) -> None:
    turn = Scripted("NEXT: seguir solo", "DONE: aplicado el cambio pedido")
    (tmp_path / "steer.txt").write_text("usa el informe de cobertura, no el de lint\n", encoding="utf-8")
    outcome = run_supervisor("objetivo", turn, report_dir=tmp_path, budget=SupervisorBudget(max_turns=5))
    assert outcome.state == "done"
    assert "informe de cobertura" in turn.prompts[0]  # se lee antes del primer turno, no después
    assert "informe de cobertura" not in turn.prompts[1]
    assert not (tmp_path / "steer.txt").exists()


def test_el_archivo_stop_corta_limpio(tmp_path: Path) -> None:
    (tmp_path / STOP_FILENAME).write_text("", encoding="utf-8")
    turn = Scripted("NEXT: seguir")
    outcome = run_supervisor("objetivo", turn, report_dir=tmp_path, budget=SupervisorBudget(max_turns=5))
    assert outcome.state == "exhausted"
    assert "STOP" in outcome.report.stopped_by
    assert turn.prompts == []


def test_el_informe_deja_rastro_auditable(tmp_path: Path) -> None:
    turn = Scripted("NEXT: un paso", "DONE: dos pasos hechos")
    outcome = run_supervisor(
        "objetivo", turn, report_dir=tmp_path, session_id="s-42", budget=SupervisorBudget(max_turns=4)
    )
    saved = tmp_path / TURNS_FILENAME
    assert saved.is_file()
    payload = json.loads(saved.read_text(encoding="utf-8"))
    assert payload["session_id"] == "s-42"
    assert [turn_["kind"] for turn_ in payload["turns"]] == ["next", "done"]
    assert payload["turns"][1]["status"] == "ok"
    assert outcome.report.as_payload()["goal"] == "objetivo"


def test_un_informe_que_no_puede_escribir_no_tumba_el_lazo(tmp_path: Path) -> None:
    bloque = tmp_path / "bloqueado"
    bloque.write_text("no es un directorio", encoding="utf-8")
    turn = Scripted("DONE: igual")
    outcome = run_supervisor("objetivo", turn, report_dir=bloque / "hijo", budget=SupervisorBudget(max_turns=3))
    assert outcome.state == "done"


# ──────────────────────────────────────────────────────────── objetivo
def test_el_objetivo_de_un_archivo_ignora_comentarios(tmp_path: Path) -> None:
    archivo = tmp_path / "objetivo.md"
    archivo.write_text("# comentario\n\ndejar la puerta verde\n\n# otro\ny documentarlo\n", encoding="utf-8")
    assert goal_from_file(archivo) == "dejar la puerta verde\ny documentarlo"


def test_objetivo_inexistente_es_error_de_configuracion(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="No se pudo leer el objetivo"):
        goal_from_file(tmp_path / "no-esta.md")


def test_un_objetivo_enorme_se_recorta_sin_reventar(tmp_path: Path) -> None:
    archivo = tmp_path / "grande.md"
    archivo.write_text("a" * 40_000, encoding="utf-8")
    assert len(goal_from_file(archivo, max_chars=500)) == 500
    assert len(task_prompt("b" * 40_000)) < 9_000


def test_el_lazo_usa_sleeper_inyectable() -> None:
    slept: list[float] = []
    turn = Scripted("NEXT: uno", "DONE: dos")
    outcome = run_supervisor(
        "objetivo",
        turn,
        budget=SupervisorBudget(max_turns=4, idle_seconds=0.5),
        sleeper=slept.append,
    )
    assert outcome.state == "done"
    assert slept == [0.5]


def test_el_lazo_puede_no_devolver_nada_y_aun_asi_cortar() -> None:
    def vacio(_prompt: str) -> str:
        return ""

    turn: Callable[[str], str] = vacio
    outcome = run_supervisor("objetivo", turn, budget=SupervisorBudget(max_turns=2, max_unparsable=0))
    assert outcome.state == "failed"
    assert len(outcome.report.turns) == 1


# ─────────────────────────────────────────────── modo "hasta que yo pare"
def test_hasta_que_pare_no_se_corta_por_el_tope_de_turnos() -> None:
    """Con el tope quitado, el lazo sigue tantos turnos como haga falta hasta DONE."""
    turn = Scripted(*(f"NEXT: paso {i}" for i in range(1, 6)), "DONE: los cinco pasos")
    outcome = run_supervisor("objetivo", turn, budget=SupervisorBudget(max_turns=1, hasta_que_pare=True))
    assert outcome.state == "done"
    assert len(turn.prompts) == 6


def test_hasta_que_pare_ignora_la_ventana_de_tiempo() -> None:
    """El reloj puede pasar de la ventana: en este modo la ventana deja de existir."""
    turn = Scripted("NEXT: paso uno", "DONE: listo")
    outcome = run_supervisor(
        "objetivo",
        turn,
        budget=SupervisorBudget(max_turns=1, max_minutes=1.0, hasta_que_pare=True),
        clock=FakeClock(step=120.0),
    )
    assert outcome.state == "done"
    assert len(turn.prompts) == 2


def test_hasta_que_pare_se_rinde_tras_los_fallos_seguidos() -> None:
    """El freno: sin él, un proveedor caído dejaría el lazo girando indefinidamente."""
    llamadas: list[int] = []

    def roto(_prompt: str) -> str:
        llamadas.append(1)
        raise AutonomaError("proveedor caído")

    outcome = run_supervisor(
        "objetivo", roto, budget=SupervisorBudget(hasta_que_pare=True, max_fallos_seguidos=3)
    )
    assert outcome.state == "failed"
    assert len(llamadas) == 4  # tolera 3 y se rinde en el cuarto
    assert "fallos seguidos" in outcome.summary


def test_el_freno_de_fallos_se_reinicia_cuando_un_turno_sale_bien() -> None:
    """Un fallo aislado no debe cortar una corrida larga: lo que cuenta es la racha."""
    llamadas: list[int] = []

    def intermitente(_prompt: str) -> str:
        llamadas.append(1)
        if len(llamadas) <= 2:
            raise AutonomaError("fallo de red")
        return "NEXT: avanza" if len(llamadas) == 3 else "DONE: listo"

    outcome = run_supervisor(
        "objetivo", intermitente, budget=SupervisorBudget(hasta_que_pare=True, max_fallos_seguidos=2)
    )
    assert outcome.state == "done"
    assert len(llamadas) == 4


def test_el_archivo_stop_corta_el_modo_hasta_que_pare(tmp_path: Path) -> None:
    """La salida manual: tocar `supervisor/STOP` desde otra terminal para el lazo."""
    directorio = tmp_path / "supervisor"
    directorio.mkdir()

    class ParaEnElSegundo:
        """Devuelve `NEXT` siempre y crea el `STOP` al llegar al segundo turno."""

        def __init__(self) -> None:
            self.prompts: list[str] = []

        def __call__(self, prompt: str) -> str:
            self.prompts.append(prompt)
            if len(self.prompts) >= 2:
                (directorio / STOP_FILENAME).write_text("para", encoding="utf-8")
            return "NEXT: sigo"

    turn = ParaEnElSegundo()
    outcome = run_supervisor(
        "objetivo", turn, budget=SupervisorBudget(hasta_que_pare=True), report_dir=directorio
    )
    assert outcome.state == "exhausted"
    assert STOP_FILENAME in outcome.report.stopped_by
    assert len(turn.prompts) == 2  # el STOP se lee al empezar el turno siguiente


# ──────────────────────────────────────────────────────── reanudación (--continuar)
def test_el_estado_se_guarda_con_la_continuacion_del_paso_siguiente(tmp_path: Path) -> None:
    """`estado.json` se escribe al final de cada turno: reanudar no repite trabajo."""
    outcome = run_supervisor(
        "objetivo",
        Scripted("NEXT: paso uno", "DONE: listo"),
        budget=SupervisorBudget(max_turns=1),
        report_dir=tmp_path,
    )
    assert outcome.state == "exhausted"
    estado = SupervisorState.cargar(tmp_path)
    assert estado is not None
    assert estado.goal == "objetivo"
    assert estado.turno == 1
    assert estado.continuation == "paso uno"
    assert estado.actualizado  # sello de tiempo, para saber de cuándo es


def test_continuar_arranca_en_el_turno_siguiente_y_con_el_paso_pendiente(tmp_path: Path) -> None:
    """`--continuar`: el turno se numera siguiendo la corrida anterior y recupera el paso."""
    run_supervisor(
        "objetivo",
        Scripted("NEXT: paso uno", "DONE: listo"),
        budget=SupervisorBudget(max_turns=1),
        report_dir=tmp_path,
    )
    anterior = SupervisorState.cargar(tmp_path)
    assert anterior is not None
    turn = Scripted("DONE: listo")
    outcome = run_supervisor(
        "objetivo",
        turn,
        budget=SupervisorBudget(max_turns=1),
        report_dir=tmp_path,
        continuation=anterior.continuation,
        turn_offset=anterior.turno,
    )
    assert outcome.state == "done"
    assert outcome.report.turns[0].index == 2  # sigue la numeración, no vuelve a 1
    assert "paso uno" in turn.prompts[0]


def test_cargar_estado_sin_corrida_previa_devuelve_none(tmp_path: Path) -> None:
    assert SupervisorState.cargar(tmp_path) is None
    (tmp_path / "estado.json").write_text("{ esto no es json", encoding="utf-8")
    assert SupervisorState.cargar(tmp_path) is None
    (tmp_path / "estado.json").write_text('{"goal": "   "}', encoding="utf-8")
    assert SupervisorState.cargar(tmp_path) is None  # objetivo vacío = corrida inservible
