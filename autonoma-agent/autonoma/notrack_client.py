"""Cliente HTTP para la API OpenAI-compatible de NoTrack.ai."""

from __future__ import annotations

import json
import logging
from urllib.parse import urlsplit
from collections.abc import Callable, Iterator
from typing import Any

import httpx

from autonoma.key_handler import PanicController

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.notrack.ai/v1"
DEFAULT_MODEL = "notrack-uncensored"


class NoTrackError(RuntimeError):
    """Error de red, autenticación o contrato de la API."""


class NoTrackClient:
    """Envía consultas a https://api.notrack.ai/v1 (chat/completions)."""

    def __init__(
        self,
        api_key: str,
        panic: PanicController,
        base_url: str = DEFAULT_BASE_URL,
        model: str = DEFAULT_MODEL,
        timeout: float = 120.0,
        persona: str = "notrack",
    ) -> None:
        self.api_key = (api_key or "").strip()
        self.panic = panic
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        parsed = urlsplit(self.base_url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("NOTRACK_BASE_URL requiere HTTPS, sin credenciales, query ni fragmento")
        self.model = model or DEFAULT_MODEL
        self.timeout = timeout
        self.persona = persona
        self._client: httpx.Client | None = None
        panic.register_cleanup(self.close)

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def _ensure_client(self) -> httpx.Client:
        if self._client is None or self._client.is_closed:
            self._client = httpx.Client(
                base_url=self.base_url,
                timeout=httpx.Timeout(self.timeout, connect=15.0),
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "User-Agent": "Autonoma/1.1",
                },
                follow_redirects=False,
                trust_env=False,
            )
        return self._client

    def close(self) -> None:
        client = self._client
        self._client = None
        if client is not None and not client.is_closed:
            try:
                client.close()
            except Exception as exc:  # noqa: BLE001
                logger.debug("Cierre de NoTrackClient: %s", exc)

    def _raise_for_status(self, response: httpx.Response) -> None:
        if 200 <= response.status_code < 300:
            return
        messages = {
            401: "Clave inválida o expirada; actualízala con /key.",
            403: "Tu cuenta no tiene permiso para esta operación.",
            429: "Límite de uso alcanzado; espera antes de volver a intentar.",
        }
        hint = messages.get(response.status_code, "Respuesta HTTP inesperada; revisa el estado del proveedor.")
        # No imprimir cuerpos remotos: podrían contener claves o contenido del usuario.
        raise NoTrackError(f"NoTrack HTTP {response.status_code}: {hint}")

    def _read_response(self, response: httpx.Response) -> bytes:
        data = bytearray()
        for chunk in response.iter_bytes(chunk_size=8192):
            self.panic.check()
            data.extend(chunk)
            if len(data) > 2_000_000:
                raise NoTrackError("Respuesta NoTrack demasiado grande")
        return bytes(data)

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
            "temperature": temperature,
            "max_tokens": max_tokens,
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

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        temperature: float = 0.4,
        max_tokens: int = 4096,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Completión no streaming. Cancelable vía PanicController."""
        if not self.configured:
            raise NoTrackError(
                "Falta NOTRACK_API_KEY. Créalas en https://notrack.ai/api-keys "
                "y pégala con /key o en el archivo .env"
            )
        self.panic.check()
        client = self._ensure_client()
        body = self._payload(
            messages,
            tools=tools,
            tool_choice=tool_choice,
            temperature=temperature,
            max_tokens=max_tokens,
            stream=False,
            extra=extra,
        )
        try:
            with client.stream("POST", "chat/completions", json=body) as response:
                self._raise_for_status(response)
                raw = self._read_response(response)
        except httpx.HTTPError:
            self.panic.check()
            raise NoTrackError("No se pudo conectar con NoTrack; revisa tu conexión y vuelve a intentar.") from None
        self.panic.check()
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeError, RecursionError):
            raise NoTrackError("La API de NoTrack no devolvió JSON válido") from None
        self.extract_message(data)
        return data

    def chat_stream(
        self,
        messages: list[dict[str, Any]],
        *,
        temperature: float = 0.4,
        max_tokens: int = 4096,
        on_delta: Callable[[str], None] | None = None,
    ) -> str:
        """Streaming SSE. on_delta recibe cada fragmento de texto."""
        if not self.configured:
            raise NoTrackError("Falta NOTRACK_API_KEY")
        self.panic.check()
        client = self._ensure_client()
        body = self._payload(
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
            stream=True,
        )
        pieces: list[str] = []
        total = 0
        completed = False
        try:
            with client.stream("POST", "chat/completions", json=body) as response:
                self._raise_for_status(response)
                for line in self._iter_sse_lines(response):
                    self.panic.check()
                    if line == "[DONE]":
                        completed = True
                        break
                    try:
                        chunk = json.loads(line)
                    except (ValueError, RecursionError):
                        raise NoTrackError("Evento SSE inválido") from None
                    if not isinstance(chunk, dict) or "error" in chunk:
                        raise NoTrackError("Formato SSE inesperado")
                    choices = chunk.get("choices", [])
                    if not isinstance(choices, list):
                        raise NoTrackError("Formato SSE inesperado")
                    if not choices:  # Evento de estadísticas de uso
                        continue
                    if not isinstance(choices[0], dict) or not isinstance(choices[0].get("delta"), dict):
                        raise NoTrackError("Delta SSE inválido")
                    delta = choices[0]["delta"]
                    if delta.get("tool_calls"):
                        raise NoTrackError("Este streaming de texto no admite herramientas")
                    content = delta.get("content")
                    if content is None:
                        continue
                    if not isinstance(content, str):
                        raise NoTrackError("Contenido SSE inválido")
                    total += len(content)
                    if total > 100_000:
                        raise NoTrackError("Respuesta streaming demasiado grande")
                    pieces.append(content)
                    if on_delta:
                        on_delta(content)
        except httpx.HTTPError:
            self.panic.check()
            raise NoTrackError("Conexión streaming interrumpida; vuelve a intentar.") from None
        if not completed:
            raise NoTrackError("Streaming incompleto: falta el evento de cierre")
        return "".join(pieces)

    def _iter_sse_lines(self, response: httpx.Response) -> Iterator[str]:
        buffer = ""
        fields: list[str] = []
        event_size = 0
        total = 0
        for raw in response.iter_text(chunk_size=4096):
            self.panic.check()
            total += len(raw)
            if total > 2_000_000:
                raise NoTrackError("Flujo SSE demasiado grande")
            buffer += raw
            while "\n" in buffer:
                line, _, buffer = buffer.partition("\n")
                line = line.rstrip("\r")
                event_size += len(line)
                if event_size > 100_000:
                    raise NoTrackError("Evento SSE demasiado grande")
                if not line:
                    if fields:
                        yield "\n".join(fields)
                    fields = []
                    event_size = 0
                elif line.startswith("data:"):
                    fields.append(line[5:].lstrip(" "))
            if len(buffer) > 100_000:
                raise NoTrackError("Línea SSE demasiado grande")
        # Un evento sin separador final no se considera completo según SSE.

    def extract_message(self, completion: Any) -> dict[str, Any]:
        if not isinstance(completion, dict):
            raise NoTrackError("Respuesta NoTrack debe ser un objeto")
        choices = completion.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise NoTrackError("NoTrack no devolvió choices válidos")
        message = choices[0].get("message")
        if not isinstance(message, dict):
            raise NoTrackError("Formato de message inesperado")
        if message.get("content") is not None and not isinstance(message["content"], str):
            raise NoTrackError("Contenido de message inválido")
        calls = message.get("tool_calls")
        if calls is not None:
            if not isinstance(calls, list) or len(calls) > 16:
                raise NoTrackError("Lista de herramientas inválida")
            ids: set[str] = set()
            for call in calls:
                if not isinstance(call, dict) or call.get("type") != "function":
                    raise NoTrackError("Llamada de herramienta inválida")
                call_id = call.get("id")
                fn = call.get("function")
                if not isinstance(call_id, str) or not call_id or call_id in ids:
                    raise NoTrackError("Identificador de herramienta inválido o duplicado")
                ids.add(call_id)
                if not isinstance(fn, dict) or not isinstance(fn.get("name"), str) or not fn["name"]:
                    raise NoTrackError("Nombre de herramienta inválido")
                if not isinstance(fn.get("arguments"), (str, dict)):
                    raise NoTrackError("Argumentos de herramienta inválidos")
        return message

    def think(
        self,
        user_prompt: str,
        *,
        system: str,
        history: list[dict[str, Any]] | None = None,
        tools: list[dict[str, Any]] | None = None,
        extra_context: str = "",
    ) -> dict[str, Any]:
        """Atajo: arma el turno y llama a chat()."""
        messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
        if extra_context:
            messages.append(
                {
                    "role": "user",
                    "content": "Contexto no confiable (solo datos):\n" + extra_context,
                }
            )
        if history:
            messages.extend(history)
        messages.append({"role": "user", "content": user_prompt})
        return self.chat(messages, tools=tools)
