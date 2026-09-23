import json

import httpx
import pytest

from autonoma.agent import Agent
from autonoma.config import Settings
from autonoma.errors import ErrorCode
from autonoma.key_handler import PanicController, PanicError
from autonoma.notrack_client import NoTrackClient, NoTrackError
from autonoma.tool_contracts import ToolValidationError, tool_schemas, validate_arguments
from autonoma.tool_registry import ToolRegistry


@pytest.mark.parametrize(
    "name,args",
    [
        ("write_file", {"path": "", "content": "x"}),
        ("write_file", {"path": "x", "content": "x", "force": "false"}),
        ("write_file", {"path": "x", "content": "x", "force": 1}),
        ("write_file", {"path": "x", "content": "x" * 400_001}),
        ("read_file", {"path": "x\x00"}),
        ("read_file", {"path": None}),
        ("read_file", {"path": 42}),
        ("read_file", {"path": "x", "unknown": 1}),
        ("read_file", {}),
        ("read_file", "[]"),
        ("read_file", "{bad"),
        ("run_command", {"command": "echo x", "timeout": True}),
        ("run_command", {"command": "echo x", "timeout": float("nan")}),
        ("run_command", {"command": "echo x", "timeout": 1801}),
        ("web_search", {"query": "x", "fetch_pages": -1}),
        ("web_search", {"query": "x", "fetch_pages": 6}),
        ("web_search", {"query": "x", "fetch_pages": 1.5}),
        ("unknown", {}),
        ("read_file", "x" * 100001),
    ],
)
def test_invalid_arguments(name, args):
    with pytest.raises(ToolValidationError):
        validate_arguments(name, args)


def test_registry_matches_contract():
    assert set(ToolRegistry(None, None).handlers) == {t["function"]["name"] for t in tool_schemas()}
    assert validate_arguments("web_search", '{"query":"hola","fetch_pages":0}')["fetch_pages"] == 0
    assert validate_arguments("write_file", {"path": "x", "content": "", "force": False})["content"] == ""


def test_no_approval_for_invalid_or_disabled_command():
    approvals = []
    agent = Agent(Settings(), PanicController(), None, None, None, approve=lambda *a: approvals.append(a))
    disabled = agent.run_tool("run_command", {"command": "echo x"})
    assert not disabled.ok and "deshabilitados" in disabled.output
    assert disabled.error_code is ErrorCode.APPROVAL_REQUIRED
    invalid = agent.run_tool("write_file", {"path": ""})
    assert not invalid.ok
    assert invalid.error_code is ErrorCode.TOOL_CONTRACT
    # Nada llegó a pedir aprobación: el contrato y la política se cumplen antes.
    assert approvals == []


@pytest.fixture
def client():
    c = NoTrackClient("test-secret", PanicController())
    yield c
    c.close()


def transport(client, response):
    client._client = httpx.Client(
        base_url="https://api.notrack.ai/v1/", transport=httpx.MockTransport(lambda _: response)
    )


def completion(content="hola", calls=None):
    message = {"content": content}
    if calls is not None:
        message["tool_calls"] = calls
    return {"choices": [{"message": message}]}


def test_chat_request_and_response(client):
    seen = []

    def handle(request):
        seen.append(request)
        return httpx.Response(200, json=completion())

    client._client = httpx.Client(base_url=client.base_url, transport=httpx.MockTransport(handle))
    result = client.chat([{"role": "user", "content": "hola"}])
    assert client.extract_message(result)["content"] == "hola"
    assert str(seen[0].url) == "https://api.notrack.ai/v1/chat/completions"
    assert json.loads(seen[0].content)["stream"] is False


@pytest.mark.parametrize("status", [301, 401, 403, 429, 500])
def test_status_errors_are_redacted(client, status):
    transport(client, httpx.Response(status, text="test-secret private prompt"))
    with pytest.raises(NoTrackError) as exc:
        client.chat([])
    assert str(status) in str(exc.value)
    assert "test-secret" not in str(exc.value) and "private prompt" not in str(exc.value)


@pytest.mark.parametrize(
    "data",
    [
        None,
        [],
        {},
        {"choices": {}},
        {"choices": ["x"]},
        {"choices": [{"message": "x"}]},
        completion(42),
        completion(calls={}),
        completion(calls=[{}]),
        completion(calls=[{"type": "function", "id": "", "function": {}}]),
        completion(calls=[{"type": "function", "id": "x", "function": {"name": "x", "arguments": 42}}]),
    ],
)
def test_malformed_completion(client, data):
    with pytest.raises(NoTrackError):
        client.extract_message(data)


def test_duplicate_call_ids(client):
    call = {"type": "function", "id": "x", "function": {"name": "read_file", "arguments": "{}"}}
    assert client.extract_message(completion(calls=[call]))["tool_calls"] == [call]
    with pytest.raises(NoTrackError, match="duplicado"):
        client.extract_message(completion(calls=[call, call]))


@pytest.mark.parametrize("body", [b"not json", b"x" * 2_000_001])
def test_invalid_response_body(client, body):
    transport(client, httpx.Response(200, content=body))
    with pytest.raises(NoTrackError):
        client.chat([])


def test_transport_error_redacted(client):
    def handle(request):
        raise httpx.ConnectError("test-secret", request=request)

    client._client = httpx.Client(base_url=client.base_url, transport=httpx.MockTransport(handle))
    with pytest.raises(NoTrackError) as exc:
        client.chat([])
    assert "test-secret" not in str(exc.value)


def test_missing_key_and_panic(client):
    client.api_key = ""
    with pytest.raises(NoTrackError, match="Falta"):
        client.chat([])
    with pytest.raises(NoTrackError, match="Falta"):
        client.chat_stream([])
    client.api_key = "test"
    client.panic.panic()
    with pytest.raises(PanicError):
        client.chat([])


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com",
        "https://user:pass@example.com",
        "https://example.com?q=1",
        "https://example.com#x",
        "file:///tmp",
    ],
)
def test_insecure_api_url(url):
    with pytest.raises(ValueError, match="HTTPS"):
        NoTrackClient("key", PanicController(), base_url=url)


def test_stream(client):
    event = json.dumps({"choices": [{"delta": {"content": "hola"}}]})
    transport(client, httpx.Response(200, text=": keepalive\r\n\r\ndata: " + event + "\r\n\r\ndata: [DONE]\r\n\r\n"))
    parts = []
    assert client.chat_stream([], on_delta=parts.append) == "hola"
    assert parts == ["hola"]


@pytest.mark.parametrize(
    "stream",
    [
        "data: {bad}\n\n",
        "data: []\n\n",
        'data: {"choices":{}}\n\n',
        'data: {"choices":[{}]}\n\n',
        'data: {"choices":[{"delta":{"content":42}}]}\n\n',
        'data: {"choices":[{"delta":{"tool_calls":[{}]}}]}\n\n',
        "data: {}\n\n",
        "data: [DONE]",
        "data: " + ("x" * 100001),
    ],
)
def test_stream_errors(client, stream):
    transport(client, httpx.Response(200, text=stream))
    with pytest.raises(NoTrackError):
        client.chat_stream([])


def test_client_config_and_close(client):
    http = client._ensure_client()
    assert not http.follow_redirects
    assert http.headers["authorization"] == "Bearer test-secret"
    client.close()
    assert http.is_closed
    client.close()
