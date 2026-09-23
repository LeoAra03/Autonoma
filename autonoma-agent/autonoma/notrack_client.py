"""Cliente HTTP para la API OpenAI-compatible de NoTrack.ai.

Refuerzo respecto a la versión anterior:
- Errores tipados (`ProviderHttpError`, `ProviderUnavailableError`,
  `ProviderContractError`) en lugar de una sola clase con el detalle en el mensaje;
  `NoTrackError` se conserva como alias de la base para llamadores existentes.
- Reintentos con retroceso exponencial y respeto de `Retry-After`, sólo para fallos
  transitorios (429/5xx/red), cancelables por `PanicController`.
- El cuerpo remoto nunca se interpola en el error: se pierde el detalle a cambio de
  no filtrar secretos ni contenido del usuario (contrato probado en `tests`).
"""

from __future__ import annotations

import ipaddress
import json
import logging
import math
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Final, TypeVar
from urllib.parse import urlsplit

import httpx

from autonoma.config import DEFAULT_NOTRACK_BASE_URL, DEFAULT_NOTRACK_MODEL
from autonoma.errors import (
    ConfigurationError,
    ProviderContractError,
    ProviderError,
    ProviderHttpError,
    ProviderUnavailableError,
)
from autonoma.key_handler import PanicController

logger = logging.getLogger(__name__)

T = TypeVar("T")

__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_MODEL",
    "NoTrackClient",
    "NoTrackError",
    "Usage",
]

DEFAULT_BASE_URL: Final[str] = DEFAULT_NOTRACK_BASE_URL
_LOOPBACK_HOSTS: Final[frozenset[str]] = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0", "::"})
DEFAULT_MODEL: Final[str] = DEFAULT_NOTRACK_MODEL
NoTrackError = ProviderError

_MAX_RESPONSE_BYTES: Final[int] = 2_000_000
_MAX_STREAM_CHARS: Final[int] = 100_000
_MAX_SSE_EVENT_CHARS: Final[int] = 100_000
_MAX_TOOL_CALLS: Final[int] = 16
_RETRYABLE_STATUS: Final[frozenset[int]] = ProviderHttpError.RETRYABLE_STATUS
_RETRY_AFTER_CAP_SECONDS: Final[float] = 10.0
_HINTS: Final[Mapping[int, str]] = MappingProxyType(
    {
        401: "Clave inválida o expirada; actualízala con /key.",
        403: "Tu cuenta no tiene permiso para esta operación.",
        404: "El endpoint o modelo no existe; revisa NOTRACK_MODEL.",
        429: "Límite de uso alcanzado; espera antes de volver a intentar.",
    }
)


@dataclass(frozen=True, slots=True)
class Usage:
    """Tokens informados por el proveedor; `None` cuando el proveedor no los envía."""

    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any] | None) -> Usage:
        if not isinstance(payload, dict):
            return cls()
        return cls(
            prompt_tokens=_int_or_none(payload.get("prompt_tokens")),
            completion_tokens=_int_or_none(payload.get("completion_tokens")),
            total_tokens=_int_or_none(payload.get("total_tokens")),
        )

    def as_dict(self) -> dict[str, int]:
        return {
            key: value
            for key, value in {
                "prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens,
                "total_tokens": self.total_tokens,
            }.items()
            if value is not None
        }


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


class NoTrackClient:
    """Envía consultas a `{base_url}/chat/completions` con contrato validado."""

    def __init__(
        self,
        api_key: str,
        panic: PanicController,
        base_url: str = DEFAULT_BASE_URL,
        model: str = DEFAULT_MODEL,
        timeout: float = 120.0,
        persona: str = "notrack",
        *,
        max_retries: int = 2,
        retry_backoff: float = 0.5,
    ) -> None:
        self.api_key = (api_key or "").strip()
        self.panic = panic
        self.base_url = validate_base_url(base_url)
        self.model = model or DEFAULT_MODEL
        self.timeout = timeout
        self.persona = persona
        self.max_retries = max(0, int(max_retries))
        self.retry_backoff = max(0.0, float(retry_backoff))
        self._client: httpx.Client | None = None
        panic.register_cleanup(self.close)

    # ------------------------------------------------------------------ recursos
    @property
    def local_endpoint(self) -> bool:
        """El endpoint es esta máquina: no hace falta TLS ni clave para hablar con él."""
        return is_loopback_host(urlsplit(self.base_url).hostname or "")

    @property
    def configured(self) -> bool:
        return bool(self.api_key) or self.local_endpoint

    def ensure_client(self) -> httpx.Client:
        if self._client is None or self._client.is_closed:
            self._client = httpx.Client(
                base_url=self.base_url,
                timeout=httpx.Timeout(self.timeout, connect=15.0),
                headers=_build_headers(self.api_key, self._app_version()),
                follow_redirects=False,
                trust_env=False,
            )
        return self._client

    @staticmethod
    def _app_version() -> str:
        try:
            from autonoma import __version__

            return str(__version__)
        except ImportError:  # pragma: no cover - sólo en empaquetados parciales
            return "0"

    # PyPI/tests antiguos usan el nombre privado; se conserva como alias.
    _ensure_client = ensure_client

    def close(self) -> None:
        """Cierra el cliente y desregistra su propia limpieza (dobles cierres: seguros)."""
        self.panic.unregister_cleanup(self.close)
        client, self._client = self._client, None
        if client is not None and not client.is_closed:
            try:
                client.close()
            except httpx.HTTPError as exc:
                logger.debug(
                    "cierre de cliente",
                    extra={"event": "provider.close_error", "fields": {"error": type(exc).__name__}},
                )

    # -------------------------------------------------------------------- red
    def _raise_for_status(self, response: httpx.Response) -> None:
        if 200 <= response.status_code < 300:
            return
        hint = _HINTS.get(response.status_code, "Respuesta HTTP inesperada; revisa el estado del proveedor.")
        raise ProviderHttpError(
            f"NoTrack HTTP {response.status_code}: {hint}",
            status_code=response.status_code,
        )

    def _read_response(self, response: httpx.Response) -> bytes:
        data = bytearray()
        for chunk in response.iter_bytes(chunk_size=8192):
            self.panic.check()
            data.extend(chunk)
            if len(data) > _MAX_RESPONSE_BYTES:
                raise ProviderContractError("Respuesta NoTrack demasiado grande")
        return bytes(data)

    def _delay_before_retry(self, response: httpx.Response | None, attempt: int) -> float:
        """Respeta `Retry-After` del proveedor, con tope, y si no hay cabecera retrocede."""
        backoff = float(min(self.retry_backoff * (2.0**attempt), _RETRY_AFTER_CAP_SECONDS))
        if response is None:
            return backoff
        raw = response.headers.get("retry-after", "").strip()
        if not raw:
            return backoff
        try:
            return float(max(0.0, min(float(raw), _RETRY_AFTER_CAP_SECONDS)))
        except ValueError:
            return backoff

    def _request_with_retry(
        self,
        client: httpx.Client,
        body: dict[str, Any],
        *,
        consume: Callable[[httpx.Response], T],
        failure_message: str,
        allow_retry: Callable[[], bool],
    ) -> T:
        """Un único punto de reintento: sólo lo transitorio se repite; nada se traga.

        `allow_retry` corta el reintento cuando ya se entregó algo al usuario, para
        no duplicar texto en streaming.
        """
        attempts = self.max_retries + 1
        last_error: BaseException | None = None
        for attempt in range(attempts):
            self.panic.check()
            response: httpx.Response | None = None
            try:
                with client.stream("POST", "chat/completions", json=body) as response:
                    self._raise_for_status(response)
                    return consume(response)
            except httpx.HTTPError as exc:
                self.panic.check()
                # La causa cruda se encadena (traceback en el log) pero el mensaje
                # que ve el usuario/esquema del modelo sigue siendo el genérico.
                last_error = ProviderUnavailableError(
                    failure_message,
                    context={"cause": type(exc).__name__, "attempt": attempt + 1},
                )
                last_error.__cause__ = exc
                logger.warning(
                    "fallo de transporte con NoTrack",
                    extra={
                        "event": "provider.transport_error",
                        "fields": {"attempt": attempt + 1, "error": type(exc).__name__},
                    },
                )
            except ProviderHttpError as exc:
                last_error = exc
                if exc.status_code not in _RETRYABLE_STATUS:
                    raise
            if attempt >= attempts - 1 or not allow_retry():
                break
            self.panic.wait(self._delay_before_retry(response, attempt))
        if last_error is None:  # defensivo: el bucle siempre asigna antes de salir
            raise ProviderUnavailableError(failure_message)
        raise last_error

    # ------------------------------------------------------------------- API
    def chat(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        tools: Sequence[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        temperature: float = 0.4,
        max_tokens: int = 4096,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Completión no streaming, con contrato validado y cancelación cooperativa."""
        self._require_key()
        self.panic.check()
        body = self._payload(
            list(messages),
            tools=list(tools) if tools else None,
            tool_choice=tool_choice,
            temperature=temperature,
            max_tokens=max_tokens,
            stream=False,
            extra=extra,
        )

        def consume(response: httpx.Response) -> dict[str, Any]:
            raw = self._read_response(response)
            return self._decode(raw)

        data = self._request_with_retry(
            self.ensure_client(),
            body,
            consume=consume,
            failure_message="No se pudo conectar con NoTrack; revisa tu conexión y vuelve a intentar.",
            allow_retry=_always,
        )
        self.panic.check()
        self.extract_message(data)  # valida el contrato antes de tocar cualquier herramienta
        return data

    def chat_stream(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        temperature: float = 0.4,
        max_tokens: int = 4096,
        on_delta: Callable[[str], None] | None = None,
    ) -> str:
        """Streaming SSE de texto; exige evento de cierre y rechaza tool_calls."""
        self._require_key()
        self.panic.check()
        body = self._payload(list(messages), temperature=temperature, max_tokens=max_tokens, stream=True)
        pieces: list[str] = []
        state = _StreamState()

        def consume(response: httpx.Response) -> str:
            state.reset()
            pieces.clear()
            for line in self._iter_sse_lines(response, state):
                self.panic.check()
                if line == "[DONE]":
                    state.completed = True
                    break
                self._apply_sse_event(line, pieces, state, on_delta)
            return "".join(pieces)

        text = self._request_with_retry(
            self.ensure_client(),
            body,
            consume=consume,
            failure_message="Conexión streaming interrumpida; vuelve a intentar.",
            allow_retry=state.untouched,
        )
        if not state.completed:
            raise ProviderContractError("Streaming incompleto: falta el evento de cierre")
        return text

    def _require_key(self) -> None:
        if self.configured:
            return
        raise ProviderUnavailableError(
            "Falta NOTRACK_API_KEY. Créalas en https://notrack.ai/api-keys y pégala con /key "
            "o en el archivo .env; o apunta NOTRACK_BASE_URL a un servidor local (http://localhost:…)"
        )

    def _payload(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        temperature: float = 0.4,
        max_tokens: int = 4096,
        stream: bool = False,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": _bounded_temperature(temperature),
            "max_tokens": _positive_int(max_tokens, 1, 32_768),
            "stream": stream,
            "notrack": {"persona": self.persona},
        }
        if tools:
            body["tools"] = tools
            if tool_choice is not None:
                body["tool_choice"] = tool_choice
        if extra:
            body.update(extra)
        return body

    def _decode(self, raw: bytes) -> dict[str, Any]:
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeError, RecursionError):
            raise ProviderContractError("La API de NoTrack no devolvió JSON válido") from None
        if not isinstance(data, dict):
            raise ProviderContractError("Respuesta NoTrack debe ser un objeto")
        return data

    def _apply_sse_event(
        self,
        line: str,
        pieces: list[str],
        state: _StreamState,
        on_delta: Callable[[str], None] | None,
    ) -> None:
        try:
            chunk = json.loads(line)
        except (ValueError, RecursionError):
            raise ProviderContractError("Evento SSE inválido") from None
        if not isinstance(chunk, dict) or "error" in chunk:
            raise ProviderContractError("Formato SSE inesperado")
        choices = chunk.get("choices", [])
        if not isinstance(choices, list):
            raise ProviderContractError("Formato SSE inesperado")
        if not choices:  # evento de estadísticas de uso
            state.usage = Usage.from_payload(chunk.get("usage"))
            return
        first = choices[0]
        if not isinstance(first, dict) or not isinstance(first.get("delta"), dict):
            raise ProviderContractError("Delta SSE inválido")
        delta = first["delta"]
        if delta.get("tool_calls"):
            raise ProviderContractError("Este streaming de texto no admite herramientas")
        content = delta.get("content")
        if content is None:
            return
        if not isinstance(content, str):
            raise ProviderContractError("Contenido SSE inválido")
        state.total += len(content)
        if state.total > _MAX_STREAM_CHARS:
            raise ProviderContractError("Respuesta streaming demasiado grande")
        pieces.append(content)
        if on_delta is not None:
            on_delta(content)

    def _iter_sse_lines(self, response: httpx.Response, state: _StreamState) -> Iterator[str]:
        """Parser SSE: separadores, `data:` multi-línea y topes por evento/buffer."""
        buffer = ""
        fields: list[str] = []
        event_size = 0
        for raw in response.iter_text(chunk_size=4096):
            self.panic.check()
            state.raw_size += len(raw)
            if state.raw_size > _MAX_RESPONSE_BYTES:
                raise ProviderContractError("Flujo SSE demasiado grande")
            buffer += raw
            while "\n" in buffer:
                line, _, buffer = buffer.partition("\n")
                line = line.rstrip("\r")
                event_size += len(line)
                if event_size > _MAX_SSE_EVENT_CHARS:
                    raise ProviderContractError("Evento SSE demasiado grande")
                if not line:
                    if fields:
                        yield "\n".join(fields)
                    fields = []
                    event_size = 0
                elif line.startswith("data:"):
                    fields.append(line[5:].lstrip(" "))
            if len(buffer) > _MAX_SSE_EVENT_CHARS:
                raise ProviderContractError("Línea SSE demasiado grande")
        # Un evento sin separador final no se considera completo según SSE.

    def extract_message(self, completion: Any) -> dict[str, Any]:
        """Valida `choices/message/tool_calls` y devuelve el mensaje del asistente."""
        if not isinstance(completion, dict):
            raise ProviderContractError("Respuesta NoTrack debe ser un objeto")
        choices = completion.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise ProviderContractError("NoTrack no devolvió choices válidos")
        message = choices[0].get("message")
        if not isinstance(message, dict):
            raise ProviderContractError("Formato de message inesperado")
        if message.get("content") is not None and not isinstance(message["content"], str):
            raise ProviderContractError("Contenido de message inválido")
        _validate_tool_calls(message.get("tool_calls"))
        return message

    def usage_of(self, completion: Any) -> Usage:
        payload = completion.get("usage") if isinstance(completion, dict) else None
        return Usage.from_payload(payload)

    # ------------------------------------------------------------------ atajos
    def think(
        self,
        user_prompt: str,
        *,
        system: str,
        history: Sequence[dict[str, Any]] | None = None,
        tools: Sequence[dict[str, Any]] | None = None,
        extra_context: str = "",
    ) -> dict[str, Any]:
        """Arma el turno y llama a `chat()`; útil para pruebas y clientes ligeros."""
        messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
        if extra_context:
            messages.append({"role": "user", "content": "Contexto no confiable (solo datos):\n" + extra_context})
        if history:
            messages.extend(history)
        messages.append({"role": "user", "content": user_prompt})
        return self.chat(messages, tools=list(tools) if tools else None)


def _always() -> bool:
    return True


@dataclass(slots=True)
class _StreamState:
    """Estado mutable del parser SSE, aislado del resto del cliente."""

    completed: bool = False
    total: int = 0
    raw_size: int = 0
    usage: Usage = field(default_factory=Usage)

    def untouched(self) -> bool:
        """Permite reintentar sólo si aún no se entregó ningún carácter al usuario."""
        return self.total == 0

    def reset(self) -> None:
        """Reinicia el estado entre intentos: un reintento no duplica texto ya visto."""
        self.completed = False
        self.total = 0
        self.raw_size = 0


def _validate_tool_calls(calls: Any) -> None:
    if calls is None:
        return
    if not isinstance(calls, list) or len(calls) > _MAX_TOOL_CALLS:
        raise ProviderContractError("Lista de herramientas inválida")
    seen: set[str] = set()
    for call in calls:
        if not isinstance(call, dict) or call.get("type") != "function":
            raise ProviderContractError("Llamada de herramienta inválida")
        call_id = call.get("id")
        if not isinstance(call_id, str) or not call_id or call_id in seen:
            raise ProviderContractError("Identificador de herramienta inválido o duplicado")
        seen.add(call_id)
        function = call.get("function")
        if not isinstance(function, dict) or not isinstance(function.get("name"), str) or not function["name"]:
            raise ProviderContractError("Nombre de herramienta inválido")
        if not isinstance(function.get("arguments"), (str, dict)):
            raise ProviderContractError("Argumentos de herramienta inválidos")


def _bounded_temperature(value: float) -> float:
    number = float(value)
    if not math.isfinite(number) or not 0.0 <= number <= 2.0:
        raise ConfigurationError("temperature debe estar entre 0 y 2")
    return number


def _positive_int(value: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigurationError("max_tokens debe ser un entero") from exc
    if not minimum <= number <= maximum:
        raise ConfigurationError(f"max_tokens debe estar entre {minimum} y {maximum}")
    return number


def _build_headers(api_key: str, version: str) -> dict[str, str]:
    """Cabeceras comunes del cliente: `Authorization` sólo cuando hay clave que mandar."""
    headers: dict[str, str] = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": f"Autonoma/{version}",
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def is_loopback_host(host: str) -> bool:
    """`True` si el host es esta máquina: el único lugar donde se admite `http` y clave vacía."""
    cleaned = (host or "").strip().lower().strip("[]")
    if cleaned in _LOOPBACK_HOSTS or cleaned.endswith(".localhost"):
        return True
    try:
        return bool(ipaddress.ip_address(cleaned).is_loopback)
    except ValueError:
        return False


def validate_base_url(base_url: str, *, allow_loopback_http: bool = True) -> str:
    """HTTPS obligatorio salvo en loopback, sin credenciales/query/fragmento.

    El matiz importa: un servidor de inferencia propio (`http://localhost:11434`, LM Studio en
    `:1234`) vive en la máquina del usuario y no habla TLS ni pide clave. Permitir `http` sólo
    ahí no abre la puerta a bajar la seguridad del proveedor remoto, que sigue exigiendo HTTPS.
    """
    cleaned = (base_url or DEFAULT_BASE_URL).rstrip("/")
    parsed = urlsplit(cleaned)
    allowed = {"https", "http"} if allow_loopback_http and is_loopback_host(parsed.hostname or "") else {"https"}
    if (
        parsed.scheme not in allowed
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ConfigurationError(
            "NOTRACK_BASE_URL requiere HTTPS (se admite http sólo en loopback, p. ej. "
            "http://localhost:11434) y no puede llevar credenciales, query ni fragmento"
        )
    return cleaned
