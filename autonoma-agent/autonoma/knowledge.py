"""Almacenamiento de notas: rutas confinadas y lecturas limitadas, sin transporte web."""
from __future__ import annotations
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4
from autonoma.key_handler import PanicController
from autonoma.path_policy import is_redirected

logger = logging.getLogger(__name__)

def _slug(text: str) -> str:
    return re.sub(r"[^\w-]", "-", text.lower()).strip("-")[:60] or "nota"

def _now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S") + "_" + uuid4().hex

class KnowledgeStore:
    def __init__(self, panic: PanicController, knowledge_dir: Path) -> None:
        self.panic = panic
        self.knowledge_dir = Path(knowledge_dir)
        self.knowledge_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _write_exclusive(path: Path, text: str) -> None:
        with path.open("x", encoding="utf-8") as stream:
            stream.write(text)

    # ---------------------------------------------------------- knowledge_base
    def save_findings(self, query: str, hits: list[Any], notes: str = "") -> Path:
        self.panic.check()
        self.knowledge_dir.mkdir(parents=True, exist_ok=True)
        name = f"{_now_stamp()}_{_slug(query)}.md"
        path = self.knowledge_dir / name
        blocks = [
            f"# Investigación: {query}",
            "",
            f"- fecha: {datetime.now(timezone.utc).isoformat()}",
            f"- consulta: {query}",
            f"- resultados: {len(hits)}",
            "",
        ]
        if notes:
            blocks += ["## Notas del agente", "", notes.strip(), ""]
        for i, hit in enumerate(hits, 1):
            blocks += [
                f"## {i}. {hit.title}",
                "",
                f"- url: {hit.url}",
                "",
                hit.snippet.strip(),
                "",
            ]
            if hit.extra:
                blocks += [hit.extra.strip(), ""]
            if hit.content:
                blocks += ["### Contenido extraído", "", hit.content.strip(), ""]
        self._write_exclusive(path, "\n".join(blocks).strip() + "\n")
        logger.info("Knowledge guardado: %s", path)
        return path

    def save_note(self, title: str, body: str, source: str = "agente") -> Path:
        self.panic.check()
        self.knowledge_dir.mkdir(parents=True, exist_ok=True)
        path = self.knowledge_dir / f"{_now_stamp()}_{_slug(title)}.md"
        text = (
            f"# {title}\n\n"
            f"- fecha: {datetime.now(timezone.utc).isoformat()}\n"
            f"- fuente: {source}\n\n"
            f"{body.strip()}\n"
        )
        self._write_exclusive(path, text)
        return path

    def list_notes(self, limit: int = 40) -> list[Path]:
        if not self.knowledge_dir.is_dir():
            return []
        files = sorted(
            [p for p in self.knowledge_dir.iterdir() if p.suffix.lower() in {".md", ".txt"} and p.is_file() and not is_redirected(p)],
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        return files[:limit]

    def read_note(self, name_or_path: str, max_chars: int = 20_000) -> str:
        base = self.knowledge_dir.resolve()
        candidate = (base / name_or_path).resolve()
        if not candidate.is_relative_to(base):
            raise ValueError("La nota debe estar dentro de knowledge_base")
        if not candidate.is_file():
            matches = [
                p
                for p in self.list_notes(200)
                if name_or_path.lower() in p.name.lower()
            ]
            if not matches:
                raise FileNotFoundError(f"No hay nota que coincida con {name_or_path!r}")
            candidate = matches[0]
        with candidate.open(encoding="utf-8", errors="replace") as stream:
            data = stream.read(max_chars + 1)
        if len(data) > max_chars:
            return data[:max_chars] + "\n\n…[truncado]"
        return f"# archivo: {candidate.name}\n\n{data}"

    def context_digest(self, limit_files: int = 8, per_file: int = 1800) -> str:
        """Resumen del knowledge_base para inyectar como contexto al modelo."""
        files = self.list_notes(limit_files)
        if not files:
            return "(knowledge_base vacía)"
        chunks = ["Notas recientes en knowledge_base:"]
        for path in files:
            try:
                with path.open(encoding="utf-8", errors="replace") as stream:
                    text = stream.read(80_000)
            except OSError:
                continue
            chunks.append(f"\n--- {path.name} ---\n{text[:per_file]}")
        return "\n".join(chunks)

    def search_notes(self, query: str, limit: int = 6) -> str:
        tokens = [t.lower() for t in re.split(r"\s+", query) if t]
        scored: list[tuple[int, Path, str]] = []
        for path in self.list_notes(200):
            try:
                with path.open(encoding="utf-8", errors="replace") as stream:
                    text = stream.read(80_000)
            except OSError:
                continue
            blob = (path.name + "\n" + text).lower()
            score = sum(blob.count(tok) for tok in tokens) if tokens else 1
            if score:
                scored.append((score, path, text))
        scored.sort(key=lambda x: x[0], reverse=True)
        if not scored:
            return f"Sin coincidencias en knowledge_base para {query!r}"
        parts = []
        for score, path, text in scored[:limit]:
            parts.append(f"### {path.name} (score={score})\n{text[:2500]}")
        return "\n\n".join(parts)
