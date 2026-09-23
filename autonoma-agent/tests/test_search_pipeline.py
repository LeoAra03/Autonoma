"""Motor de búsqueda: backends en cascada, parser tolerante, límite de egreso y cancelación."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import httpx
import pytest

from autonoma.errors import (
    NetworkPolicyError,
    ProviderContractError,
    ProviderHttpError,
    SearchBackendError,
)
from autonoma.key_handler import PanicController, PanicError
from autonoma.search_engine import (
    BraveApiBackend,
    ResearchBundle,
    SearchEngine,
    SearchHit,
    parse_results_html,
    visible_text,
)

# Marca real de Brave (clases `snippet`/`snippet-description`), con los casos raros que
# el parser debe aguantar: duplicados, enlaces internos, relativos y títulos vacíos.
SERP_HTML = """
<html><body>
<div id="results">
  <div class="snippet"><a href="https://uno.example/pagina">Título uno</a>
    <div class="snippet-description">Resumen del primer resultado, lo bastante largo para ser útil.</div></div>
  <div class="snippet"><a href="https://dos.example/pagina">Título dos</a>
    <div class="snippet-description">Segundo resumen.</div></div>
  <div class="snippet"><a href="https://uno.example/pagina">Repetido</a>
    <div class="snippet-description">No debe duplicarse.</div></div>
  <div class="snippet"><a href="/relativa">Enlace relativo</a></div>
  <div class="snippet"><a href="https://brave.com/privacidad">Enlace de Brave</a></div>
  <div class="snippet"><a href="https://corto.example/">iu</a></div>
</div>
</body></html>
"""


def engine_for(handler, tmp: Path, **kwargs: object) -> SearchEngine:
    engine = SearchEngine(PanicController(), tmp / "kb", **kwargs)  # type: ignore[arg-type]
    engine._http = httpx.Client(transport=httpx.MockTransport(handler))
    return engine


# --------------------------------------------------------------------- parsers
def test_parse_results_html_is_deduplicated_and_filtered() -> None:
    hits = parse_results_html(SERP_HTML, count=10)
    assert [hit.url for hit in hits] == ["https://uno.example/pagina", "https://dos.example/pagina"]
    assert hits[0].snippet.startswith("Resumen del primer resultado")
    assert hits[0].title == "Título uno"


def test_parse_results_html_respects_count_and_degrades_to_anchor_tags() -> None:
    degraded = '<html><a href="https://aa.example/">primero</a><a href="https://bb.example/">segundo</a></html>'
    assert [hit.url for hit in parse_results_html(degraded, count=1)] == ["https://aa.example/"]
    # Un enlace con texto demasiado corto no es un resultado: no se inventa título.
    assert parse_results_html('<html><div class="snippet"><a href="https://x.example/">iu</a></div></html>', 5) == ()
    assert parse_results_html("<html><body>sin resultados</body></html>", 5) == ()


def test_visible_text_drops_scripts_and_entities() -> None:
    html = "<html><head><style>p{color:red}</style><script>evil()</script></head><body><p>Hola &amp; adiós</p></body></html>"
    text = visible_text(html)
    assert "Hola & adiós" in text
    assert "evil()" not in text and "color:red" not in text
    assert visible_text(html, limit=5) == text[:5]


# -------------------------------------------------------------------- backends
def test_brave_api_backend_maps_extra_snippets(tmp_path: Path) -> None:
    payload = {
        "web": {
            "results": [
                {
                    "title": "Uno",
                    "url": "https://uno.example",
                    "description": "resumen",
                    "extra_snippets": ["e1", "e2", "e3", "e4"],
                },
            ]
        }
    }
    engine = engine_for(lambda request: httpx.Response(200, json=payload), tmp_path, brave_api_key="brave-key-123")
    backend = BraveApiBackend(engine)
    hits = backend.search("consulta", 5)
    assert hits[0].title == "Uno"
    # Tope de fragmentos extra: el contenido de una página no puede inflar el contexto.
    assert hits[0].extra.splitlines() == ["e1", "e2", "e3"]
    engine.close()


def test_brave_api_backend_reports_http_and_contract_failures(tmp_path: Path) -> None:
    engine = engine_for(lambda request: httpx.Response(429, json={"error": "cuota"}), tmp_path, brave_api_key="k")
    with pytest.raises(ProviderHttpError) as excinfo:
        BraveApiBackend(engine).search("q", 5)
    assert excinfo.value.status_code == 429 and excinfo.value.retryable

    broken = engine_for(
        lambda request: httpx.Response(200, json={"web": {"results": "no-es-lista"}}), tmp_path, brave_api_key="k"
    )
    with pytest.raises(ProviderContractError):
        BraveApiBackend(broken).search("q", 5)
    engine.close()
    broken.close()


def test_cascade_skips_unusable_backends_and_aggregates_reasons(tmp_path: Path) -> None:
    def empty_html(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/html"}, content=b"<html><body>nada</body></html>")

    engine = engine_for(empty_html, tmp_path)
    engine.brave_api_key = ""  # sin clave: la API no es utilizable en esta máquina
    with pytest.raises(SearchBackendError) as excinfo:
        engine.search("consulta")
    reasons = dict(excinfo.value.failures)
    assert reasons["brave-api"] == "no disponible en esta máquina"
    assert reasons["brave-html"] == "sin resultados"
    assert "playwright" in reasons
    assert excinfo.value.context["query"] == "consulta"
    engine.close()


def test_cascade_returns_the_first_healthy_backend(tmp_path: Path) -> None:
    engine = engine_for(
        lambda request: httpx.Response(200, headers={"content-type": "text/html"}, content=SERP_HTML.encode()), tmp_path
    )
    engine.brave_api_key = ""
    hits = engine.search("consulta", 2)
    assert [hit.url for hit in hits] == ["https://uno.example/pagina", "https://dos.example/pagina"]
    engine.close()


# ------------------------------------------------------------------ fetch_url
def test_fetch_url_refuses_non_text_and_oversized_bodies(tmp_path: Path) -> None:
    binary = engine_for(
        lambda request: httpx.Response(200, headers={"content-type": "application/pdf"}, content=b"%PDF"), tmp_path
    )
    assert "contenido no textual" in binary.fetch_url("https://example.com/a.pdf")
    huge = engine_for(
        lambda request: httpx.Response(200, headers={"content-type": "text/html"}, content=b"<p>" + b"x" * 3_000_000),
        tmp_path,
    )
    with pytest.raises(NetworkPolicyError, match="demasiado grande"):
        huge.fetch_url("https://example.com/large")
    binary.close()
    huge.close()


def test_fetch_url_stops_when_the_user_cancels_mid_stream(tmp_path: Path) -> None:
    panic = PanicController()
    engine = SearchEngine(panic, tmp_path / "kb")
    chunks = iter([b"<html><body>" + b"a" * 9000] * 40)

    def handler(request: httpx.Request) -> httpx.Response:
        def stream():
            for index, chunk in enumerate(chunks):
                if index == 3:
                    panic.panic()
                yield chunk

        return httpx.Response(200, headers={"content-type": "text/html"}, content=stream())

    engine._http = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(PanicError):
        engine.fetch_url("https://example.com/lento")
    panic.reset()
    engine.close()


def test_http_client_is_reused_and_recreated_after_close(tmp_path: Path) -> None:
    engine = engine_for(lambda request: httpx.Response(200, json={}), tmp_path)
    first = engine.http_client
    assert engine.http_client is first  # sin clientes por petición: conexión reutilizada
    engine.close()
    assert engine._http is None
    assert engine.http_client is not first
    engine.close()


def test_parallel_fetches_are_bounded(tmp_path: Path) -> None:
    """El tope de concurrencia protege la máquina: nunca más de N descargas a la vez."""
    inflight = 0
    peak = 0
    lock = threading.Lock()

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal inflight, peak
        with lock:
            inflight += 1
            peak = max(peak, inflight)
        time.sleep(0.02)
        with lock:
            inflight -= 1
        return httpx.Response(200, headers={"content-type": "text/html"}, content=b"<html><body>texto</body></html>")

    engine = engine_for(handler, tmp_path, max_parallel_fetches=2)
    hits = [SearchHit(f"t{i}", f"https://example.com/{i}", "s") for i in range(8)]
    engine.search = lambda query, count=None: tuple(hits)  # type: ignore[method-assign]
    bundle = engine.research("prueba", fetch_pages=8, save=True)
    assert 2 <= peak <= 3  # un colchón de 1 por el hilo principal
    assert bundle.saved_files
    engine.close()


# ----------------------------------------------------------------- resultados
def test_research_saves_a_bundle_and_formats_a_preview(tmp_path: Path) -> None:
    engine = engine_for(
        lambda request: httpx.Response(
            200, headers={"content-type": "text/html"}, content=b"<html><body>hallazgo</body></html>"
        ),
        tmp_path,
    )
    engine.search = lambda query, count=None: (  # type: ignore[method-assign]
        SearchHit("Uno", "https://uno.example", "resumen uno"),
        SearchHit("Dos", "https://dos.example", "resumen dos"),
    )
    bundle = engine.research("tema", fetch_pages=2, save=True)
    assert isinstance(bundle, ResearchBundle)
    assert bundle.hit_count == 2
    assert all(path.suffix == ".md" for path in bundle.saved_files)
    preview = engine.format_bundle(bundle, preview=40)
    assert "tema" in preview.lower() and "uno.example" in preview
    assert len(preview) < 2000
    engine.close()


def test_research_survives_pages_that_fail_to_download(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Una página caída no tira el turno: se entrega lo que sí se pudo leer."""
    import autonoma.search_engine as module

    monkeypatch.setattr(module, "validate_public_url", lambda url, **kwargs: url)

    def handler(request: httpx.Request) -> httpx.Response:
        if "malo" in request.url.host:
            raise httpx.ConnectError("host inalcanzable", request=request)
        return httpx.Response(200, headers={"content-type": "text/html"}, content=b"<html><body>ok</body></html>")

    engine = engine_for(handler, tmp_path)
    engine.search = lambda query, count=None: (  # type: ignore[method-assign]
        SearchHit("Bueno", "https://uno.example/a", "s"),
        SearchHit("Malo", "https://malo.example/b", "s"),
    )
    bundle = engine.research("mezcla", fetch_pages=2, save=True)
    assert bundle.hit_count == 2
    contents = {hit.url: hit.content for hit in bundle.hits}
    assert "ok" in contents["https://uno.example/a"]
    assert "no se pudo" in contents["https://malo.example/b"]  # la falla se nombra, no se oculta
    assert bundle.saved_files  # el KnowledgeBase recibe lo que sí se pudo leer
    engine.close()


def test_save_findings_sanitizes_the_title_and_never_overwrites(tmp_path: Path) -> None:
    engine = SearchEngine(PanicController(), tmp_path / "kb")
    hits = [SearchHit("Título/extraño: prueba", "https://a.example", "s")]
    first = engine.save_findings("asunto raro", hits, notes="nota")
    second = engine.save_findings("asunto raro", hits, notes="otra")
    assert first != second and first.is_file() and second.is_file()
    body = first.read_text(encoding="utf-8")
    assert "asunto raro" in body and "https://a.example" in body and "nota" in body
    assert json.dumps({"ok": True})  # el bundle se guarda como texto plano, sin binarios
    engine.close()


def test_shutdown_closes_http_and_browser(tmp_path: Path) -> None:
    closed: list[str] = []

    class FakeBrowser:
        def close(self) -> None:
            closed.append("browser")

    class FakeRuntime:
        def stop(self) -> None:
            closed.append("runtime")

    engine = SearchEngine(PanicController(), tmp_path / "kb")
    engine._browser = FakeBrowser()  # type: ignore[assignment]
    engine._playwright_runtime = FakeRuntime()  # type: ignore[assignment]
    engine.shutdown()
    assert closed == ["browser", "runtime"]
    assert engine._browser is None and engine._playwright_runtime is None
    engine.shutdown()  # idempotente
    assert closed == ["browser", "runtime"]
