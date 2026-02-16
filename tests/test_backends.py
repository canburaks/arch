import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from architect.backends import RetryPolicy
from architect.backends.base import AgentBackend, BackendExecutionError
from architect.backends.claude import ClaudeCodeBackend
from architect.backends.codex import CodexBackend
from architect.backends.resilient import ResilientBackend


class AlwaysFailBackend(AgentBackend):
    async def execute(
        self,
        system_prompt: str,
        user_prompt: str,
        context: dict[str, Any],
        tools: list[str] | None = None,
    ) -> AsyncIterator[str]:
        _ = system_prompt, user_prompt, context, tools
        raise BackendExecutionError("boom", backend="fake", retriable=True)
        yield ""  # pragma: no cover

    async def execute_with_tools(
        self,
        system_prompt: str,
        user_prompt: str,
        allowed_tools: list[str],
    ) -> dict[str, Any]:
        _ = system_prompt, user_prompt, allowed_tools
        raise BackendExecutionError("boom", backend="fake", retriable=True)


class SuccessBackend(AgentBackend):
    async def execute(
        self,
        system_prompt: str,
        user_prompt: str,
        context: dict[str, Any],
        tools: list[str] | None = None,
    ) -> AsyncIterator[str]:
        _ = system_prompt, user_prompt, context, tools
        yield "ok"

    async def execute_with_tools(
        self,
        system_prompt: str,
        user_prompt: str,
        allowed_tools: list[str],
    ) -> dict[str, Any]:
        _ = system_prompt, user_prompt, allowed_tools
        return {"content": "ok"}


def test_codex_build_command_shape() -> None:
    backend = CodexBackend(binary="codex", working_directory=Path("."))
    command = backend.build_command(
        system_prompt="system",
        user_prompt="implement feature",
        context={"goal": "x"},
        tools=["read", "write"],
    )

    assert command[0:2] == ["codex", "exec"]
    assert "--output-format" in command
    assert "jsonl" in command
    assert any(part.startswith("system_prompt=") for part in command)
    assert command[-1] == "implement feature"


def test_claude_build_command_shape() -> None:
    backend = ClaudeCodeBackend(binary="claude", working_directory=Path("."))
    command = backend.build_command("implement feature")

    assert command[0:2] == ["claude", "-p"]
    assert "--output-format" in command
    assert "stream-json" in command


def test_resilient_backend_fallback_and_retry_events() -> None:
    events: list[dict[str, Any]] = []

    backend = ResilientBackend(
        primary_name="primary",
        primary_backend=AlwaysFailBackend(),
        fallback_name="fallback",
        fallback_backend=SuccessBackend(),
        retry_policy=RetryPolicy(max_retries=1, backoff_seconds=0.0, timeout_seconds=5.0),
        event_hook=events.append,
    )

    async def _run() -> str:
        parts: list[str] = []
        async for part in backend.execute("system", "user", context={}):
            parts.append(part)
        return "".join(parts)

    output = asyncio.run(_run())

    assert output == "ok"
    event_names = [event["event"] for event in events]
    assert "backend_retry" in event_names
    assert "backend_fallback_success" in event_names


def test_resilient_backend_execute_with_tools_fallback() -> None:
    events: list[dict[str, Any]] = []
    backend = ResilientBackend(
        primary_name="primary",
        primary_backend=AlwaysFailBackend(),
        fallback_name="fallback",
        fallback_backend=SuccessBackend(),
        retry_policy=RetryPolicy(max_retries=1, backoff_seconds=0.0, timeout_seconds=5.0),
        event_hook=events.append,
    )

    payload = asyncio.run(
        backend.execute_with_tools(
            system_prompt="system",
            user_prompt="user",
            allowed_tools=["read_file"],
        )
    )

    assert payload["content"] == "ok"
    event_names = [event["event"] for event in events]
    assert "backend_retry" in event_names
    assert "backend_fallback_success" in event_names
