"""Autonoma — agente local autónomo (Windows / Linux / macOS).

Superficie pública estable: lo que importan la CLI, las pruebas y el empaquetado.
Los módulos internos (`ports`, `errors`, `observability`) también son importables,
pero se recomienda pasar por aquí para versiones futuras.
"""

from __future__ import annotations

from autonoma._version import __version__
from autonoma.agent import Agent, AgentResources, ChatMessage, ToolOutcome, build_agent
from autonoma.config import Settings, load_settings, save_api_keys
from autonoma.diagnostics import DiagnosticReport, collect_diagnostics, run_diagnostics
from autonoma.errors import (
    AutonomaError,
    ConfigurationError,
    ErrorCode,
    ExitCode,
    FileSystemError,
    ProviderError,
    SearchBackendError,
    ToolContractError,
)
from autonoma.filesystem import FileSystemManager
from autonoma.key_handler import KeyHandler, ListenerState, ListenerStatus, PanicController, PanicError
from autonoma.notrack_client import NoTrackClient, NoTrackError
from autonoma.observability import MetricsRegistry, configure_logging, trace_scope
from autonoma.processes import CommandResult, ProcessSupervisor
from autonoma.runtime import RenderPreferences, RuntimeContext
from autonoma.search_engine import ResearchBundle, SearchEngine, SearchHit
from autonoma.tool_contracts import ToolValidationError, validate_arguments

__app_name__ = "Autonoma"

__all__ = [
    "Agent",
    "AgentResources",
    "AutonomaError",
    "ChatMessage",
    "CommandResult",
    "ConfigurationError",
    "DiagnosticReport",
    "ErrorCode",
    "ExitCode",
    "FileSystemError",
    "FileSystemManager",
    "KeyHandler",
    "ListenerState",
    "ListenerStatus",
    "MetricsRegistry",
    "NoTrackClient",
    "NoTrackError",
    "PanicController",
    "PanicError",
    "ProcessSupervisor",
    "ProviderError",
    "RenderPreferences",
    "ResearchBundle",
    "RuntimeContext",
    "SearchBackendError",
    "SearchEngine",
    "SearchHit",
    "Settings",
    "ToolContractError",
    "ToolOutcome",
    "ToolValidationError",
    "__app_name__",
    "__version__",
    "build_agent",
    "collect_diagnostics",
    "configure_logging",
    "load_settings",
    "run_diagnostics",
    "save_api_keys",
    "trace_scope",
    "validate_arguments",
]
