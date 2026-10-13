"""Lazo supervisor: el agente se persigue el objetivo solo, sin que el usuario pegue prompts.

El usuario pidió trabajar en otra cosa: el supervisor toma un objetivo, lanza turnos, lee la
línea de control que devuelve el modelo (`NEXT:` / `DONE:` / `BLOCKED:`) y decide si sigue. Todo
lo que pasa por aquí es texto y planificación; la autoridad para tocar el disco sigue viviendo en
`autonoma.approval` y en la aprobación por TTY.

Propiedades deliberadas:

- El corte por límite de iteraciones NO es un DONE: se reinyecta el objetivo para que el trabajo
  continúe en el turno siguiente en vez de quedar a medias.
- Un turno que no devuelve nada accionable (modelo caído, respuesta vacía) se reintenta un número
  fijo de veces y se rinde con motivo. No hay bucle infinito con la tarifa del proveedor por medio.
- `panic` y Ctrl+C se respetan entre turnos y dentro de ellos: la cancelación humana gana siempre.
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final, Literal

from autonoma.errors import AutonomaError, CancelledByUserError
from autonoma.observability import log_event

__all__ = [
    "SIN_AVANCE_TOPE",
    "STATE_FILENAME",
    "STEER_FILENAME",
    "STOP_FILENAME",
    "TURNS_FILENAME",
    "Continuation",
    "SupervisorBudget",
    "SupervisorOutcome",
    "SupervisorReport",
    "SupervisorState",
    "cargar_estado",
    "parse_continuation",
    "run_supervisor",
    "task_prompt",
]

logger = logging.getLogger(__name__)

TURNS_FILENAME: Final[str] = "turns.jsonl"
#: Archivo donde el humano puede escribir para meter una instrucción en pleno lazo.
STEER_FILENAME: Final[str] = "steer.txt"
#: Dónde va el lazo: permite reanudar una corrida cortada (Ctrl+C, apagón, reinicio).
STATE_FILENAME: Final[str] = "estado.json"
#: En modo "hasta que pare", cuántos turnos seguidos sin línea de control se toleran.
#: Es el freno para que un modelo que no avanza no consuma presupuesto indefinidamente;
#: se sube con `SUPERVISOR_MAX_UNPARSABLE` si hace falta.
SIN_AVANCE_TOPE: Final[int] = 25
#: Archivo que, si aparece, corta el lazo de forma limpia (útil desde otra terminal).
STOP_FILENAME: Final[str] = "STOP"

_TURN_KINDS: Final[tuple[str, ...]] = ("next", "done", "blocked")
SignalKind = Literal["next", "done", "blocked", "none"]

#: Lo que se le inyecta al modelo cada turno. Corto por diseño: la memoria está en el historial.
_TASK_HEADER: Final[str] = "OBJETIVO SUPERVISADO"
_REINJECT_NOTE: Final[str] = (
    "Turno de continuación: retoma exactamente donde quedó el anterior. No vuelvas a pedir permiso "
    "por lo que la política ya autoriza ni repitas trabajo ya hecho; usa las notas y el historial."
)
_LIMIT_NOTE: Final[str] = (
    "El turno anterior terminó por tope de iteraciones, no por falta de trabajo. Continúa el objetivo."
)
_CONTROL_INSTRUCTIONS: Final[str] = (
    "Termina tu respuesta con UNA sola línea de control, al final y sin adornos:\n"
    "NEXT: <siguiente paso concreto>\n"
    "DONE: <resumen breve de lo hecho>\n"
    "BLOCKED: <qué falta y por qué no puedes avanzar>\n"
    "Si el objetivo está completo, DONE. Si algo te excede (falta permiso, falta dato externo, la "
    "política no lo cubre), BLOCKED en vez de dar vueltas."
)

#: Un `NEXT` que no dice nada nuevo (repetición literal del anterior) es un lazo girando en vacío.
_NO_PROGRESS_MARKS: Final[tuple[str, ...]] = ("continuar", "seguir", "next", "...", "-")


@dataclass(frozen=True, slots=True)
class Continuation:
    """Señal de continuidad leída de la respuesta del modelo."""

    kind: SignalKind
    instruction: str
    raw_line: str = ""

    @property
    def actionable(self) -> bool:
        return self.kind in _TURN_KINDS and bool(self.instruction.strip())

    def as_payload(self) -> dict[str, str]:
        return {"kind": self.kind, "instruction": self.instruction, "raw": self.raw_line}


_FENCE_RE: Final[re.Pattern[str]] = re.compile(r"```.*?```", re.DOTALL)


def _strip_code_fences(answer: str) -> str:
    """Fuera los bloques de código: un `DONE:` dentro de un ejemplo no es una señal de terminación."""
    return _FENCE_RE.sub("", (answer or "").replace("\r\n", "\n").replace("\r", "\n"))


_SIGNAL_RE: Final[re.Pattern[str]] = re.compile(
    r"^\s*(?P<kind>NEXT|DONE|BLOCKED)\s*[:\-–]\s*(?P<body>.*)$",
    re.IGNORECASE,
)


def parse_continuation(answer: str, *, max_chars: int = 1_200) -> Continuation:
    """Lee la última línea de control de la respuesta. Sin línea, `kind="none"`.

    Se mira de abajo hacia arriba y se ignora lo que esté dentro de bloques de código: un `DONE:`
    escrito dentro de un ejemplo no es una señal de terminación.
    """
    lines = _strip_code_fences(answer).split("\n")
    for index in range(len(lines) - 1, -1, -1):
        line = lines[index]
        stripped = line.strip()
        match = _SIGNAL_RE.match(line)
        if match is None:
            continue
        kind = match.group("kind").lower()
        body = " ".join(match.group("body").split())[:max_chars]
        if kind == "next":
            return Continuation(kind="next", instruction=body, raw_line=stripped[:max_chars])
        if kind == "done":
            return Continuation(kind="done", instruction=body or "objetivo terminado", raw_line=stripped[:max_chars])
        return Continuation(kind="blocked", instruction=body or "sin motivo declarado", raw_line=stripped[:max_chars])
    return Continuation(kind="none", instruction="", raw_line="")


@dataclass(frozen=True, slots=True)
class SupervisorBudget:
    """Cuánto se persigue el objetivo antes de devolverle el control al humano."""

    max_turns: int = 12
    max_minutes: float = 90.0
    idle_seconds: float = 0.0
    max_repairs: int = 2
    #: Cuántos turnos sin línea de control se toleran antes de rendirse.
    max_unparsable: int = 2
    #: Modo "hasta que yo pare": sin tope de turnos ni ventana de tiempo. Sólo corta
    #: DONE, el archivo `STOP`, la cancelación humana o el freno de fallos seguidos.
    hasta_que_pare: bool = False
    #: Freno del modo "hasta que pare": cuántos turnos seguidos con fallo duro (proveedor
    #: caído, clave rota, sin red) se toleran antes de rendirse. Sin este freno, una
    #: avería externa dejaría el lazo girando en vacío de forma indefinida.
    max_fallos_seguidos: int = 10

    def as_payload(self) -> dict[str, float]:
        return {
            "max_turns": self.max_turns,
            "max_minutes": self.max_minutes,
            "idle_seconds": self.idle_seconds,
            "max_repairs": self.max_repairs,
            "max_unparsable": self.max_unparsable,
            "hasta_que_pare": self.hasta_que_pare,
            "max_fallos_seguidos": self.max_fallos_seguidos,
        }


@dataclass(frozen=True, slots=True)
class TurnRecord:
    """Un turno del lazo, tal como queda en el registro auditable."""

    index: int
    kind: SignalKind
    instruction: str
    chars: int
    seconds: float
    status: str
    error: str = ""

    def as_payload(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "kind": self.kind,
            "instruction": self.instruction,
            "chars": self.chars,
            "seconds": round(self.seconds, 1),
            "status": self.status,
            "error": self.error,
        }


@dataclass(frozen=True, slots=True)
class SupervisorReport:
    """Historial del lazo: lo que se escribe en disco para poder auditar qué se hizo solo."""

    goal: str
    session_id: str
    turns: tuple[TurnRecord, ...]
    state: str
    summary: str
    stopped_by: str = ""

    def as_payload(self) -> dict[str, Any]:
        return {
            "goal": self.goal[:4_000],
            "session_id": self.session_id,
            "state": self.state,
            "summary": self.summary[:2_000],
            "stopped_by": self.stopped_by,
            "turns": [turn.as_payload() for turn in self.turns],
        }

    def save(self, directory: Path) -> Path:
        """Escribe `turns.jsonl`. Un disco de sólo lectura o una ruta torcida NO tumbean el lazo."""
        target = directory / TURNS_FILENAME
        payload = self.as_payload()
        try:
            directory.mkdir(parents=True, exist_ok=True)
            with target.open("w", encoding="utf-8", newline="\n") as handle:
                json.dump(payload, handle, ensure_ascii=False)
                handle.write("\n")
        except OSError:  # un registro que no escribe no puede tumbar el trabajo
            log_event(logger, logging.WARNING, "supervisor.report_unsaved", {"path": str(target)})
        return target


@dataclass(frozen=True, slots=True)
class SupervisorState:
    """Dónde va el lazo: `goal`, turno y continuación pendiente.

    Se escribe en cada turno para que una corrida cortada (Ctrl+C, cierre de la
    terminal, reinicio) se pueda retomar con `--continuar` sin volver a empezar.
    """

    goal: str
    turno: int
    continuation: str
    actualizado: str

    def save(self, directory: Path) -> Path:
        """Escribe `estado.json`; un disco que no escribe nunca tumba el lazo."""
        target = directory / STATE_FILENAME
        try:
            directory.mkdir(parents=True, exist_ok=True)
            with target.open("w", encoding="utf-8", newline="\n") as handle:
                json.dump(
                    {
                        "goal": self.goal,
                        "turno": self.turno,
                        "continuation": self.continuation,
                        "actualizado": self.actualizado,
                    },
                    handle,
                    ensure_ascii=False,
                )
                handle.write("\n")
        except OSError:
            log_event(logger, logging.WARNING, "supervisor.state_unsaved", {"path": str(target)})
        return target

    @classmethod
    def cargar(cls, directory: Path) -> SupervisorState | None:
        """Lee el estado anterior; `None` si no hay corrida previa o está corrupto."""
        target = directory / STATE_FILENAME
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(payload, dict):
            return None
        goal = payload.get("goal")
        if not isinstance(goal, str) or not goal.strip():
            return None
        turno = payload.get("turno", 0)
        continuation = payload.get("continuation", "")
        actualizado = payload.get("actualizado", "")
        return cls(
            goal=goal,
            turno=int(turno) if isinstance(turno, (int, float)) else 0,
            continuation=continuation if isinstance(continuation, str) else "",
            actualizado=actualizado if isinstance(actualizado, str) else "",
        )


def cargar_estado(directory: Path) -> SupervisorState | None:
    """Atajo de `SupervisorState.cargar` para la CLI (y para quien quiera inspeccionarla)."""
    return SupervisorState.cargar(directory)


@dataclass(frozen=True, slots=True)
class SupervisorOutcome:
    """Decisión del lazo más su reporte: el CLI sólo tiene que imprimir y elegir código de salida."""

    state: Literal["done", "blocked", "exhausted", "cancelled", "failed"]
    summary: str
    report: SupervisorReport

    @property
    def exit_code(self) -> int:
        from autonoma.errors import ExitCode

        if self.state == "done":
            return int(ExitCode.SUCCESS)
        if self.state == "cancelled":
            return int(ExitCode.CANCELLED)
        return int(ExitCode.INCOMPLETE)


RunTurn = Callable[[str], str]
ReadSteer = Callable[[], tuple[str, ...]]


@dataclass(slots=True)
class _Lazo:
    """Estado mutable del lazo; separado de `run_supervisor` para que el ciclo sea legible."""

    budget: SupervisorBudget
    goal: str
    started: float
    turns: list[TurnRecord]
    #: El mismo reloj que fijó `started`. Sin guardarlo, `elapsed()` medía con el reloj
    #: del sistema (el uptime) y el reloj inyectado no servía para acotar la ventana:
    #: en una máquina recién arrancada el lazo seguía en lugar de agotarse.
    clock: Callable[[], float] = time.monotonic
    repairs: int = 0
    #: Fallos duros consecutivos: se reinicia con cada turno que sale bien.
    fallos_seguidos: int = 0
    unparsable: int = 0
    last_next: str = ""

    def elapsed(self) -> float:
        return max(0.0, self.clock() - self.started)

    def time_left(self) -> bool:
        return self.budget.max_minutes <= 0 or self.elapsed() < self.budget.max_minutes * 60.0


def task_prompt(goal: str, *, continuation: str = "", reinject: bool = False, cut: bool = False) -> str:
    """Prompt de un turno del lazo: objetivo + (si hace falta) la orden de continuidad."""
    blocks: list[str] = [f"{_TASK_HEADER}\n{goal.strip()[:8_000]}"]
    if reinject:
        blocks.append(_REINJECT_NOTE if not cut else f"{_REINJECT_NOTE}\n{_LIMIT_NOTE}")
    if continuation.strip():
        blocks.append(f"SIGUIENTE PASO DECLARADO\n{continuation.strip()[:2_000]}")
    blocks.append(_CONTROL_INSTRUCTIONS)
    return "\n\n".join(blocks)


def _looks_like_no_progress(instruction: str, previous: str) -> bool:
    current = " ".join((instruction or "").lower().split())
    before = " ".join((previous or "").lower().split())
    if not current:
        return True
    if current == before:
        return True
    return current in _NO_PROGRESS_MARKS


def _read_steer_file(directory: Path | None) -> tuple[str, ...]:
    """El humano puede meter la mano: si aparece `steer.txt`, se consume y se antepone."""
    if directory is None:
        return ()
    target = directory / STEER_FILENAME
    try:
        text = target.read_text(encoding="utf-8")
    except (FileNotFoundError, IsADirectoryError, PermissionError, UnicodeDecodeError, OSError):
        return ()
    if not text.strip():
        return ()
    with contextlib.suppress(OSError):
        target.unlink()
    return tuple(line.strip() for line in text.splitlines() if line.strip())


def _stop_requested(directory: Path | None) -> bool:
    return directory is not None and (directory / STOP_FILENAME).exists()


def run_supervisor(
    goal: str,
    run_turn: RunTurn,
    *,
    budget: SupervisorBudget | None = None,
    session_id: str = "",
    report_dir: Path | None = None,
    on_event: Callable[[str, str], None] | None = None,
    is_cancelled: Callable[[], bool] | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
    stop_on_block: bool = True,
    continuation: str = "",
    turn_offset: int = 0,
) -> SupervisorOutcome:
    """Encadena turnos hasta DONE / BLOCKED / tope. `run_turn(prompt)` devuelve la respuesta.

    Ningún fallo de un turno aborta el lazo sin más: se reinyecta el objetivo hasta `max_repairs`
    veces, y si aun así no avanza, se rinde con `state="failed"` y el motivo en el resumen.

    Con `budget.hasta_que_pare` el lazo no tiene tope de turnos ni de tiempo: sólo corta DONE,
    el archivo `STOP`, la cancelación humana o los dos frenos de seguridad (`max_fallos_seguidos`
    y los turnos seguidos sin avance). `continuation` y `turn_offset` sirven para reanudar una
    corrida cortada leyendo `estado.json` (`--continuar`).
    """

    def emit(kind: str, message: str) -> None:
        if on_event is not None:
            on_event(kind, message)

    effective = budget or SupervisorBudget()
    lazo = _Lazo(budget=effective, goal=goal, started=clock(), turns=[], clock=clock)
    reinject = False  # `continuation` llega como parámetro: vacío al empezar, o el paso pendiente al reanudar
    state: Literal["done", "blocked", "exhausted", "cancelled", "failed"] = "exhausted"
    summary = ""
    stopped_by = ""

    #: Modo "hasta que yo pare": desaparecen el tope de turnos y la ventana de tiempo. Quedan
    #: dos frenos, ambos para no gastar de más: los fallos duros seguidos del proveedor y los
    #: turnos seguidos sin avance (ése se puede subir con `SUPERVISOR_MAX_UNPARSABLE`).
    sin_tope = effective.hasta_que_pare
    tope_sin_avance = max(effective.max_unparsable, SIN_AVANCE_TOPE) if sin_tope else effective.max_unparsable
    turnos_tope = None if sin_tope else max(1, effective.max_turns)
    log_event(
        logger,
        logging.INFO,
        "supervisor.started",
        {"turns_max": turnos_tope, "hasta_que_pare": sin_tope, "goal_chars": len(goal)},
    )

    index = turn_offset
    while turnos_tope is None or (index - turn_offset) < turnos_tope:
        index += 1
        if is_cancelled is not None and is_cancelled():
            state, stopped_by = "cancelled", "cancelación humana"
            summary = summary or "lazo detenido antes del turno"
            break
        steer = _read_steer_file(report_dir)
        if steer:
            continuation = "\n".join(steer)[:2_000]
            reinject = False
            emit("steer", f"instrucción externa: {steer[0][:160]}")
        if _stop_requested(report_dir):
            state, stopped_by = "exhausted", f"archivo {STOP_FILENAME} presente"
            summary = summary or "detenido por petición externa"
            break

        prompt = task_prompt(goal, continuation=continuation, reinject=reinject)
        emit("turn", f"turno {index}" if sin_tope else f"turno {index}/{effective.max_turns}")
        started = clock()
        answer = ""
        status = "ok"
        error = ""
        try:
            answer = run_turn(prompt) or ""
        except CancelledByUserError:
            lazo.turns.append(
                TurnRecord(
                    index=index, kind="none", instruction="", chars=0, seconds=clock() - started, status="cancelled"
                )
            )
            state, stopped_by = "cancelled", "cancelación humana"
            summary = "turno cancelado por el usuario"
            break
        except AutonomaError as exc:
            status, error = "error", exc.user_message()[:400]
        except (ValueError, OSError) as exc:
            status, error = "error", f"{type(exc).__name__}: {str(exc)[:200]}"
        except Exception as exc:  # noqa: BLE001 — el lazo no puede propagar un fallo opaco
            status, error = "error", f"falla interna: {type(exc).__name__}"
        seconds = max(0.0, clock() - started)

        signal = parse_continuation(answer)
        lazo.turns.append(
            TurnRecord(
                index=index,
                kind=signal.kind,
                instruction=signal.instruction[:1_200],
                chars=len(answer),
                seconds=seconds,
                status=status,
                error=error,
            )
        )
        report = SupervisorReport(
            goal=goal,
            session_id=session_id,
            turns=tuple(lazo.turns),
            state=state,
            summary=summary,
            stopped_by=stopped_by,
        )
        if report_dir is not None:
            report = replace(report, state=status)
            report.save(report_dir)

        if status == "error":
            lazo.repairs += 1
            lazo.fallos_seguidos += 1
            if sin_tope:
                if lazo.fallos_seguidos > effective.max_fallos_seguidos:
                    state, summary = (
                        "failed",
                        f"el lazo se rinde tras {lazo.fallos_seguidos} fallos seguidos: {error}",
                    )
                    break
                emit("retry", f"turno con fallo; reintento {lazo.fallos_seguidos}/{effective.max_fallos_seguidos}")
            else:
                if lazo.repairs > effective.max_repairs or not lazo.time_left():
                    state, summary = "failed", f"el lazo se rinde: {error}"
                    break
                emit("retry", f"turno con fallo; reintento {lazo.repairs}/{effective.max_repairs}")
            continuation = ""
            reinject = True
            continue
        lazo.fallos_seguidos = 0  # un turno bueno reinicia el freno de fallos seguidos

        if signal.kind == "done":
            state, summary = "done", signal.instruction or "objetivo terminado"
            break
        if signal.kind == "blocked":
            if stop_on_block:
                state, summary = "blocked", signal.instruction or "bloqueado sin motivo declarado"
                break
            # Con el lazo terco, un BLOCKED es un turno sin avance: se cuenta igual que una
            # respuesta sin línea de control y, si se repite, el tope de `max_unparsable` corta.
            lazo.unparsable += 1
            continuation = ""
            reinject = True
            if lazo.unparsable > tope_sin_avance:
                state, summary = (
                    "blocked",
                    f"bloqueado {tope_sin_avance + 1} veces seguidas: {signal.instruction[:180]}",
                )
                break
        if signal.kind == "next":
            if _looks_like_no_progress(signal.instruction, lazo.last_next):
                lazo.unparsable += 1
                if lazo.unparsable > tope_sin_avance:
                    state, summary = "blocked", "el lazo repite el mismo paso: se detiene para no girar en vacío"
                    break
            else:
                lazo.unparsable = 0
            lazo.last_next = signal.instruction
            continuation = signal.instruction
            reinject = False
        else:
            lazo.unparsable += 1
            continuation = ""
            reinject = True
            if lazo.unparsable > tope_sin_avance:
                state, summary = (
                    "failed",
                    f"el modelo no devolvió línea de control en {tope_sin_avance + 1} turnos seguidos",
                )
                break

        if not sin_tope and not lazo.time_left():
            state, summary = "exhausted", f"ventana de {effective.max_minutes:g} minutos agotada"
            stopped_by = "tiempo"
            break

        # Fin del turno: `estado.json` queda con la continuación del paso siguiente, de modo
        # que `--continuar` retoma el trabajo sin repetir lo ya hecho.
        if report_dir is not None:
            SupervisorState(
                goal=goal,
                turno=index,
                continuation=continuation,
                actualizado=datetime.now(timezone.utc).isoformat(),
            ).save(report_dir)

        # Cada turno es una consulta larga al proveedor: una pausa corta evita martillar la API
        # cuando el lazo avanza en pasos diminutos.
        if effective.idle_seconds > 0:
            sleeper(max(0.0, effective.idle_seconds))
    else:
        state = "exhausted" if state == "exhausted" else state
        summary = summary or f"se alcanzaron los {effective.max_turns} turnos del presupuesto sin DONE"

    final = SupervisorReport(
        goal=goal,
        session_id=session_id,
        turns=tuple(lazo.turns),
        state=state,
        summary=summary,
        stopped_by=stopped_by,
    )
    if report_dir is not None:
        final.save(report_dir)
    log_event(
        logger,
        logging.INFO,
        "supervisor.finished",
        {"state": state, "turns": len(final.turns), "seconds": round(lazo.elapsed(), 1)},
    )
    return SupervisorOutcome(state=state, summary=summary or final.summary, report=final)


def goal_from_file(path: Path, *, max_chars: int = 8_000) -> str:
    """Objetivo desde un archivo: `--goal-file` permite preparar el trabajo y dejar corriendo."""
    try:
        text = path.expanduser().read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        from autonoma.errors import ConfigurationError

        raise ConfigurationError(
            f"No se pudo leer el objetivo: {exc}",
            context={"path": str(path)},
        ) from exc
    lines = [line.rstrip() for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    return "\n".join(lines)[:max_chars]
