#!/usr/bin/env python3
"""Microbenchmarks reproducibles de las rutas calientes del agente.

No tocan la red ni el disco del usuario: el transporte HTTP se simula con
`httpx.MockTransport` (latencia fija) y los árboles de conocimiento se crean en un
directorio temporal. Sirven para dos cosas:

1. detectar una regresión de rendimiento en el momento (comparar con `--baseline`);
2. documentar de dónde sale la ganancia real (política O(1), validación cacheada,
   abanico concurrente de descargas, registro de logs sin trabajo inútil).

Uso:
    python scripts/bench.py                 # informe actual
    python scripts/bench.py --save /tmp/b1  # guarda línea base
    python scripts/bench.py --baseline /tmp/b1  # compara y sale 1 si hay regresión >15%
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from autonoma.filesystem import FileSystemManager  # noqa: E402
from autonoma.key_handler import PanicController  # noqa: E402
from autonoma.knowledge import KnowledgeStore  # noqa: E402
from autonoma.observability import MetricsRegistry, configure_logging, log_event, trace_scope  # noqa: E402
from autonoma.search_engine import SearchEngine, SearchHit  # noqa: E402
from autonoma.tool_contracts import tool_schemas, validate_arguments  # noqa: E402

REPEATS = 4000
LATENCY_SECONDS = 0.05
PAGES = 3
NOTES = 150
# Tolerancia de ruido entre máquinas/CI: por encima se considera regresión real.
REGRESSION_TOLERANCE = 1.35


def _time(fn: Callable[[], Any], repeats: int = REPEATS, warmup: int = 200, runs: int = 5) -> float:
    """Microsegundos por operación: mínimo de `runs` corridas.

    El mínimo es el estimador estándar en microbenchmarks: el planificador, el
    recolector de basura o un segundo proceso sólo *suman* tiempo, así que la corrida
    más rápida es la que mejor aproxima el coste real de la operación. La mediana
    resultó demasiado ruidosa para una puerta de regresión (1.6x entre dos ejecuciones
    del mismo código).
    """
    for _ in range(warmup):
        fn()
    measured: list[float] = []
    for _ in range(runs):
        start = time.perf_counter()
        for _ in range(repeats):
            fn()
        measured.append((time.perf_counter() - start) / repeats * 1e6)
    return min(measured)


def bench_contract_validation() -> float:
    """Validación de argumentos por llamada de herramienta (antes: reconstruía tablas)."""
    return _time(lambda: validate_arguments("read_file", {"path": "notas.md"}))


def bench_schema_build() -> float:
    """Construcción del esquema enviado al modelo (debe ser precomputado + copia barata)."""
    return _time(lambda: tool_schemas(), repeats=200, warmup=20)


def bench_is_protected(tmp: Path) -> float:
    fs = FileSystemManager(
        PanicController(),
        extra_protected=[str(tmp / f"critica-{index}") for index in range(60)],
    )
    target = str(tmp / "a" / "b" / "c.txt")
    return _time(lambda: fs.is_protected(target))


def bench_knowledge(tmp: Path) -> tuple[float, float]:
    store = KnowledgeStore(PanicController(), tmp / "kb")
    for index in range(NOTES):
        store.save_note(f"nota {index}", ("palabra clave " * 400) + f"unice{index}")
    # `_time` devuelve microsegundos por operación; aquí se expresan en milisegundos.
    listed = _time(lambda: store.list_notes(50), repeats=20, warmup=5) / 1000.0
    searched = _time(lambda: store.search_notes("palabra clave unice119 otras"), repeats=20, warmup=5) / 1000.0
    return listed, searched


def bench_logging(tmp: Path) -> float:
    """Coste de un evento estructurado en el hot path (debe ser despreciable u omitible)."""
    runtime = configure_logging(log_dir=tmp / "logs", level=logging.DEBUG, json_logs=True)
    logger = logging.getLogger("autonoma.bench")
    registry = MetricsRegistry()

    def one() -> None:
        with trace_scope():
            log_event(logger, logging.DEBUG, "tool.read_file", dict(registry.snapshot()["counters"]))

    try:
        return _time(one, repeats=2000, warmup=100)
    finally:
        runtime.close()


def bench_fetch_fanout(tmp: Path) -> dict[str, float]:
    """Descarga de páginas: concurrente (pipeline) frente a bucle secuencial."""

    def handle(_request: httpx.Request) -> httpx.Response:
        time.sleep(LATENCY_SECONDS)
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            content=b"<html><body><p>contenido util " * 4 + b"</p></body></html>",
        )

    hits = tuple(SearchHit(f"titulo {i}", f"https://example.com/{i}", "s") for i in range(PAGES))

    engine = SearchEngine(PanicController(), tmp / "pipe", fetch_pages=PAGES)
    engine._http = httpx.Client(transport=httpx.MockTransport(handle))  # noqa: SLF001 — banco de pruebas

    def stub_search(_query: str, _count: int | None = None) -> tuple[SearchHit, ...]:
        return hits  # se mide la descarga concurrente, no la búsqueda

    engine.search = stub_search  # se sustituye la búsqueda: sin red en el benchmark
    start = time.perf_counter()
    engine.research("prueba", save=False)
    pipeline_ms = (time.perf_counter() - start) * 1000.0
    engine.close()

    serial = SearchEngine(PanicController(), tmp / "serial")
    serial._http = httpx.Client(transport=httpx.MockTransport(handle))  # noqa: SLF001
    start = time.perf_counter()
    for hit in hits:
        serial.fetch_url(hit.url)
    serial_ms = (time.perf_counter() - start) * 1000.0
    serial.close()
    return {"pipeline_ms": pipeline_ms, "sequential_ms": serial_ms}


def measure(tmp: Path) -> dict[str, float]:
    listed, searched = bench_knowledge(tmp)
    fanout = bench_fetch_fanout(tmp)
    metrics: dict[str, float] = {
        "contract_validate_us": bench_contract_validation(),
        "schema_build_us": bench_schema_build(),
        "is_protected_us": bench_is_protected(tmp),
        "list_notes_ms": listed,
        "search_notes_ms": searched,
        "log_event_us": bench_logging(tmp),
        "research_pipeline_ms": fanout["pipeline_ms"],
        "research_sequential_ms": fanout["sequential_ms"],
    }
    metrics["fetch_speedup"] = fanout["sequential_ms"] / max(fanout["pipeline_ms"], 0.001)
    return metrics


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__ or "")
    parser.add_argument("--save", type=Path, help="guarda estas cifras como línea base")
    parser.add_argument("--baseline", type=Path, help="compara contra una línea base guardada")
    args = parser.parse_args(argv)

    with tempfile.TemporaryDirectory(prefix="autonoma-bench-") as raw:
        metrics = measure(Path(raw))

    print(json.dumps(metrics, indent=2, sort_keys=True))
    if args.save:
        args.save.parent.mkdir(parents=True, exist_ok=True)
        args.save.write_text(json.dumps(metrics, indent=2, sort_keys=True), encoding="utf-8")
        print(f"línea base guardada en {args.save}", file=sys.stderr)
        return 0

    if args.baseline:
        before = json.loads(args.baseline.read_text(encoding="utf-8"))
        worst = 0.0
        for name, current in metrics.items():
            previous = before.get(name)
            if previous is None:
                continue
            ratio = current / previous if "speedup" not in name else previous / max(current, 0.001)
            worst = max(worst, ratio)
            flag = "  ⚠ regresión" if ratio > REGRESSION_TOLERANCE else ""
            print(f"{name}: {previous:.2f} → {current:.2f} ({ratio:.2f}x){flag}", file=sys.stderr)
        return 1 if worst > REGRESSION_TOLERANCE else 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
