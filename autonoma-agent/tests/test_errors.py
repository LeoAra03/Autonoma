"""Contratos de la taxonomía de errores: códigos, severidad, redacción y salidas."""

from __future__ import annotations

import logging

import pytest

from autonoma.errors import (
    AutonomaError,
    ConfigurationError,
    ErrorCode,
    ExitCode,
    FileSystemError,
    NetworkPolicyError,
    PathPolicyError,
    ProcessTimeoutError,
    ProviderContractError,
    ProviderHttpError,
    ProviderUnavailableError,
    SearchBackendError,
    ToolContractError,
    ToolExecutionDeniedError,
    classify_log_level,
    describe,
    redact,
    traits_for,
)


def test_every_code_has_traits_and_exit_code() -> None:
    seen: set[int] = set()
    for code in ErrorCode:
        traits = traits_for(code)
        assert isinstance(traits.severity, int) and traits.severity >= logging.DEBUG
        assert isinstance(traits.retryable, bool)
        assert isinstance(traits.exit_code, ExitCode)
        assert traits.hint
        seen.add(int(traits.exit_code))
    assert int(ExitCode.SUCCESS) not in seen  # ningún error reutiliza el éxito


@pytest.mark.parametrize(
    ("exception", "expected_code"),
    [
        (ConfigurationError("x"), ErrorCode.CONFIGURATION),
        (FileSystemError("x"), ErrorCode.FILESYSTEM_IO),
        (PathPolicyError("x"), ErrorCode.PATH_POLICY),
        (ToolContractError("x"), ErrorCode.TOOL_CONTRACT),
        (ToolExecutionDeniedError("x"), ErrorCode.APPROVAL_REQUIRED),
        (ProviderUnavailableError("x"), ErrorCode.PROVIDER_UNAVAILABLE),
        (ProviderContractError("x"), ErrorCode.PROVIDER_CONTRACT),
        (NetworkPolicyError("x"), ErrorCode.NETWORK_POLICY),
        (ProcessTimeoutError("x"), ErrorCode.PROCESS_TIMEOUT),
        (RuntimeError("x"), ErrorCode.INTERNAL),
        (FileNotFoundError("x"), ErrorCode.FILESYSTEM_IO),
        (KeyboardInterrupt(), ErrorCode.CANCELLED),
    ],
)
def test_describe_classifies_without_reading_messages(exception: BaseException, expected_code: ErrorCode) -> None:
    code, _retryable = describe(exception)
    assert code is expected_code


def test_redaction_bounds_length_and_hides_secrets() -> None:
    secret = "sk-supersecreto-abcdef123456"
    text = redact(f"authorization: {secret} y más {'x' * 2000}", [secret])
    assert secret not in text
    assert "(redactado)" in text
    assert len(text) < 700
    assert text.endswith("…[truncado]")


def test_short_or_empty_secrets_are_not_used_for_scrubbing() -> None:
    # Un secreto corto coincidiría con texto inocuo y corrompería el log.
    assert redact("clave: ab", ["ab"]) == "clave: ab"


def test_context_masks_secret_keys_and_is_read_only() -> None:
    error = ConfigurationError("no vale", context={"notrack_api_key": "sk-abcdef123", "field": "http_timeout"})
    assert error.to_log_fields()["ctx_notrack_api_key"] == "(redactado)"
    assert error.to_log_fields()["ctx_field"] == "http_timeout"
    with pytest.raises(TypeError):
        error.context["nuevo"] = "x"  # type: ignore[index]


def test_user_message_is_actionable() -> None:
    error = ProviderUnavailableError("sin conexión")
    assert "verifica la conexión" in error.user_message().lower()
    assert error.retryable and error.exit_code is ExitCode.PROVIDER


def test_provider_http_retryability_depends_on_status() -> None:
    assert ProviderHttpError("x", status_code=429).retryable
    assert not ProviderHttpError("x", status_code=401).retryable
    assert ProviderHttpError("x", status_code=500).exit_code is ExitCode.PROVIDER


def test_search_backend_error_keeps_per_backend_detail() -> None:
    error = SearchBackendError("todo falló", failures=(("brave-api", "HTTP 503"), ("playwright", "no instalado")))
    assert [name for name, _ in error.failures] == ["brave-api", "playwright"]
    assert "playwright" in error.context["backends"]


def test_autonoma_error_is_runtime_error_by_default() -> None:
    assert isinstance(FileSystemError("x"), RuntimeError)
    assert not isinstance(FileSystemError("x"), ValueError)
    # Configuración y política de rutas siguen siendo ValueErrors para los llamadores viejos.
    assert isinstance(ConfigurationError("x"), ValueError)
    assert isinstance(NetworkPolicyError("x"), ValueError)


@pytest.mark.parametrize(
    ("exception", "level"),
    [
        (ToolExecutionDeniedError("x"), logging.INFO),
        (FileSystemError("x"), logging.ERROR),
        (KeyboardInterrupt(), logging.DEBUG),
        (RuntimeError("x"), logging.ERROR),
    ],
)
def test_log_levels_come_from_taxonomy(exception: BaseException, level: int) -> None:
    assert classify_log_level(exception) == level


def test_base_error_carries_code_and_severity() -> None:
    error = AutonomaError("generico")
    assert error.code is ErrorCode.INTERNAL
    assert error.severity == logging.ERROR
    assert not error.retryable
    assert error.exit_code is ExitCode.INTERNAL
    assert error.message == "generico"
