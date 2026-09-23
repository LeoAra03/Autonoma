"""Investigación web con backends conmutables y extracción paralela de páginas.

Cambios de fondo:
- Strategy explícito: `BraveApiBackend` → `PlaywrightBackend` → `BraveHtmlBackend`.
  El orden se declara una vez y el bucle de fallback es genérico (antes: tres
  bloques `try/except Exception` casi idénticos que silenciaban causas).
- Falta de resultados y fallo del backend se diferencian; el error final es
  `SearchBackendError` con el detalle por backend (y sin secretos).
- Las páginas top se descargan en paralelo con un charco de trabajadores acotado:
  `research` pasa de O(páginas × RTT) a ~O(RTT + páginas/capacidad).
- `SearchHit`/`ResearchBundle` inmutables: el contenido extraído se añade con
  `dataclasses.replace`, nunca mutando el objeto que ya se devolvió a otro consumidor.
- El almacén de notas se compone (`self.store`) en lugar de heredarlo.
"""

from __future__ import annotations

import logging
import re
import threading
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from html import unescape
from pathlib import Path
from typing import Any, Final, Protocol
from urllib.parse import quote_plus, urlparse

import httpx
from bs4 import BeautifulSoup

from autonoma.errors import (
    AutonomaError,
    NetworkPolicyError,
    ProviderContractError,
    ProviderHttpError,
    SearchBackendError,
)
from autonoma.key_handler import PanicController, PanicError
from autonoma.knowledge import KnowledgeStore
from autonoma.network import validate_public_url

__all__ = [
    "BackendFailure",
    "ResearchBundle",
    "SearchBackend",
    "SearchEngine",
    "SearchHit",
]

logger = logging.getLogger(__name__)

BRAVE_SEARCH_URL: Final[str] = "https://api.search.brave.com/res/v1/web/search"
BRAVE_WEB_URL: Final[str] = "https://search.brave.com/search"
USER_AGENT: Final[str] = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
_MAX_PAGE_BYTES: Final[int] = 2_000_000
_TEXT_LIMIT: Final[int] = 12_000
_MAX_PARALLEL_FETCHES: Final[int] = 3
_MAX_EXTRA_SNIPPETS: Final[int] = 3
_MAX_FETCH_PAGES: Final[int] = 5
_BLOCKED_TAGS: Final[tuple[str, ...]] = (
    "script",
    "style",
    "noscript",
    "svg",
    "iframe",
    "header",
    "footer",
    "nav",
    "form",
)
_RESULT_SELECTORS: Final[tuple[str, ...]] = ("div.snippet", "div[data-type='web']", "div.fdb", "div#results div")
_SNIPPET_SELECTORS: Final[tuple[str, ...]] = (".snippet-description", ".snippet-content", "p")
_TITLE_MIN_CHARS: Final[int] = 3


@dataclass(frozen=True, slots=True)
class SearchHit:
    """Resultado de búsqueda; inmutable, con el texto extraído añadido por `replace`."""

    title: str
    url: str
    snippet: str = ""
    extra: str = ""
    content: str = ""

    def with_content(self, content: str) -> SearchHit:
        return replace(self, content=content)


@dataclass(frozen=True, slots=True)
class BackendFailure:
    """Causa acotada de por qué un backend no aportó resultados."""

    backend: str
    reason: str


@dataclass(frozen=True, slots=True)
class ResearchBundle:
    """Paquete de investigación listo para el modelo y para la nota en disco."""

    query: str
    hits: tuple[SearchHit, ...] = ()
    saved_files: tuple[Path, ...] = ()
    backend: str = ""
    notes: str = ""

    @property
    def hit_count(self) -> int:
        return len(self.hits)


@dataclass(frozen=True, slots=True)
class _BraveCandidate:
    url: str
    title: str
    snippet: str


def visible_text(html: str, limit: int = _TEXT_LIMIT) -> str:
    """Texto visible sin scripts ni maquetación, colapsado y recortado."""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(_BLOCKED_TAGS):
        tag.decompose()
    text = soup.get_text("\n")
    lines = [unescape(line).strip() for line in text.splitlines()]
    compact = re.sub(r"\n{3,}", "\n\n", "\n".join(line for line in lines if line))
    return compact[:limit]


def _brave_executable() -> str | None:
    """Rutas conocidas de Brave; se comprueban en orden y sin ejecutar nada."""
    for raw in _BRAVE_CANDIDATES:
        path = Path(raw)
        if path.is_file():
            return str(path)
    return None


_BRAVE_CANDIDATES: Final[tuple[str, ...]] = (
    r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
    r"C:\Program Files (x86)\BraveSoftware\Brave-Browser\Application\brave.exe",
    "/usr/bin/brave-browser",
    "/usr/bin/brave",
    "/usr/bin/brave-browser-stable",
    "/snap/bin/brave",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
)


class SearchBackend(Protocol):
    """Contrato mínimo de un backend de búsqueda."""

    name: str

    def is_usable(self) -> bool: ...

    def search(self, query: str, count: int) -> tuple[SearchHit, ...]: ...


class _HttpBacked:
    """Acceso común al cliente HTTP y a la revisión de cancelación."""

    __slots__ = ("_engine",)

    def __init__(self, engine: SearchEngine) -> None:
        self._engine = engine

    @property
    def client(self) -> httpx.Client:
        return self._engine.http_client

    @property
    def panic(self) -> PanicController:
        return self._engine.panic


class BraveApiBackend(_HttpBacked):
    """API oficial de Brave: el único backend con resultados estructurados fiables."""

    name = "brave-api"

    def is_usable(self) -> bool:
        return bool(self._engine.brave_api_key)

    def search(self, query: str, count: int) -> tuple[SearchHit, ...]:
        self.panic.check()
        response = self.client.get(
            BRAVE_SEARCH_URL,
            params={"q": query, "count": min(count, 20), "extra_snippets": True},
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "gzip",
                "X-Subscription-Token": self._engine.brave_api_key,
            },
        )
        self.panic.check()
        if response.status_code >= 400:
            # El cuerpo del error va sólo al `context` (redactado en el log): puede reenviar
            # la consulta o fragmentos de la cabecera de autenticación.
            raise ProviderHttpError(
                f"Brave respondió HTTP {response.status_code}",
                status_code=response.status_code,
                context={"body": response.text[:240]},
            )
        payload = response.json()
        results = (payload.get("web") or {}).get("results") or []
        if not isinstance(results, list):
            raise ProviderContractError("respuesta de Brave sin lista `web.results`")
        return tuple(_hit_from_api(item) for item in results[:count])


class BraveHtmlBackend(_HttpBacked):
    """Último recurso: scrape del HTML de la búsqueda pública."""

    name = "brave-html"

    def is_usable(self) -> bool:
        return True

    def search(self, query: str, count: int) -> tuple[SearchHit, ...]:
        self.panic.check()
        response = self.client.get(
            f"{BRAVE_WEB_URL}?q={quote_plus(query)}",
            headers={"Accept-Language": "es,en;q=0.8"},
        )
        self.panic.check()
        response.raise_for_status()
        return parse_results_html(response.text, count)


class PlaywrightBackend(_HttpBacked):
    """Navegador real (Chromium/Brave) cuando no hay API key ni HTML útil."""

    name = "playwright"

    def is_usable(self) -> bool:
        try:
            import playwright.sync_api  # noqa: F401
        except ImportError:
            return False
        return True

    def search(self, query: str, count: int) -> tuple[SearchHit, ...]:
        return self._engine.playwright_search(query, count)


def _hit_from_api(item: Any) -> SearchHit:
    extras = item.get("extra_snippets") or []
    extra_text = "\n".join(str(extra) for extra in list(extras)[:_MAX_EXTRA_SNIPPETS])
    return SearchHit(
        title=str(item.get("title") or ""),
        url=str(item.get("url") or ""),
        snippet=str(item.get("description") or ""),
        extra=extra_text,
    )


def parse_results_html(html: str, count: int) -> tuple[SearchHit, ...]:
    """Extracción tolerante: si Brave cambia el marcado, se degrada a `a[href]`."""
    soup = BeautifulSoup(html, "lxml")
    nodes = _result_nodes(soup)
    hits: list[SearchHit] = []
    seen: set[str] = set()
    for node in nodes:
        candidate = _candidate_from_node(node)
        if candidate is None or candidate.url in seen:
            continue
        seen.add(candidate.url)
        hits.append(SearchHit(title=candidate.title, url=candidate.url, snippet=candidate.snippet))
        if len(hits) >= count:
            break
    return tuple(hits)


def _result_nodes(soup: Any) -> list[Any]:
    for selector in _RESULT_SELECTORS:
        found = soup.select(selector)
        if found:
            return list(found)
    return list(soup.select("a[href]"))


def _candidate_from_node(node: Any) -> _BraveCandidate | None:
    link = node if getattr(node, "name", "") == "a" else node.find("a", href=True)
    if link is None:
        return None
    href = str(link.get("href") or "").strip()
    if not href.startswith("http"):
        return None
    host = urlparse(href).netloc.lower()
    if "brave.com" in host or "brave.search" in host:
        return None
    title = link.get_text(" ", strip=True) or href
    if len(title) < _TITLE_MIN_CHARS:
        return None
    return _BraveCandidate(url=href, title=title[:300], snippet=_snippet_of(node)[:800])


def _snippet_of(node: Any) -> str:
    select_one = getattr(node, "select_one", None)
    if not callable(select_one):
        return ""
    for selector in _SNIPPET_SELECTORS:
        found = select_one(selector)
        if found is not None:
            return str(found.get_text(" ", strip=True))
    return ""


def _slice_window(text: str, *, start: int, limit: int) -> str:
    """Recorte con aviso del resto: sin esa pista el modelo no sabe si debe pedir más."""
    if start >= len(text):
        return f"[no hay texto en el desplazamiento {start}; la página tiene {len(text)} caracteres]"
    body = text[start : start + limit]
    remaining = len(text) - (start + len(body))
    if remaining <= 0:
        return body
    return f"{body}\n\n…[{remaining} caracteres más; continúa con start_char={start + len(body)}]"


class SearchEngine:
    """Orquesta backends, extracción de páginas y el almacén de notas."""

    def __init__(
        self,
        panic: PanicController,
        knowledge_dir: Path,
        brave_api_key: str = "",
        timeout: float = 30.0,
        default_count: int = 5,
        fetch_pages: int = 3,
        *,
        max_parallel_fetches: int = _MAX_PARALLEL_FETCHES,
        text_limit: int = _TEXT_LIMIT,
        max_page_bytes: int = _MAX_PAGE_BYTES,
        allow_private_network: bool = False,
    ) -> None:
        self.panic = panic
        self.knowledge_dir = Path(knowledge_dir)
        self.store = KnowledgeStore(panic, self.knowledge_dir)
        self.brave_api_key = (brave_api_key or "").strip()
        self.timeout = timeout
        self.default_count = default_count
        self.fetch_pages = fetch_pages
        self.max_parallel_fetches = max(1, min(max_parallel_fetches, _MAX_PARALLEL_FETCHES))
        # Ventanas de texto y tope de descarga: configurables porque 12 000 caracteres no
        # cubren una página de documentación, y recorrerla a ciegas costaba más re-descargas.
        self.text_limit = max(256, int(text_limit))
        self.max_page_bytes = max(65_536, int(max_page_bytes))
        self.allow_private_network = bool(allow_private_network)
        self._page_cache: tuple[str, str] | None = None
        self._http: httpx.Client | None = None
        self._http_lock = threading.Lock()
        self._browser_lock = threading.Lock()
        self._playwright_runtime: Any = None
        self._browser: Any = None
        self.backends: tuple[SearchBackend, ...] = (
            BraveApiBackend(self),
            PlaywrightBackend(self),
            BraveHtmlBackend(self),
        )
        # Sólo se cancela HTTP desde el hilo de pánico: Playwright no es thread-safe
        # y lo cierra el propio hilo de trabajo en su bloque finally.
        panic.register_cleanup(self.close)

    # ---------------------------------------------------------------- recursos
    @property
    def http_client(self) -> httpx.Client:
        return self._ensure_client()

    def _ensure_client(self) -> httpx.Client:
        """Cliente creado una sola vez, bajo lock: es thread-safe compartirlo."""
        client = self._http
        if client is not None and not client.is_closed:
            return client
        with self._http_lock:
            if self._http is None or self._http.is_closed:
                self._http = httpx.Client(
                    timeout=httpx.Timeout(self.timeout, connect=10.0),
                    headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/json"},
                    follow_redirects=False,
                )
            return self._http

    def close(self) -> None:
        """Cierra el cliente HTTP (seguro desde el hilo de pánico) y su registro de limpieza."""
        self.panic.unregister_cleanup(self.close)
        client = self._http
        self._http = None
        if client is not None and not client.is_closed:
            try:
                client.close()
            except httpx.HTTPError as exc:
                logger.debug(
                    "cierre http",
                    extra={"event": "search.close_error", "fields": {"error": type(exc).__name__}},
                )

    def shutdown(self) -> None:
        """Cierre completo del proceso: HTTP y navegador."""
        self.close()
        self._close_browser()

    def _close_browser(self) -> None:
        with self._browser_lock:
            browser, runtime = self._browser, self._playwright_runtime
            self._browser = None
            self._playwright_runtime = None
        for item, method in ((browser, "close"), (runtime, "stop")):
            if item is None:
                continue
            try:
                getattr(item, method)()
            except Exception as exc:  # noqa: BLE001 — el cierre no puede tumbar el turno
                logger.debug(
                    "cierre playwright",
                    extra={"event": "search.playwright_close_error", "fields": {"error": type(exc).__name__}},
                )

    # ---------------------------------------------------------------- búsqueda
    def search(self, query: str, count: int | None = None) -> tuple[SearchHit, ...]:
        """Prueba los backends en orden declarado y devuelve el primer resultado útil."""
        self.panic.check()
        limit = count or self.default_count
        failures: list[BackendFailure] = []
        for backend in self.backends:
            if not backend.is_usable():
                failures.append(BackendFailure(backend.name, "no disponible en esta máquina"))
                continue
            try:
                hits = backend.search(query, limit)
            except PanicError:
                raise
            except Exception as exc:  # noqa: BLE001 — un backend caído degrada, no aborta el turno
                failures.append(BackendFailure(backend.name, _reason(exc)))
                logger.warning(
                    "backend de búsqueda falló",
                    extra={"event": "search.backend_error", "fields": {"backend": backend.name, "error": _reason(exc)}},
                )
                continue
            if hits:
                logger.info(
                    "backend de búsqueda respondió",
                    extra={"event": "search.backend_ok", "fields": {"backend": backend.name, "hits": len(hits)}},
                )
                return hits
            failures.append(BackendFailure(backend.name, "sin resultados"))
        raise SearchBackendError(
            "Búsqueda fallida en todos los backends",
            failures=tuple((failure.backend, failure.reason) for failure in failures),
            context={"query": query[:180]},
        )

    def playwright_search(self, query: str, count: int) -> tuple[SearchHit, ...]:
        """Ruta interna del navegador: el hilo de trabajo es el dueño del runtime."""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise SearchBackendError(
                "playwright no instalado; instala el extra [browser] o usa la API de Brave",
                failures=(("playwright", "importación ausente"),),
            ) from exc
        self.panic.check()
        with self._browser_lock:
            runtime = sync_playwright().start()
            self._playwright_runtime = runtime
            launch_kwargs: dict[str, Any] = {"headless": True}
            brave = _brave_executable()
            if brave:
                launch_kwargs["executable_path"] = brave
            try:
                browser = runtime.chromium.launch(**launch_kwargs)
            except Exception as exc:  # noqa: BLE001 — el navegador del sistema puede rechazar `executable_path`
                logger.debug(
                    "lanzamiento con executable_path falló; se reintenta sin él",
                    extra={"event": "search.browser_fallback", "fields": {"launcher": type(exc).__name__}},
                )
                browser = runtime.chromium.launch(headless=True)
            self._browser = browser
        try:
            html = self._read_playwright_page(browser, query)
        finally:
            self._close_browser()
        return parse_results_html(html, count)

    def _read_playwright_page(self, browser: Any, query: str) -> str:
        page = browser.new_page(user_agent=USER_AGENT)
        try:
            page.set_default_timeout(int(self.timeout * 1000))
            self.panic.check()
            page.goto(f"{BRAVE_WEB_URL}?q={quote_plus(query)}", wait_until="domcontentloaded")
            try:
                page.wait_for_selector("div.snippet, a.result-header, #results", timeout=8000)
            except Exception as exc:  # noqa: BLE001 — selectores del SERP cambian: se degrada a una espera fija
                logger.debug(
                    "selector de resultados no apareció; se usa espera fija",
                    extra={"event": "search.selector_miss", "fields": {"error": type(exc).__name__}},
                )
                page.wait_for_timeout(1500)
            self.panic.check()
            return str(page.content())
        finally:
            try:
                page.close()
            except Exception:  # noqa: BLE001
                logger.debug("no se pudo cerrar la página de Playwright")

    # -------------------------------------------------------------- extracción
    def fetch_url(self, url: str, limit: int | None = None, *, start: int = 0) -> str:
        """Texto visible de un destino, en ventanas, sin seguir redirecciones y con tope de bytes.

        Los fallos de transporte y de estado se convierten en errores del proyecto: el
        llamador (y el modelo) reciben una explicación accionable, no un trazado de `httpx`
        con la URL dentro. La página leída queda en un cache de una entrada, así que pedir
        la ventana siguiente no vuelve a descargar nada.
        """
        self.panic.check()
        text = self._page_text(url)
        window = self.text_limit if limit is None else max(256, int(limit))
        return _slice_window(text, start=max(0, int(start)), limit=window)

    def _page_text(self, url: str) -> str:
        """Página completa ya extraída (o descargada ahora), con la política de egreso aplicada."""
        validate_public_url(url, allow_private=self.allow_private_network)
        cached = self._page_cache
        if cached is not None and cached[0] == url:
            return cached[1]
        text = self._download_visible_text(url)
        self._page_cache = (url, text)
        return text

    def _download_visible_text(self, url: str) -> str:
        client = self._ensure_client()
        try:
            # `follow_redirects=False` también por petición: un cliente compartido mal
            # configurado no debe poder redirigir hacia la red local.
            with client.stream(
                "GET", url, headers={"Accept": "text/html,text/plain"}, follow_redirects=False
            ) as response:
                if 300 <= response.status_code < 400:
                    # Política de egreso: una redirección puede apuntar a la red local,
                    # así que se informa del destino pero nunca se persigue.
                    location = response.headers.get("location", "")
                    raise NetworkPolicyError(
                        f"El destino redirige (HTTP {response.status_code}); no se siguen redirecciones",
                        context={
                            "host": urlparse(url).netloc,
                            "status": response.status_code,
                            "redirect_host": urlparse(location, url).netloc,
                        },
                    )
                if response.status_code >= 400:
                    raise SearchBackendError(
                        f"El destino respondió HTTP {response.status_code}",
                        failures=(("fetch", f"HTTP {response.status_code}"),),
                        context={"host": urlparse(url).netloc, "status": response.status_code},
                    )
                content_type = response.headers.get("content-type", "").lower()
                if not any(kind in content_type for kind in ("html", "xml", "text")):
                    return f"[contenido no textual: {content_type or 'desconocido'}]"
                body = bytearray()
                for chunk in response.iter_bytes(chunk_size=8192):
                    self.panic.check()
                    body.extend(chunk)
                    if len(body) > self.max_page_bytes:
                        raise NetworkPolicyError(
                            f"Página demasiado grande (máximo {self.max_page_bytes} bytes)",
                            context={"limit": self.max_page_bytes},
                        )
                return visible_text(
                    body.decode(response.encoding or "utf-8", errors="replace"), limit=self.max_page_bytes
                )
        except AutonomaError:
            raise
        except (PanicError, KeyboardInterrupt):
            raise
        except httpx.HTTPError as exc:
            raise SearchBackendError(
                "No se pudo descargar el destino; la conexión falló antes de leer el contenido",
                failures=(("fetch", type(exc).__name__),),
                context={"host": urlparse(url).netloc},
            ) from exc

    def _fetch_or_error(self, url: str, limit: int) -> str:
        try:
            return self.fetch_url(url, limit if limit > 0 else None)
        except PanicError:
            raise
        except Exception as exc:  # noqa: BLE001
            return f"(no se pudo extraer: {_reason(exc)})"

    # ------------------------------------------------------------- investigación
    def research(self, query: str, fetch_pages: int | None = None, *, save: bool = True) -> ResearchBundle:
        """Busca, descarga en paralelo las páginas top y guarda una nota."""
        self.panic.check()
        pages = clamp_fetch_pages(self.fetch_pages if fetch_pages is None else int(fetch_pages))
        backend = "brave-api" if self.brave_api_key else "web"
        hits = self.search(query)
        if pages and hits:
            contents = self._fetch_contents(hits[:pages])
            hits = tuple(hit.with_content(contents.get(hit.url, "")) for hit in hits)
        bundle = ResearchBundle(query=query, hits=hits, backend=backend)
        if save:
            bundle = replace(bundle, saved_files=(self.store.save_findings(query, list(hits)),))
        return bundle

    def _fetch_contents(self, hits: Sequence[SearchHit]) -> dict[str, str]:
        urls = [hit.url for hit in hits if hit.url]
        results: dict[str, str] = {}
        if not urls:
            return results
        workers = min(self.max_parallel_fetches, len(urls))
        if workers == 1:
            return {url: self._fetch_or_error(url, self.text_limit) for url in urls}
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="autonoma-fetch") as pool:
            futures = {pool.submit(self.fetch_url, url, self.text_limit): url for url in urls}
            for future, url in futures.items():
                try:
                    results[url] = future.result()
                except PanicError:
                    for other in futures:
                        other.cancel()
                    raise
                except Exception as exc:  # noqa: BLE001
                    results[url] = f"(no se pudo extraer: {_reason(exc)})"
        return results

    def format_bundle(self, bundle: ResearchBundle, preview: int = 900) -> str:
        """Vista compacta para el modelo: consulta, backend, hits y extractos."""
        lines = [
            f"Consulta: {bundle.query}",
            f"Backend: {bundle.backend or 'auto'}",
            f"Hits: {bundle.hit_count}",
        ]
        if bundle.saved_files:
            lines.append("Guardado en: " + ", ".join(path.name for path in bundle.saved_files))
        for position, hit in enumerate(bundle.hits, 1):
            lines += ["", f"{position}. {hit.title}", f"   {hit.url}", f"   {hit.snippet[:400]}"]
            if hit.content:
                lines.append(f"   extracto: {hit.content[:preview]}")
        return "\n".join(lines)

    # ------------------------------------------------------------- notas (E/P)
    def save_note(self, title: str, body: str, source: str = "agente") -> Path:
        return self.store.save_note(title, body, source)

    def save_findings(self, query: str, hits: list[Any], notes: str = "") -> Path:
        return self.store.save_findings(query, hits, notes)

    def list_notes(self, limit: int = 40) -> list[Path]:
        return self.store.list_notes(limit)

    def read_note(self, name_or_path: str, max_chars: int = 20_000) -> str:
        return self.store.read_note(name_or_path, max_chars)

    def search_notes(self, query: str, limit: int = 6) -> str:
        return self.store.search_notes(query, limit)

    def context_digest(self, limit_files: int = 8, per_file: int = 1800) -> str:
        return self.store.context_digest(limit_files, per_file)


def clamp_fetch_pages(value: int) -> int:
    """Límite duro de páginas por investigación, independientemente del modelo."""
    return max(0, min(int(value), _MAX_FETCH_PAGES))


def _reason(exc: BaseException) -> str:
    text = str(exc) or type(exc).__name__
    return text if len(text) <= 240 else text[:240] + "…"
