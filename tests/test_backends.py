import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from architect.backends import RetryPolicy
from architect.backends.base import AgentBackend, BackendExecutionError
from architect.backends.claude import ClaudeCodeBackend
from architect.backends.codex import CodexBackend
from architect.backends.codex_sdk import CodexSDKBackend
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
        context={"goal": "x", "model": "gpt-5-codex"},
        tools=["read", "write"],
    )

    assert command[0:2] == ["codex", "exec"]
    assert "--json" in command
    assert "--output-format" not in command
    assert "-m" in command
    assert "gpt-5-codex" in command
    assert any(part.startswith("instructions=") for part in command)
    assert "implement feature" in command[-1]
    assert "Context JSON:" in command[-1]
    assert "Allowed tools:" in command[-1]


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
    assert "backend_failover_start" in event_names
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


def test_codex_backend_emits_stream_telemetry(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[dict[str, Any]] = []

    class FakeStdout:
        def __init__(self, lines: list[bytes]) -> None:
            self._lines = lines
            self._index = 0

        def __aiter__(self) -> "FakeStdout":
            return self

        async def __anext__(self) -> bytes:
            if self._index >= len(self._lines):
                raise StopAsyncIteration
            line = self._lines[self._index]
            self._index += 1
            return line

    class FakeStderr:
        async def read(self) -> bytes:
            return b""

    class FakeProcess:
        def __init__(self) -> None:
            self.stdout = FakeStdout(
                [
                    b"{\"type\":\"response.output_text.delta\",\"content\":\"hello\"}\n",
                    b"noise-before-json\n",
                    b"{\"type\":\"response.completed\"}\n",
                ]
            )
            self.stderr = FakeStderr()

        async def wait(self) -> int:
            return 0

    async def fake_create_subprocess_exec(*args: Any, **kwargs: Any) -> FakeProcess:
        _ = args, kwargs
        return FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    backend = CodexBackend(event_hook=events.append)

    async def _run() -> str:
        chunks: list[str] = []
        async for chunk in backend.execute("system", "user", context={}):
            chunks.append(chunk)
        return "".join(chunks)

    output = asyncio.run(_run())

    assert output == "hello"
    event_names = [event.get("event") for event in events]
    assert "codex_cli_start" in event_names
    assert "codex_json_event" in event_names
    assert "codex_json_parse_fallback" in event_names
    assert "codex_cli_exit" in event_names


def test_codex_sdk_uses_context_model(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    class FakeResponses:
        def create(self, **kwargs: Any) -> dict[str, Any]:
            captured.update(kwargs)
            return {"output_text": "ok"}

    class FakeClient:
        def __init__(self) -> None:
            self.responses = FakeResponses()

    backend = CodexSDKBackend(model="gpt-5-codex")
    monkeypatch.setattr(backend, "_client", FakeClient())

    async def _run() -> str:
        chunks: list[str] = []
        async for chunk in backend.execute(
            system_prompt="system",
            user_prompt="user",
            context={"model": "gpt-5.3-codex"},
        ):
            chunks.append(chunk)
        return "".join(chunks)

    output = asyncio.run(_run())

    assert output == "ok"
    assert captured["model"] == "gpt-5.3-codex"
