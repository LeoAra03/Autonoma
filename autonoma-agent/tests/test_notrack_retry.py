"""Reintentos y contrato del proveedor: sólo lo transitorio, con `Retry-After` acotado."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from autonoma.errors import ProviderContractError, ProviderHttpError, ProviderUnavailableError
from autonoma.key_handler import PanicController
from autonoma.notrack_client import NoTrackClient


class WaitingPanic(PanicController):
    """Controlador que registra los descansos en lugar de dormir: pruebas rápidas y deterministas."""

    def __init__(self) -> None:
        super().__init__()
        self.waits: list[float | None] = []

    def wait(self, timeout: float | None = None) -> bool:
        self.waits.append(timeout)
        return False


def client_for(
    handler: Any, *, retries: int = 3, backoff: float = 0.0, panic: PanicController | None = None
) -> NoTrackClient:
    client = NoTrackClient(
        "sk-prueba-123456",
        panic if panic is not None else PanicController(),
        base_url="https://notrack.example/v1",
        max_retries=retries,
        retry_backoff=backoff,
    )
    client._client = httpx.Client(  # inyección de transporte: la política de reintento es la real
        transport=httpx.MockTransport(handler), base_url="https://notrack.example/v1"
    )
    return client


def completion(content: str = "ok") -> dict[str, Any]:
    return {"choices": [{"message": {"role": "assistant", "content": content}}]}


def test_transient_status_is_retried_until_success() -> None:
    attempts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request.url.path)
        if len(attempts) < 3:
            return httpx.Response(503, json={"error": "mantenimiento"})
        return httpx.Response(200, json=completion("listo"))

    client = client_for(handler)
    assert client.chat([{"role": "user", "content": "hola"}])["choices"][0]["message"]["content"] == "listo"
    assert len(attempts) == 3


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_client_errors_are_not_retried(status: int) -> None:
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(status)
        return httpx.Response(status, json={"error": {"message": "mal pedido"}})

    client = client_for(handler)
    with pytest.raises(ProviderHttpError) as excinfo:
        client.chat([{"role": "user", "content": "hola"}])
    assert excinfo.value.status_code == status
    assert not excinfo.value.retryable
    assert len(attempts) == 1  # un fallo determinista no se repite


@pytest.mark.parametrize("status", [408, 409, 425, 429, 500, 502, 503, 504])
def test_only_the_transient_whitelist_is_retried(status: int) -> None:
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(status)
        return httpx.Response(status, json={"error": "transitorio"})

    client = client_for(handler, retries=2)
    with pytest.raises(ProviderHttpError) as excinfo:
        client.chat([{"role": "user", "content": "hola"}])
    assert excinfo.value.retryable
    assert len(attempts) == 3  # intento inicial + 2 reintentos


def test_retry_after_is_honoured_but_capped() -> None:
    panic = WaitingPanic()
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        if len(attempts) == 1:
            return httpx.Response(429, headers={"Retry-After": "3600"}, json={"error": "lento"})
        return httpx.Response(200, json=completion())

    client = client_for(handler, retries=1, panic=panic)
    client.chat([{"role": "user", "content": "hola"}])
    assert panic.waits == [pytest.approx(10.0)]  # el techo de seguridad manda sobre el proveedor
    assert attempts == [1, 1]


def test_numeric_backoff_is_exponential_when_there_is_no_header() -> None:
    panic = WaitingPanic()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "mantenimiento"})

    client = client_for(handler, retries=3, backoff=0.5, panic=panic)
    with pytest.raises(ProviderHttpError):
        client.chat([{"role": "user", "content": "hola"}])
    assert panic.waits == [0.5, 1.0, 2.0]


def test_transport_failures_are_wrapped_as_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("conexión rechazada", request=request)

    client = client_for(handler, retries=1)
    with pytest.raises(ProviderUnavailableError) as excinfo:
        client.chat([{"role": "user", "content": "hola"}])
    assert excinfo.value.retryable
    # El detalle crudo se encadena para el log pero no se filtra al modelo/usuario.
    assert isinstance(excinfo.value.__cause__, httpx.ConnectError)
    assert "rechazada" not in excinfo.value.user_message()


def test_panicking_during_retries_stops_immediately() -> None:
    from autonoma.key_handler import PanicError

    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        client.panic.panic()
        return httpx.Response(503, json={"error": "mantenimiento"})

    client = client_for(handler, retries=5)
    with pytest.raises((ProviderUnavailableError, PanicError)):
        client.chat([{"role": "user", "content": "hola"}])
    assert len(attempts) <= 2  # no se agotan los 6 intentos contra un proveedor caído


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"choices": []},
        {"choices": [{"message": None}]},
        "no-es-dict",
        {"choices": [{"message": {"content": 42}}]},
    ],
)
def test_malformed_provider_payloads_raise_a_typed_contract_error(payload: Any) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=json.dumps(payload) if not isinstance(payload, str) else payload)

    client = client_for(handler)
    # `chat` valida el contrato en la frontera: un payload roto nunca llega al bucle.
    with pytest.raises(ProviderContractError):
        client.chat([{"role": "user", "content": "hola"}])


def test_streaming_requires_a_terminal_event_and_rejects_partial_tools() -> None:
    sse = (
        b'data: {"choices":[{"delta":{"content":"ho"}}]}\n\n'
        b'data: {"choices":[{"delta":{"content":"la"}}]}\n\n'
        b"data: [DONE]\n\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=sse, headers={"content-type": "text/event-stream"})

    client = client_for(handler)
    collected: list[str] = []
    assert client.chat_stream([{"role": "user", "content": "hola"}], on_delta=collected.append) == "hola"
    assert collected == ["ho", "la"]


def test_truncated_stream_is_not_reported_as_a_complete_answer() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b'data: {"choices":[{"delta":{"content":"parcial"}}]}\n\n',
            headers={"content-type": "text/event-stream"},
        )

    client = client_for(handler)
    with pytest.raises(ProviderContractError):
        client.chat_stream([{"role": "user", "content": "hola"}])
