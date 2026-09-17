"""Autonoma — agente local autónomo (Windows / Linux / macOS)."""

from __future__ import annotations

__version__ = "1.1.0"
__app_name__ = "Autonoma"

from autonoma.agent import Agent
from autonoma.filesystem import FileSystemManager
from autonoma.key_handler import KeyHandler, PanicController, PanicError
from autonoma.notrack_client import NoTrackClient
from autonoma.search_engine import SearchEngine

__all__ = [
    "Agent",
    "FileSystemManager",
    "KeyHandler",
    "NoTrackClient",
    "PanicController",
    "PanicError",
    "SearchEngine",
    "__app_name__",
    "__version__",
]
