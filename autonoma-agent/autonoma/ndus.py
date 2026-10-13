"""Cookie NDUS: recuperarla rápido, importarla y guardarla sin fricción.

Por qué existe este módulo: la cookie NDUS vive en el navegador del usuario. En su
propia máquina se puede leer del almacén de cookies; dentro de un entorno sin
navegador (un sandbox, una VM, CI) no hay almacén que leer, así que la única vía
es pegarla. Los tres caminos, de más rápido a más lento:

  1. Pegarla              → `coerce_ndus()` acepta el valor suelto, `ndus=...` o la
                            cabecera `Cookie:` completa copiada del inspector.
  2. Importar un archivo  → Netscape `cookies.txt` (el que exportan las extensiones)
                            o cualquier txt que contenga la cabecera o el valor.
  3. Leer el navegador    → Firefox (valor en claro) y Chromium/Chrome/Edge/Brave
                            (mejor esfuerzo: DPAPI en Windows, llavero en macOS/Linux).

Reglas duras de este módulo:

  * El valor nunca se imprime: sólo `mask()` sale al exterior.
  * Los almacenes del navegador se leen; nunca se escriben.
  * Cada fuente se prueba aislada: el fallo de una no tumba a las demás, y siempre
    se devuelve una nota accionable (`CookieHit.note`) en lugar de silencio.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import os
import platform
import re
import shutil
import sqlite3
import subprocess
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

__all__ = [
    "COOKIE_NAME",
    "COPY_HINTS",
    "BrowserProfile",
    "CookieHit",
    "NdusImport",
    "SourceProbe",
    "coerce_ndus",
    "collect_browser_hits",
    "discover_browser_profiles",
    "extract_ndus",
    "find_cookies_txt",
    "import_from_browsers",
    "import_from_cookies_txt",
    "import_from_file",
    "import_from_text",
    "import_ndus",
    "is_real_ndus",
    "mask",
    "parse_netscape_cookies",
    "probe_sources",
]

COOKIE_NAME: Final[str] = "ndus"
#: Por debajo de esto no es una cookie: es un resto de plantilla o un error al copiar.
MIN_VALUE_CHARS: Final[int] = 8
#: Tope de lectura de un archivo de cookies exportado (8 MB cubre exportaciones grandes).
MAX_FILE_CHARS: Final[int] = 8_000_000
#: Segundos de espera al preguntar al llavero del sistema; nunca bloquea el arranque.
_KEYRING_TIMEOUT: Final[float] = 4.0

#: Marcadores de plantilla: sólo subcadenas inequívocas. Un filtro más agresivo (p. ej.
#: buscar "valor" o "example") rechazaría cookies reales que los contienen por azar.
_PLACEHOLDER_MARKERS: Final[tuple[str, ...]] = ("pega-aqui", "pega_aqui", "changeme", "change-me")
#: Valores de plantilla completos que sólo se rechazan si son el valor entero.
_PLACEHOLDER_VALUES: Final[frozenset[str]] = frozenset(
    {"tu-ndus", "tu-clave", "ndus", "cookie", "tu-valor-largo-aqui"}
)
_ILLEGAL_VALUE_CHARS: Final[tuple[str, ...]] = ("\r", "\n", "\x00")

# `ndus=valor`, `ndus: valor`, `Cookie: a=b; ndus=valor; c=d`, `{"ndus":"valor"}`
_ASSIGNMENT: Final[re.Pattern[str]] = re.compile(
    r"ndus\s*[:=]\s*(?P<quote>[\"']?)(?P<value>[^;,)\s\"']+)(?P=quote)",
    re.IGNORECASE,
)
# JSON suelto del inspector: `"ndus": "valor"`
_JSON_FIELD: Final[re.Pattern[str]] = re.compile(
    r"[\"']ndus[\"']\s*:\s*[\"'](?P<value>[^\"']+)[\"']",
    re.IGNORECASE,
)
# "Copy as JSON" de DevTools: `{"name":"ndus","value":"..."}`
_NAMED_JSON: Final[re.Pattern[str]] = re.compile(
    r"[\"']name[\"']\s*:\s*[\"']ndus[\"']\s*,\s*[\"']value[\"']\s*:\s*[\"'](?P<value>[^\"']+)[\"']",
    re.IGNORECASE,
)

#: Dónde guarda Firefox su sqlite de cookies, por plataforma (las que no existan se ignoran).
_FIREFOX_ROOTS: Final[tuple[str, ...]] = (
    ".mozilla/firefox",
    "snap/firefox/common/.mozilla/firefox",
    ".var/app/org.mozilla.firefox/.mozilla/firefox",
    "Library/Application Support/Firefox",
    "AppData/Roaming/Mozilla/Firefox",
)
#: (etiqueta, raíz de datos) de la familia Chromium: Chrome, Edge, Brave, Vivaldi, Opera…
_CHROMIUM_ROOTS: Final[tuple[tuple[str, str], ...]] = (
    ("Chrome", ".config/google-chrome"),
    ("Chromium", ".config/chromium"),
    ("Edge", ".config/microsoft-edge"),
    ("Brave", ".config/BraveSoftware/Brave-Browser"),
    ("Vivaldi", ".config/vivaldi"),
    ("Opera", ".config/opera"),
    ("Chrome", "Library/Application Support/Google/Chrome"),
    ("Chromium", "Library/Application Support/Chromium"),
    ("Edge", "Library/Application Support/Microsoft Edge"),
    ("Brave", "Library/Application Support/BraveSoftware/Brave-Browser"),
    ("Chrome", "AppData/Local/Google/Chrome/User Data"),
    ("Edge", "AppData/Local/Microsoft/Edge/User Data"),
    ("Brave", "AppData/Local/BraveSoftware/Brave-Browser/User Data"),
    ("Chromium", "AppData/Local/Chromium/User Data"),
)
_COOKIES_TXT_NAMES: Final[tuple[str, ...]] = (
    "cookies.txt",
    "Descargas/cookies.txt",
    "Downloads/cookies.txt",
    "Escritorio/cookies.txt",
    "Desktop/cookies.txt",
)

#: Instrucciones de copiado: es lo que se muestra cuando no hay navegador que leer.
COPY_HINTS: Final[str] = (
    "Cómo copiarla en 10 segundos (en tu navegador, no aquí):\n"
    "  1. Entra en el sitio de la cookie con tu sesión abierta.\n"
    "  2. F12 → Application (Chrome/Edge) o Storage (Firefox) → Cookies → el dominio.\n"
    "  3. Copia el valor de la cookie `ndus` (o la cabecera `Cookie:` completa, también vale).\n"
    "  4. Pégala: /ndus <valor>   —   o: autonoma --ndus \"<valor>\"\n"
    "Sin navegador en este entorno: NDUS=<valor> autonoma ...  (o --ndus-file cookies.txt)"
)


@dataclass(frozen=True, slots=True)
class BrowserProfile:
    """Un perfil de navegador con su almacén de cookies localizable."""

    browser: str
    family: str  # "firefox" | "chromium"
    cookie_db: Path
    #: `Local State` de Chromium: de ahí sale la clave de cifrado; `None` en Firefox.
    local_state: Path | None = None


@dataclass(frozen=True, slots=True)
class CookieHit:
    """Una cookie `ndus` encontrada. `value` vacío = estaba cifrada y no se pudo abrir."""

    value: str
    browser: str
    host: str
    origin_file: Path
    encrypted: bool = False
    note: str = ""

    @property
    def usable(self) -> bool:
        return bool(self.value)


@dataclass(frozen=True, slots=True)
class NdusImport:
    """Resultado de intentar recuperar la cookie; `ok` evita comparar cadenas vacías."""

    value: str
    origin: str  # "texto" | "archivo" | "cookies.txt" | "navegador" | ""
    detail: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.value)


@dataclass(frozen=True, slots=True)
class SourceProbe:
    """Una fuente examinada: sirve para explicar por qué no se encontró nada."""

    label: str
    path: Path
    present: bool
    note: str = ""

    def describe(self) -> str:
        state = "encontrado" if self.present else "ausente"
        return f"  {self.label}: {state}" + (f" — {self.note}" if self.note else "")


# --------------------------------------------------------------------- extracción
def _clean(value: str) -> str:
    """Quita espacios y comillas envolventes sin tocar el interior del valor."""
    text = (value or "").strip().strip("\"'").strip()
    return "".join(char for char in text if char not in _ILLEGAL_VALUE_CHARS)


def _looks_like_bare_value(text: str) -> bool:
    """Un valor suelto plausible: sin espacios, largo, y que no sea una plantilla."""
    if len(text) < MIN_VALUE_CHARS:
        return False
    if any(char.isspace() for char in text):
        return False
    lowered = text.lower()
    if lowered.startswith("ndus") or "ndus=" in lowered or "ndus:" in lowered:
        return False
    if lowered in _PLACEHOLDER_VALUES:
        return False
    return not any(marker in lowered for marker in _PLACEHOLDER_MARKERS)


def extract_ndus(text: str) -> str | None:
    """Extrae el valor de la cookie `ndus` de texto pegado; `None` si no aparece.

    Acepta `ndus=valor`, `ndus: valor`, cabeceras `Cookie:` completas, JSON del
    inspector y `{"name":"ndus","value":"..."}`. Es estricto a propósito: el valor
    suelto (sin marca `ndus`) lo acepta `coerce_ndus`, no esta función, para no
    confundir una clave API pegada por descuido con la cookie.
    """
    if not text:
        return None
    for pattern in (_ASSIGNMENT, _JSON_FIELD, _NAMED_JSON):
        match = pattern.search(text)
        if match:
            value = _clean(match.group("value"))
            if value:
                return value
    return None


def coerce_ndus(text: str) -> str | None:
    """Como `extract_ndus`, y además acepta el valor suelto pegado tal cual."""
    direct = extract_ndus(text)
    if direct:
        return direct
    bare = _clean(text)
    return bare if _looks_like_bare_value(bare) else None


def is_real_ndus(value: str) -> bool:
    """¿Es un valor de cookie plausible (y no un `pega-aqui` de la plantilla)?"""
    return _looks_like_bare_value(_clean(value))


def mask(value: str) -> str:
    """Representación imprimible: extremos y longitud, nunca el valor completo."""
    text = _clean(value)
    if not text:
        return "(vacío)"
    if len(text) <= MIN_VALUE_CHARS:
        return "*" * len(text)
    return f"{text[:4]}…{text[-4:]} ({len(text)} caracteres)"


# ------------------------------------------------------------- navegador: descubrimiento
def _profile_dirs(root: Path) -> list[Path]:
    """Perfiles dentro de una raíz Chromium: la propia raíz, `Default`, `Profile 1`…"""
    if not root.is_dir():
        return []
    candidates: list[Path] = []
    with contextlib.suppress(OSError):
        if (root / "Cookies").is_file():
            candidates.append(root)
        candidates.extend(
            child
            for child in sorted(root.iterdir())
            if child.is_dir() and (child.name == "Default" or child.name.startswith(("Profile", "Person")))
        )
    return candidates


def _local_state_for(profile_dir: Path, browser_root: Path) -> Path | None:
    """`Local State` vive en la raíz del navegador, no dentro del perfil."""
    for candidate in (browser_root / "Local State", profile_dir / "Local State", profile_dir.parent / "Local State"):
        if candidate.is_file():
            return candidate
    return None


def discover_browser_profiles(
    home: Path | None = None,
    extra_roots: Sequence[Path] = (),
) -> list[BrowserProfile]:
    """Perfiles con almacén de cookies en esta máquina; lista vacía si no hay ninguno.

    Se listan las rutas de todas las plataformas: las que no existan simplemente no
    aparecen, así no hay que adivinar el sistema operativo desde el código.
    """
    base = home if home is not None else Path.home()
    profiles: list[BrowserProfile] = []
    seen: set[Path] = set()

    def add(profile: BrowserProfile) -> None:
        key = profile.cookie_db.resolve() if profile.cookie_db.exists() else profile.cookie_db
        if key not in seen:
            seen.add(key)
            profiles.append(profile)

    for relative in _FIREFOX_ROOTS:
        root = base / relative
        if not root.is_dir():
            continue
        with contextlib.suppress(OSError):
            for database in sorted(root.glob("*/cookies.sqlite")):
                add(BrowserProfile("Firefox", "firefox", database))
    for label, relative in _CHROMIUM_ROOTS:
        root = base / relative
        for directory in _profile_dirs(root):
            database = directory / "Cookies"
            if database.is_file():
                add(BrowserProfile(label, "chromium", database, _local_state_for(directory, root)))
    for extra in extra_roots:
        for directory in _profile_dirs(extra):
            database = directory / "Cookies"
            if database.is_file():
                add(BrowserProfile(extra.name or "perfil", "chromium", database, _local_state_for(directory, extra)))
        if (extra / "cookies.sqlite").is_file():
            add(BrowserProfile("Firefox", "firefox", extra / "cookies.sqlite"))
    return profiles


# ------------------------------------------------------------- navegador: lectura
@dataclass(frozen=True, slots=True)
class _CookieHandle:
    """Conexión de sólo lectura y, si se copió, la copia temporal que hay que borrar."""

    connection: sqlite3.Connection
    temporary: Path | None = None

    def close(self) -> None:
        """Cierra la conexión y borra la copia: ninguna huella en el disco del usuario."""
        with contextlib.suppress(sqlite3.Error):
            self.connection.close()
        if self.temporary is not None:
            with contextlib.suppress(OSError):
                self.temporary.unlink(missing_ok=True)


def _open_cookie_db(path: Path) -> _CookieHandle | None:
    """Abre la base en sólo lectura. Se copia antes: el navegador puede tenerla bloqueada.

    Copiar también evita que una consulta choque con el navegador escribiendo a la vez;
    si la copia falla, se intenta abrir el original de forma inmutable.
    """
    temporary: str | None = None
    try:
        descriptor, temporary = tempfile.mkstemp(prefix="autonoma-cookies-", suffix=".sqlite")
        os.close(descriptor)
        shutil.copyfile(path, Path(temporary))
        return _CookieHandle(sqlite3.connect(f"file:{temporary}?mode=ro", uri=True), Path(temporary))
    except (OSError, sqlite3.Error):
        if temporary:
            with contextlib.suppress(OSError):
                Path(temporary).unlink(missing_ok=True)
        try:
            return _CookieHandle(sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True))
        except sqlite3.Error:
            return None


def _read_rows(profile: BrowserProfile) -> list[tuple[str, str, bytes]]:
    """Filas `(valor_en_claro, dominio, valor_cifrado)` de la cookie `ndus` de un perfil."""
    handle = _open_cookie_db(profile.cookie_db)
    if handle is None:
        return []
    try:
        if profile.family == "firefox":
            rows = handle.connection.execute(
                "SELECT value, host FROM moz_cookies WHERE lower(name) = ?", (COOKIE_NAME,)
            ).fetchall()
            return [(str(row[0] or ""), str(row[1] or ""), b"") for row in rows]
        rows = handle.connection.execute(
            "SELECT value, host_key, encrypted_value FROM cookies WHERE lower(name) = ?", (COOKIE_NAME,)
        ).fetchall()
        return [(str(row[0] or ""), str(row[1] or ""), bytes(row[2] or b"")) for row in rows]
    except sqlite3.Error:
        return []
    finally:
        handle.close()


def _dpapi_unprotect(blob: bytes) -> bytes | None:
    """Descifra con DPAPI (Windows) usando sólo ctypes: sin dependencias extra."""
    if os.name != "nt":
        return None
    import ctypes

    class DATA_BLOB(ctypes.Structure):
        """`DATA_BLOB` mínimo: `wintypes.DWORD` es `c_uint32`, así que no hace falta importarlo."""

        _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_char))]

    # `windll` sólo existe en Windows: se accede por `getattr` para que el módulo siga
    # siendo válido (y analizable) en Linux y macOS.
    windll = getattr(ctypes, "windll", None)
    if windll is None:
        return None
    source = DATA_BLOB()
    source.cbData = len(blob)
    source.pbData = ctypes.cast(ctypes.create_string_buffer(blob, len(blob)), ctypes.POINTER(ctypes.c_char))
    target = DATA_BLOB()
    try:
        ok = windll.crypt32.CryptUnprotectData(ctypes.byref(source), None, None, None, None, 0, ctypes.byref(target))
        if not ok:
            return None
        return bytes(ctypes.string_at(target.pbData, target.cbData))
    except (OSError, AttributeError, ValueError):
        return None


def _chromium_master_key(local_state: Path | None, browser: str) -> tuple[bytes | None, str]:
    """Clave maestra de Chromium. Devuelve `(clave, nota)`; la nota explica el fallo.

    Windows: la clave va protegida con DPAPI dentro de `Local State`.
    macOS: `security find-generic-password` + PBKDF2 ("saltysalt", 1003).
    Linux: `secret-tool` y, si no contesta, la clave por defecto `"peanuts"`.
    """
    if os.name == "nt":
        if local_state is None:
            return None, "sin `Local State`: no se puede sacar la clave de cifrado"
        try:
            payload = local_state.read_text(encoding="utf-8", errors="replace")
            raw_key = _json_extract_key(payload)
        except OSError:
            return None, "no se pudo leer `Local State`"
        if not raw_key:
            return None, "`Local State` sin `os_crypt.encrypted_key`"
        encrypted = base64.b64decode(raw_key)
        if encrypted.startswith(b"DPAPI"):
            encrypted = encrypted[len(b"DPAPI") :]
        decrypted = _dpapi_unprotect(encrypted)
        if decrypted is None:
            return None, "DPAPI no devolvió la clave (¿otro usuario de Windows?)"
        return decrypted, ""
    password: str | None = None
    if platform.system() == "Darwin":
        password = _run_capture(["security", "find-generic-password", "-w", "-s", f"{browser} Safe Storage"])
    else:
        # Cada gestor de llavero guarda la entrada con atributos distintos: se prueban
        # los habituales y, si ninguno contesta, la clave por defecto de Chromium.
        for attribute, value in (
            ("service", f"{browser} Safe Storage"),
            ("application", browser.lower()),
            ("service", "chromium"),
        ):
            found = _run_capture(["secret-tool", "lookup", attribute, value])
            if found:
                password = found
                break
        if password is None:
            password = "peanuts"  # noqa: S105 — clave por defecto documentada de Chromium en Linux
    if not password:
        return None, "el llavero del sistema no devolvió la contraseña del navegador"
    return hashlib.pbkdf2_hmac("sha1", password.encode("utf-8"), b"saltysalt", 1003, 16), ""


def _run_capture(args: Sequence[str]) -> str | None:
    try:
        completed = subprocess.run(  # noqa: S603
            list(args),
            capture_output=True,
            text=True,
            timeout=_KEYRING_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    value = completed.stdout.strip()
    return value or None


def _json_extract_key(payload: str) -> str | None:
    """`os_crypt.encrypted_key` sin depender de la forma exacta del JSON."""
    match = re.search(r"\"encrypted_key\"\s*:\s*\"([^\"]+)\"", payload)
    return match.group(1) if match else None


def _aes_decrypt(blob: bytes, key: bytes) -> str | None:
    """Prueba AES-GCM (Windows/macOS) y AES-CBC (Linux/antiguo); `None` si no cuadra.

    `cryptography` es opcional: sin él no se descifra, pero el fallo se reporta como
    tal en vez de fingir que la cookie no existe.
    """
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError:
        return None
    body = blob[3:]
    gcm: bytes | None = None
    try:
        gcm = AESGCM(key).decrypt(body[:12], body[12:], None)
    except Exception:  # noqa: BLE001 — GCM es un intento: si no cuadra se prueba CBC
        gcm = None
    if gcm is not None:
        return gcm.decode("utf-8")
    try:
        decryptor = Cipher(algorithms.AES(key), modes.CBC(b" " * 16)).decryptor()
        plain: bytes = decryptor.update(body) + decryptor.finalize()
    except Exception:  # noqa: BLE001 — se reporta como "cifrada y sin descifrar"
        return None
    return plain.rstrip(b"\x00").rstrip(bytes(range(1, 17))).decode("utf-8")


def _decrypt_chromium(blob: bytes, profile: BrowserProfile) -> tuple[str, str]:
    """Devuelve `(valor, nota)`: el valor puede llegar vacío con la nota del motivo."""
    if not blob:
        return "", ""
    if not blob.startswith((b"v10", b"v11", b"v20")):
        # Chromium antiguo en Windows: DPAPI directo, sin AES por encima.
        plain = _dpapi_unprotect(blob)
        return (plain.decode("utf-8", errors="replace"), "") if plain else ("", "cifrada con DPAPI y no se pudo abrir")
    key, note = _chromium_master_key(profile.local_state, profile.browser)
    if key is None:
        return "", note or "clave de cifrado no disponible"
    value = _aes_decrypt(blob, key)
    if value is None:
        return "", (
            "encontrada pero cifrada: instala `cryptography` (pip install cryptography) "
            "o cópiala a mano desde el inspector del navegador"
        )
    return value, ""


def read_profile_cookies(profile: BrowserProfile) -> list[CookieHit]:
    """Cookies `ndus` de un perfil; una entrada por fila hallada, con su nota."""
    hits: list[CookieHit] = []
    for value, host, encrypted in _read_rows(profile):
        if value:
            hits.append(CookieHit(_clean(value), profile.browser, host, profile.cookie_db))
            continue
        if not encrypted:
            continue
        decrypted, note = _decrypt_chromium(encrypted, profile)
        hits.append(
            CookieHit(
                _clean(decrypted),
                profile.browser,
                host,
                profile.cookie_db,
                encrypted=not decrypted,
                note=note,
            )
        )
    return hits


def collect_browser_hits(
    home: Path | None = None,
    extra_roots: Sequence[Path] = (),
) -> tuple[list[CookieHit], list[str]]:
    """Busca la cookie en todos los navegadores; devuelve (hallazgos, avisos)."""
    hits: list[CookieHit] = []
    warnings: list[str] = []
    for profile in discover_browser_profiles(home=home, extra_roots=extra_roots):
        try:
            found = read_profile_cookies(profile)
        except (OSError, sqlite3.Error) as exc:
            warnings.append(f"{profile.browser}: no se pudo leer su almacén ({type(exc).__name__})")
            continue
        if not found:
            continue
        hits.extend(found)
    return hits, warnings


# --------------------------------------------------------------- cookies.txt (Netscape)
def find_cookies_txt(home: Path | None = None, cwd: Path | None = None) -> list[Path]:
    """`cookies.txt` exportado por una extensión: rutas habituales, sin recorrer el disco."""
    base = home if home is not None else Path.home()
    candidates: list[Path] = []
    forced = os.environ.get("AUTONOMA_COOKIES_FILE", "").strip()
    if forced:
        candidates.append(Path(forced).expanduser())
    with contextlib.suppress(OSError):
        candidates.append((cwd or Path.cwd()) / "cookies.txt")
    candidates.extend(base / name for name in _COOKIES_TXT_NAMES)
    with contextlib.suppress(OSError):
        downloads = base / "Downloads"
        if downloads.is_dir():
            candidates.extend(sorted(downloads.glob("*cookie*.txt"))[:5])
    unique: dict[Path, None] = {}
    for candidate in candidates:
        if candidate.is_file():
            unique.setdefault(candidate, None)
    return list(unique)


def parse_netscape_cookies(text: str) -> list[tuple[str, str]]:
    """Pares `(nombre, valor)` de un `cookies.txt` Netscape; tolera espacios o tabuladores."""
    pairs: list[tuple[str, str]] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t") if "\t" in line else line.split()
        if len(parts) < 7:
            continue
        pairs.append((parts[-2], parts[-1]))
    return pairs


def import_from_cookies_txt(path: Path) -> NdusImport:
    """Lee la cookie de un `cookies.txt` (o cualquier txt con la cabecera/valor)."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")[:MAX_FILE_CHARS]
    except OSError as exc:
        return NdusImport("", "", f"no se pudo leer {path.name}: {type(exc).__name__}")
    direct = extract_ndus(text)
    if direct:
        return NdusImport(direct, "cookies.txt", f"{path.name}")
    for name, value in parse_netscape_cookies(text):
        if name.strip().lower() == COOKIE_NAME and is_real_ndus(value):
            return NdusImport(_clean(value), "cookies.txt", f"{path.name}")
    return NdusImport("", "", f"{path.name} sin cookie `ndus`")


# ------------------------------------------------------------------------ importación
def import_from_text(text: str) -> NdusImport:
    """Importa desde texto pegado: valor suelto, `ndus=...` o cabecera completa."""
    value = coerce_ndus(text)
    if not value:
        return NdusImport("", "", "el texto no contiene una cookie `ndus` reconocible")
    if not is_real_ndus(value):
        return NdusImport("", "", "el valor parece una plantilla o está incompleto")
    return NdusImport(value, "texto")


def import_from_file(path: Path) -> NdusImport:
    """Importa desde un archivo: `cookies.txt`, cabecera `Cookie:` o valor suelto."""
    result = import_from_cookies_txt(path)
    if result.ok:
        return result
    try:
        text = path.read_text(encoding="utf-8", errors="replace")[:MAX_FILE_CHARS]
    except OSError as exc:
        return NdusImport("", "", f"no se pudo leer {path.name}: {type(exc).__name__}")
    value = coerce_ndus(text)
    if value and is_real_ndus(value):
        return NdusImport(value, "archivo", path.name)
    return NdusImport("", "", result.detail)


def import_from_browsers(
    home: Path | None = None,
    extra_roots: Sequence[Path] = (),
) -> NdusImport:
    """Busca la cookie en los navegadores instalados; usa el primer valor legible."""
    hits, warnings = collect_browser_hits(home=home, extra_roots=extra_roots)
    for hit in hits:
        if hit.usable:
            return NdusImport(hit.value, "navegador", f"{hit.browser} · {hit.host or 'sin dominio'}")
    encrypted = [hit for hit in hits if hit.encrypted]
    if encrypted:
        note = encrypted[0].note or "cifrada por el navegador"
        return NdusImport("", "", f"{encrypted[0].browser}: {note}")
    if warnings:
        return NdusImport("", "", "; ".join(warnings))
    return NdusImport("", "", "esta máquina no tiene perfiles de navegador con cookies legibles")


def import_ndus(
    *,
    text: str | None = None,
    file: Path | None = None,
    auto: bool = False,
    home: Path | None = None,
    extra_roots: Sequence[Path] = (),
) -> NdusImport:
    """Recupera la cookie por el camino más rápido disponible, en este orden.

    Texto > archivo > navegadores de la máquina. El orden es deliberado: lo que el
    usuario acaba de pegar gana siempre a lo que se pueda adivinar del disco.
    """
    if text:
        return import_from_text(text)
    if file is not None:
        return import_from_file(file)
    if auto:
        from_browser = import_from_browsers(home=home, extra_roots=extra_roots)
        if from_browser.ok:
            return from_browser
        for candidate in find_cookies_txt(home=home):
            from_file = import_from_cookies_txt(candidate)
            if from_file.ok:
                return from_file
        return from_browser
    return NdusImport("", "", "no se indicó ninguna fuente")


def probe_sources(
    home: Path | None = None,
    extra_roots: Sequence[Path] = (),
) -> list[SourceProbe]:
    """Qué se miró y qué se encontró: la explicación cuando `--ndus-auto` no halla nada."""
    probes: list[SourceProbe] = []
    for profile in discover_browser_profiles(home=home, extra_roots=extra_roots):
        note = ""
        try:
            hits = read_profile_cookies(profile)
        except (OSError, sqlite3.Error) as exc:
            note = f"ilegible ({type(exc).__name__})"
        else:
            if not hits:
                note = "sin cookie `ndus`"
            else:
                readable = sum(1 for hit in hits if hit.usable)
                note = f"{readable} legible(s)" if readable else (hits[0].note or "cifrada")
        label = f"{profile.browser} ({profile.cookie_db.parent.name})"
        probes.append(SourceProbe(label, profile.cookie_db, present=True, note=note))
    for path in find_cookies_txt(home=home):
        result = import_from_cookies_txt(path)
        probes.append(
            SourceProbe("cookies.txt", path, present=True, note="con `ndus`" if result.ok else "sin cookie `ndus`")
        )
    return probes
