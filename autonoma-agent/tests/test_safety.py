"""Pruebas locales sin API: protecciones de disco y knowledge_base."""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from autonoma.filesystem import FileSystemError, FileSystemManager  # noqa: E402
from autonoma.key_handler import PanicController  # noqa: E402
from autonoma.search_engine import SearchEngine  # noqa: E402


def test_protected_paths() -> None:
    panic = PanicController()
    fs = FileSystemManager(panic)
    if os.name == "nt":
        assert fs.is_protected(r"C:\Windows\System32\cmd.exe")
        assert fs.is_protected(r"C:\Windows")
    else:
        assert fs.is_protected("/etc/passwd")
        assert fs.is_protected("/usr/bin/python")
        assert not fs.is_protected(str(Path.home() / "autonoma-test-file.txt"))
        assert not fs.is_protected("/tmp")


def test_delete_blocked(tmp_path: Path | None = None) -> None:
    panic = PanicController()
    fs = FileSystemManager(panic)
    target = "/etc/hostname" if os.name != "nt" else r"C:\Windows\System32\drivers\etc\hosts"
    try:
        fs.delete_path(target, force=False, user_prompt="borra algo")
        raise AssertionError("debía bloquearse")
    except FileSystemError as exc:
        assert "protegida" in str(exc).lower() or "protegido" in str(exc).lower()


def test_knowledge_roundtrip(tmp_path: Path) -> None:
    panic = PanicController()
    kb = tmp_path / "knowledge_base"
    engine = SearchEngine(panic=panic, knowledge_dir=kb)
    path = engine.save_note("Prueba", "contenido de prueba")
    assert path.is_file()
    text = engine.read_note("Prueba")
    assert "contenido de prueba" in text
    digest = engine.context_digest()
    assert "Prueba" in digest or "prueba" in digest.lower() or "contenido" in digest


def test_panic_flag() -> None:
    panic = PanicController()
    panic.mark_busy()
    panic.panic()
    assert panic.is_set
    try:
        panic.check()
        raise AssertionError("check debía lanzar")
    except Exception as exc:
        assert "Detenido" in str(exc)


if __name__ == "__main__":
    from pathlib import Path as P
    import tempfile

    test_protected_paths()
    test_delete_blocked()
    test_panic_flag()
    with tempfile.TemporaryDirectory() as d:
        test_knowledge_roundtrip(P(d))
    print("ok")
