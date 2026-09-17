"""Búsqueda web (Brave API → Playwright/Brave Browser → HTML) y knowledge_base."""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass, field
from html import unescape
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus, urlparse
from autonoma.network import validate_public_url
from autonoma.knowledge import KnowledgeStore

import httpx
from bs4 import BeautifulSoup

from autonoma.key_handler import PanicController, PanicError

logger = logging.getLogger(__name__)

BRAVE_SEARCH_URL = "https://api.search.brave.com/res/v1/web/search"
BRAVE_WEB_URL = "https://search.brave.com/search"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)


@dataclass
class SearchHit:
    title: str
    url: str
    snippet: str
    extra: str = ""
    content: str = ""


@dataclass
class ResearchBundle:
    query: str
    hits: list[SearchHit] = field(default_factory=list)
    saved_files: list[Path] = field(default_factory=list)
    backend: str = ""
    notes: str = ""


def _visible_text(html: str, limit: int = 12_000) -> str:
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript", "svg", "iframe", "header", "footer", "nav", "form"]):
        tag.decompose()
    text = soup.get_text("\n")
    lines = [unescape(ln).strip() for ln in text.splitlines()]
    compact = re.sub(r"\n{3,}", "\n\n", "\n".join(ln for ln in lines if ln))
    return compact[:limit]


def _brave_executable() -> str | None:
    candidates = [
        r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
        r"C:\Program Files (x86)\BraveSoftware\Brave-Browser\Application\brave.exe",
        "/usr/bin/brave-browser",
        "/usr/bin/brave",
        "/usr/bin/brave-browser-stable",
        "/snap/bin/brave",
        "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    ]
    for raw in candidates:
        path = Path(raw)
        if path.is_file():
            return str(path)
    return None


class SearchEngine(KnowledgeStore):
    """Investiga en la web y persiste hallazgos en ./knowledge_base/."""

    def __init__(
        self,
        panic: PanicController,
        knowledge_dir: Path,
        brave_api_key: str = "",
        timeout: float = 30.0,
        default_count: int = 5,
        fetch_pages: int = 3,
    ) -> None:
        self.panic = panic
        self.knowledge_dir = Path(knowledge_dir)
        self.knowledge_dir.mkdir(parents=True, exist_ok=True)
        self.brave_api_key = (brave_api_key or "").strip()
        self.timeout = timeout
        self.default_count = default_count
        self.fetch_pages = fetch_pages
        self._http: httpx.Client | None = None
        self._playwright_lock = threading.Lock()
        self._playwright_runtime: Any = None
        self._browser: Any = None
        # Solo se cancela HTTP desde el hilo de pánico. Playwright no es
        # thread-safe: el hilo de trabajo lo cierra en sus bloques finally.
        panic.register_cleanup(self.close)

    def _http_client(self) -> httpx.Client:
        if self._http is None or self._http.is_closed:
            self._http = httpx.Client(
                timeout=httpx.Timeout(self.timeout, connect=10.0),
                headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/json"},
                follow_redirects=True,
            )
        return self._http

    def close(self) -> None:
        """Cierra el cliente HTTP (seguro desde el hilo de pánico)."""
        if self._http is not None and not self._http.is_closed:
            try:
                self._http.close()
            except Exception as exc:  # noqa: BLE001
                logger.debug("cierre http search: %s", exc)
        self._http = None

    def shutdown(self) -> None:
        """Cierre completo al salir del proceso."""
        self.close()
        self._close_browser()

    def _close_browser(self) -> None:
        with self._playwright_lock:
            browser = self._browser
            runtime = self._playwright_runtime
            self._browser = None
            self._playwright_runtime = None
        for obj, method in ((browser, "close"), (runtime, "stop")):
            if obj is None:
                continue
            try:
                getattr(obj, method)()
            except Exception as exc:  # noqa: BLE001
                logger.debug("cierre playwright: %s", exc)

    # ---------------------------------------------------------------- search
    def search(self, query: str, count: int | None = None) -> list[SearchHit]:
        """Prioridad: Brave API → Playwright/Brave → scrape HTML."""
        self.panic.check()
        n = count or self.default_count
        errors: list[str] = []

        if self.brave_api_key:
            try:
                hits = self._search_brave_api(query, n)
                if hits:
                    return hits
                errors.append("Brave API no devolvió resultados")
            except PanicError:
                raise
            except Exception as exc:  # noqa: BLE001
                errors.append(f"Brave API: {exc}")
                logger.warning("Brave API falló: %s", exc)

        try:
            hits = self._search_playwright(query, n)
            if hits:
                return hits
            errors.append("Playwright no devolvió resultados")
        except PanicError:
            raise
        except Exception as exc:  # noqa: BLE001
            errors.append(f"Playwright: {exc}")
            logger.info("Playwright no disponible o falló: %s", exc)

        try:
            hits = self._search_brave_html(query, n)
            if hits:
                return hits
            errors.append("HTML de Brave no devolvió resultados")
        except PanicError:
            raise
        except Exception as exc:  # noqa: BLE001
            errors.append(f"HTML Brave: {exc}")
            logger.warning("Scrape HTML falló: %s", exc)

        raise RuntimeError("Búsqueda fallida: " + " | ".join(errors))

    def _search_brave_api(self, query: str, count: int) -> list[SearchHit]:
        self.panic.check()
        client = self._http_client()
        response = client.get(
            BRAVE_SEARCH_URL,
            params={"q": query, "count": min(count, 20), "extra_snippets": True},
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "gzip",
                "X-Subscription-Token": self.brave_api_key,
            },
        )
        self.panic.check()
        if response.status_code >= 400:
            raise RuntimeError(f"HTTP {response.status_code}: {response.text[:240]}")
        data = response.json()
        web = (data.get("web") or {}).get("results") or []
        hits: list[SearchHit] = []
        for item in web[:count]:
            extras = item.get("extra_snippets") or []
            extra_txt = "\n".join(str(x) for x in extras[:3])
            hits.append(
                SearchHit(
                    title=item.get("title") or "",
                    url=item.get("url") or "",
                    snippet=item.get("description") or "",
                    extra=extra_txt,
                )
            )
        logger.info("Brave API: %s hits para %r", len(hits), query)
        return hits

    def _search_brave_html(self, query: str, count: int) -> list[SearchHit]:
        self.panic.check()
        client = self._http_client()
        url = f"{BRAVE_WEB_URL}?q={quote_plus(query)}"
        response = client.get(url, headers={"Accept-Language": "es,en;q=0.8"})
        self.panic.check()
        response.raise_for_status()
        return self._parse_brave_html(response.text, count)

    def _parse_brave_html(self, html: str, count: int) -> list[SearchHit]:
        soup = BeautifulSoup(html, "lxml")
        hits: list[SearchHit] = []
        seen: set[str] = set()

        selectors = [
            "div.snippet",
            "div[data-type='web']",
            "div.fdb",
            "div#results div",
        ]
        nodes: list[Any] = []
        for sel in selectors:
            found = soup.select(sel)
            if found:
                nodes = found
                break
        if not nodes:
            nodes = soup.select("a[href]")

        for node in nodes:
            if len(hits) >= count:
                break
            link = node if getattr(node, "name", "") == "a" else node.find("a", href=True)
            if not link or not link.get("href"):
                continue
            href = link["href"].strip()
            if not href.startswith("http"):
                continue
            host = urlparse(href).netloc.lower()
            if "brave.com" in host or "brave.search" in host:
                continue
            if href in seen:
                continue
            seen.add(href)
            title = link.get_text(" ", strip=True) or href
            snippet_el = None
            if hasattr(node, "select_one"):
                snippet_el = (
                    node.select_one(".snippet-description")
                    or node.select_one(".snippet-content")
                    or node.select_one("p")
                )
            snippet = snippet_el.get_text(" ", strip=True) if snippet_el else ""
            if len(title) < 3:
                continue
            hits.append(SearchHit(title=title[:300], url=href, snippet=snippet[:800]))
        return hits[:count]

    def _search_playwright(self, query: str, count: int) -> list[SearchHit]:
        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"playwright no instalado ({exc})") from exc

        self.panic.check()
        html = ""
        brave = _brave_executable()
        with self._playwright_lock:
            pw = sync_playwright().start()
            self._playwright_runtime = pw
            launch_kwargs: dict[str, Any] = {"headless": True}
            if brave:
                launch_kwargs["executable_path"] = brave
            try:
                browser = pw.chromium.launch(**launch_kwargs)
            except Exception:
                browser = pw.chromium.launch(headless=True)
            self._browser = browser
        try:
            page = browser.new_page(user_agent=USER_AGENT)
            page.set_default_timeout(int(self.timeout * 1000))
            self.panic.check()
            page.goto(f"{BRAVE_WEB_URL}?q={quote_plus(query)}", wait_until="domcontentloaded")
            try:
                page.wait_for_selector("div.snippet, a.result-header, #results", timeout=8000)
            except Exception:  # noqa: BLE001
                page.wait_for_timeout(1500)
            self.panic.check()
            html = page.content()
        finally:
            self._close_browser()
        hits = self._parse_brave_html(html, count)
        logger.info("Playwright/Brave: %s hits para %r", len(hits), query)
        return hits

    # --------------------------------------------------------------- fetch
    def fetch_url(self, url: str, limit: int = 12_000) -> str:
        self.panic.check()
        validate_public_url(url)
        client = self._http_client()
        # No seguir redirecciones: evita saltos hacia servicios de la red local.
        with client.stream("GET", url, follow_redirects=False,
                           headers={"Accept": "text/html,text/plain"}) as response:
            response.raise_for_status()
            ctype = response.headers.get("content-type", "").lower()
            if not any(kind in ctype for kind in ("html", "xml", "text")):
                return f"[contenido no textual: {ctype}]"
            body = bytearray()
            for chunk in response.iter_bytes(chunk_size=8192):
                self.panic.check()
                body.extend(chunk)
                if len(body) > 2_000_000:
                    raise ValueError("Página demasiado grande (máximo 2 MB)")
            return _visible_text(body.decode(response.encoding or "utf-8", errors="replace"), limit=limit)

    def research(self, query: str, fetch_pages: int | None = None, save: bool = True) -> ResearchBundle:
        """Busca, descarga las páginas top y opcionalmente guarda un .md."""
        self.panic.check()
        n_fetch = fetch_pages if fetch_pages is not None else self.fetch_pages
        n_fetch = max(0, min(int(n_fetch), 5))
        backend = "brave-api" if self.brave_api_key else "web"
        hits = self.search(query)
        for hit in hits[:n_fetch]:
            self.panic.check()
            try:
                hit.content = self.fetch_url(hit.url)
            except PanicError:
                raise
            except Exception as exc:  # noqa: BLE001
                hit.content = f"(no se pudo extraer: {exc})"
                logger.info("fetch %s: %s", hit.url, exc)

        bundle = ResearchBundle(query=query, hits=hits, backend=backend)
        if save:
            path = self.save_findings(query, hits)
            bundle.saved_files.append(path)
        return bundle

    def format_bundle(self, bundle: ResearchBundle, preview: int = 900) -> str:
        lines = [
            f"Consulta: {bundle.query}",
            f"Backend: {bundle.backend or 'auto'}",
            f"Hits: {len(bundle.hits)}",
        ]
        if bundle.saved_files:
            lines.append("Guardado en: " + ", ".join(p.name for p in bundle.saved_files))
        for i, hit in enumerate(bundle.hits, 1):
            lines += [
                "",
                f"{i}. {hit.title}",
                f"   {hit.url}",
                f"   {hit.snippet[:400]}",
            ]
            if hit.content:
                lines.append(f"   extracto: {hit.content[:preview]}")
        return "\n".join(lines)
