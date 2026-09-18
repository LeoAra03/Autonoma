"""Garantías de complejidad: políticas por hash, caches compartidas y cero trabajo repetido."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from autonoma.filesystem import FileSystemManager, ProtectedPolicy, normalize_parts
from autonoma.key_handler import PanicController
from autonoma.observability import MetricsRegistry
from autonoma.tool_contracts import TOOL_SPECS, schemas_payload, tool_schemas


def test_is_protected_does_one_resolution_per_query(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """La protección se decide con índices en memoria: ni un `stat` extra por raíz protegida."""
    roots = [tmp_path / f"protegido-{index}" for index in range(60)]
    fs = FileSystemManager(PanicController(), extra_protected=[str(root) for root in roots])
    calls: list[str] = []
    original = Path.resolve

    def counting_resolve(self: Path, *args: object, **kwargs: object) -> Path:
        calls.append(str(self))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", counting_resolve)
    start = time.perf_counter()
    assert fs.is_protected(str(tmp_path / "protegido-3" / "subida" / "hondo.txt")) is True
    elapsed_ms = (time.perf_counter() - start) * 1000.0
    assert len(calls) == 1
    assert elapsed_ms < 25.0  # presupuesto holgado para CI lenta: el diseño es O(1)


def test_policy_queries_scale_with_depth_not_with_roots(tmp_path: Path) -> None:
    """500 raíces protegidas no multiplican el coste de una consulta: se indexa por profundidad."""
    small = ProtectedPolicy.build([tmp_path / "a"])
    huge = ProtectedPolicy.build([tmp_path / f"r{index}" for index in range(500)])
    target = normalize_parts(tmp_path / "x" / "y" / "z")
    assert small.covers(target) is False and huge.covers(target) is False
    measured_small, measured_huge = _best_of(small, target), _best_of(huge, target)
    assert measured_huge < measured_small * 3 + 0.05


def _best_of(policy: ProtectedPolicy, target: tuple[str, ...]) -> float:
    best = float("inf")
    for _ in range(3):
        start = time.perf_counter()
        for _ in range(2000):
            policy.covers(target)
        best = min(best, time.perf_counter() - start)
    return best


def test_ancestor_guard_protects_the_parent_chain(tmp_path: Path) -> None:
    home = tmp_path / "home"
    policy = ProtectedPolicy.build([home / ".ssh", home / "Documents"])
    assert policy.contains_a_protected_root(normalize_parts(home))  # borrar `home` huérfana `.ssh`
    assert policy.contains_a_protected_root(normalize_parts(home / ".ssh"))  # la raíz también
    assert policy.contains_a_protected_root(normalize_parts(home / ".ssh" / "id_ed25519")) is False
    assert policy.covers(normalize_parts(home / ".ssh" / "id_ed25519"))


def test_schemas_are_precomputed_and_read_only() -> None:
    assert len(tool_schemas()) == len(TOOL_SPECS)
    with pytest.raises(TypeError):  # inmutable de verdad: ni una copia que mutar
        tool_schemas()[0]["function"]["name"] = "mutado"
    read_file = next(schema for schema in tool_schemas() if schema["function"]["name"] == "read_file")
    with pytest.raises(TypeError):  # la congelación es recursiva, no sólo en el nivel superior
        read_file["function"]["parameters"]["properties"]["path"]["description"] = "mutado"
    assert read_file["function"]["parameters"]["properties"]["path"]["type"] == "string"
    assert tool_schemas()[0]["function"]["name"] == TOOL_SPECS[0].name
    assert tool_schemas()[0] is tool_schemas()[0]  # mismo objeto: no se reconstruye por llamada
    payload = schemas_payload()
    assert isinstance(payload, tuple) and payload[0]["function"]["name"] == TOOL_SPECS[0].name
    json.dumps(payload)  # la vista del proveedor es serializable sin copias por turno


def test_metrics_registry_lookup_is_not_a_scan() -> None:
    metrics = MetricsRegistry()
    for index in range(200):
        metrics.increment(f"tool.t{index}")
    started = time.perf_counter()
    for _ in range(50_000):
        metrics.increment("tool.t7")
    assert time.perf_counter() - started < 1.0  # 50 k contadores en < 1 s


def test_counters_snapshot_is_a_copy_not_the_internal_state() -> None:
    metrics = MetricsRegistry()
    metrics.increment("x")
    snapshot = metrics.counters()
    with pytest.raises(TypeError):
        snapshot["y"] = 2  # type: ignore[index]
