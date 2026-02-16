from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Any


class BackendExecutionError(RuntimeError):
    """Raised when a backend process execution fails."""

    def __init__(
        self,
        message: str,
        *,
        backend: str | None = None,
        exit_code: int | None = None,
        retriable: bool = True,
    ) -> None:
        super().__init__(message)
        self.backend = backend
        self.exit_code = exit_code
        self.retriable = retriable


class BackendTimeoutError(BackendExecutionError):
    """Raised when backend execution exceeds configured timeout."""


class BackendProcessError(BackendExecutionError):
    """Raised when backend process lifecycle fails."""


class AgentBackend(ABC):
    @abstractmethod
    async def execute(
        self,
        system_prompt: str,
        user_prompt: str,
        context: dict[str, Any],
        tools: list[str] | None = None,
    ) -> AsyncIterator[str]:
        """Execute an agent and stream textual chunks."""

    @abstractmethod
    async def execute_with_tools(
        self,
        system_prompt: str,
        user_prompt: str,
        allowed_tools: list[str],
    ) -> dict[str, Any]:
        """Execute an agent and return a structured payload."""
