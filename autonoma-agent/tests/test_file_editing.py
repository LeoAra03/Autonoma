"""Edición quirúrgica, ventanas de lectura y `grep` propio: el contrato de `text_ops`.

Estas pruebas existen porque la edición por sustitución es la operación más capaz y más
destructora del agente: se vigila el rechazo a la ambigüedad, la atomicidad y el hecho de
que una lectura parcial siempre le diga al modelo que hay más archivo detrás.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from autonoma.errors import ToolContractError
from autonoma.filesystem import FileSystemError, FileSystemManager
from autonoma.key_handler import PanicController
from autonoma.text_ops import apply_edit, grep_tree, read_window
from autonoma.tool_contracts import validate_arguments
from autonoma.tool_registry import ToolRegistry


@pytest.fixture
def fs() -> FileSystemManager:
    return FileSystemManager(PanicController())


# ─────────────────────────────────────────────────────────────── apply_edit
def test_apply_edit_replaces_only_the_first_match() -> None:
    text = "cabeza\ndos\ncabecera\ntres"
    updated, outcome = apply_edit(text, "dos", "DOS\nextra", all_occurrences=False, path="a.txt")
    assert updated == "cabeza\nDOS\nextra\ncabecera\ntres"
    assert (outcome.replacements, outcome.first_line, outcome.line_delta) == (1, 2, 1)
    assert "línea 2" in outcome.summary()


def test_apply_edit_all_occurrences_counts_every_site() -> None:
    updated, outcome = apply_edit("x x x", "x", "y", all_occurrences=True)
    assert updated == "y y y"
    assert outcome.replacements == 3
    assert "3 sitios desde la línea 1" in outcome.summary()


def test_apply_edit_reports_line_delta_when_the_edit_grows() -> None:
    _, outcome = apply_edit("a\nb\n", "b", "B1\nB2", all_occurrences=False)
    assert outcome.line_delta == 1


@pytest.mark.parametrize(
    ("find", "replace", "expected"),
    [
        ("", "nada", "no puede estar vacío"),
        ("a", "a", "idénticos"),
        ("zzz", "y", "intacto"),
    ],
)
def test_apply_edit_refuses_useless_inputs(find: str, replace: str, expected: str) -> None:
    with pytest.raises(FileSystemError, match=expected):
        apply_edit("abc", find, replace, all_occurrences=False, path="a.txt")


def test_ambiguous_edit_is_rejected_not_guessed() -> None:
    with pytest.raises(FileSystemError, match="2 coincidencias") as exc:
        apply_edit("x\nx", "x", "y", all_occurrences=False)
    assert "all=true" in str(exc.value.context["sugerencia"])


# ─────────────────────────────────────────────────────────────── ventanas
def test_read_window_by_lines_carries_the_range_header() -> None:
    text = "\n".join(f"línea {n}" for n in range(1, 101))
    window = read_window(text, start_line=3, max_lines=2, path="f.py")
    assert window.startswith("f.py:3-4 de 100 líneas")
    assert "línea 5" not in window


def test_read_window_char_cut_says_the_file_continues() -> None:
    out = read_window("abcdefghij", max_lines=0, max_chars=4)
    assert out.startswith("abcd")
    assert "truncado" in out


def test_read_window_rejects_a_start_beyond_the_end() -> None:
    with pytest.raises(FileSystemError, match="más allá del final"):
        read_window("una\nDos", start_line=99, max_lines=10, path="c.txt")


def test_read_file_returns_the_whole_text_when_it_fits(fs, tmp_path: Path) -> None:
    target = tmp_path / "chico.md"
    target.write_text("sin cortes\n", encoding="utf-8")
    assert fs.read_file(str(target)) == "sin cortes\n"


def test_read_file_paginates_so_the_model_can_walk_a_big_file(fs, tmp_path: Path) -> None:
    target = tmp_path / "grande.py"
    target.write_text("\n".join(f"l{i}" for i in range(3_000)), encoding="utf-8")
    first = fs.read_file(str(target), start_line=1, max_lines=50)
    assert first.count("\n") == 50  # cabecera + 49 líneas de cuerpo
    later = fs.read_file(str(target), start_line=2_990, max_lines=50)
    assert "2990-3000 de 3000 líneas" in later
    assert "l2999" in later


def test_read_file_honours_the_configured_limit(tmp_path: Path) -> None:
    limited = FileSystemManager(PanicController(), read_limit_chars=300)
    target = tmp_path / "capped.txt"
    target.write_text("x" * 500, encoding="utf-8")
    out = limited.read_file(str(target))
    assert "truncado" in out and "300 caracteres" in out


# ─────────────────────────────────────────────────────────────── grep_tree
def test_grep_tree_skips_binaries_and_artifact_directories(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("BUSCADA aquí\n", encoding="utf-8")
    (tmp_path / "src" / "b.bin").write_bytes(b"BUSCADA\x00raro")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "c.js").write_text("BUSCADA basura\n", encoding="utf-8")
    outcome = grep_tree(tmp_path, "BUSCADA")
    assert outcome.matches == ("src/a.py:1: BUSCADA aquí",)  # `node_modules` y binarios fuera
    assert outcome.skipped_binary == 1
    assert "omitidos" in outcome.summary("BUSCADA", str(tmp_path))


def test_grep_tree_flags_truncated_results_and_bad_patterns(tmp_path: Path) -> None:
    for n in range(10):
        (tmp_path / f"f{n}.txt").write_text("hit\n", encoding="utf-8")
    outcome = grep_tree(tmp_path, "hit", max_results=3)
    assert len(outcome.matches) == 3
    assert outcome.truncated
    assert "amplía max_results" in outcome.summary("hit", str(tmp_path))
    with pytest.raises(FileSystemError, match="Patrón inválido"):
        grep_tree(tmp_path, "([unclosed")


def test_grep_tree_can_be_case_sensitive(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("Dato\n", encoding="utf-8")
    assert grep_tree(tmp_path, "Dato", ignore_case=True).matches == ("a.txt:1: Dato",)
    assert grep_tree(tmp_path, "dato", ignore_case=False).matches == ()
    assert grep_tree(tmp_path, "Dato", ignore_case=False).matches == ("a.txt:1: Dato",)


def test_default_glob_reaches_the_files_at_the_root(tmp_path: Path) -> None:
    """`**/*` es `Path.glob`, no `fnmatch`: `README.md` tiene que aparecer.

    Con la semántica de `fnmatch` una búsqueda sin `glob` devolvía "sin coincidencias"
    justo en los archivos sueltos del directorio que el usuario acaba de nombrar.
    """
    (tmp_path / "README.md").write_text("AGUJA en la raíz\n", encoding="utf-8")
    (tmp_path / "src" / "deep").mkdir(parents=True)
    (tmp_path / "src" / "deep" / "otro.py").write_text("AGUJA hondo\n", encoding="utf-8")
    assert sorted(grep_tree(tmp_path, "AGUJA").matches) == [
        "README.md:1: AGUJA en la raíz",
        "src/deep/otro.py:1: AGUJA hondo",
    ]
    assert grep_tree(tmp_path, "AGUJA", glob="*.md").matches == ("README.md:1: AGUJA en la raíz",)
    assert grep_tree(tmp_path, "AGUJA", glob="**/*.py").matches == ("src/deep/otro.py:1: AGUJA hondo",)


def test_search_summary_counts_files_in_the_singular(tmp_path: Path) -> None:
    """El resumen se lee en voz alta: "1 coincidencias" delataba texto de máquina."""
    manager = FileSystemManager(PanicController())
    (tmp_path / "único.txt").write_text("aguja\n", encoding="utf-8")
    out = manager.search_files("aguja", str(tmp_path))
    assert out.startswith("1 coincidencia de ")
    assert "1 archivos leídos" in out


def test_search_files_reports_no_matches_without_failing(fs, tmp_path: Path) -> None:
    (tmp_path / "vacío.txt").write_text("nada que ver\n", encoding="utf-8")
    out = fs.search_files("aguja", str(tmp_path))
    assert "sin coincidencias" in out


# ─────────────────────────────────────────────────────────────── editar disco
def test_edit_file_writes_atomically_and_keeps_a_backup(tmp_path: Path) -> None:
    manager = FileSystemManager(PanicController())
    target = tmp_path / "nota.md"
    target.write_text("uno\ndos\n", encoding="utf-8")
    summary = manager.edit_file(str(target), "dos", "DOS")
    assert target.read_text(encoding="utf-8") == "uno\nDOS\n"
    assert "editado" in summary.lower() or "línea 2" in summary
    assert not list(tmp_path.glob("*.tmp"))  # `atomic_write_text` limpia sus auxiliares


def test_edit_file_refuses_files_that_are_not_valid_utf8(fs, tmp_path: Path) -> None:
    target = tmp_path / "roto.txt"
    target.write_bytes("mañana".encode() + b"\xff\xfe")
    before = target.read_bytes()
    with pytest.raises(FileSystemError, match="UTF-8"):
        fs.edit_file(str(target), "ma", "ME")
    assert target.read_bytes() == before  # intacto, no reescrito con reemplazos


def test_edit_file_protects_the_paths_it_should_not_touch(fs, tmp_path: Path) -> None:
    secret = tmp_path / ".env"
    secret.write_text("K=1\n", encoding="utf-8")
    guarded = FileSystemManager(PanicController(), extra_protected=[str(tmp_path)])
    with pytest.raises(FileSystemError, match="protegida"):
        guarded.edit_file(str(secret), "K=1", "K=2")
    assert secret.read_text(encoding="utf-8") == "K=1\n"


def test_edit_file_does_not_invent_a_missing_target(fs, tmp_path: Path) -> None:
    with pytest.raises(FileSystemError, match="No se pudo leer"):
        fs.edit_file(str(tmp_path / "inexistente.txt"), "a", "b")
    assert not (tmp_path / "inexistente.txt").exists()


def test_append_file_creates_parents_and_appends(fs, tmp_path: Path) -> None:
    target = tmp_path / "logs" / "día.log"
    assert "Anexado" in fs.append_file(str(target), "primera\n")
    fs.append_file(str(target), "segunda\n")
    assert target.read_text(encoding="utf-8") == "primera\nsegunda\n"


# ─────────────────────────────────────────────────────────────── herramientas
class _NoNetEngine:
    """Sustituto mínimo del motor: el registro sólo necesita `search`/`fetch_url`/`store`."""

    def __init__(self, knowledge_dir: Path) -> None:
        from autonoma.knowledge import KnowledgeStore

        self.store = KnowledgeStore(PanicController(), knowledge_dir)

    def search(self, *args: object, **kwargs: object) -> str:
        return "sin red"

    def fetch_url(self, *args: object, **kwargs: object) -> str:
        return "sin red"


def test_registry_exposes_the_editing_and_job_tools(tmp_path: Path) -> None:
    registry = ToolRegistry(_NoNetEngine(tmp_path), FileSystemManager(PanicController()))
    assert {"edit_file", "search_files", "append_file"} <= set(registry.tool_names)
    assert {"spawn_command", "job_status", "job_output", "kill_job"} <= set(registry.tool_names)


def test_edit_tool_rejects_ambiguous_match_before_touching_disk(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    panic = PanicController()
    (tmp_path / "d.txt").write_text("x\nx\n", encoding="utf-8")
    registry = ToolRegistry(_NoNetEngine(tmp_path), FileSystemManager(panic))
    with pytest.raises(FileSystemError, match="ambigua"):
        registry.execute("edit_file", {"path": "d.txt", "find": "x", "replace": "y"})
    assert (tmp_path / "d.txt").read_text(encoding="utf-8") == "x\nx\n"
    # `all` es un flag: el contrato lo rechaza antes de que la herramienta decida nada.
    with pytest.raises(ToolContractError, match="all"):
        validate_arguments("edit_file", {"path": "d.txt", "find": "x", "replace": "y", "all": "sí"})
    assert registry.execute("edit_file", {"path": "d.txt", "find": "x", "replace": "y", "all": True})
    assert (tmp_path / "d.txt").read_text(encoding="utf-8") == "y\ny\n"
