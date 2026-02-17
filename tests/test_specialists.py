import asyncio
from collections.abc import AsyncIterator
from typing import Any

from architect.backends.base import AgentBackend
from architect.specialists import PlannerAgent


class FakeBackend(AgentBackend):
    def __init__(self) -> None:
        self.execute_calls = 0
        self.execute_with_tools_calls = 0
        self.last_context: dict[str, Any] | None = None
        self.last_tools: list[str] | None = None

    async def execute(
        self,
        system_prompt: str,
        user_prompt: str,
        context: dict[str, Any],
        tools: list[str] | None = None,
    ) -> AsyncIterator[str]:
        _ = system_prompt
        self.last_context = context
        self.last_tools = tools
        self.execute_calls += 1
        yield f"planned: {user_prompt}"

    async def execute_with_tools(
        self,
        system_prompt: str,
        user_prompt: str,
        allowed_tools: list[str],
    ) -> dict[str, Any]:
        _ = system_prompt
        _ = user_prompt
        _ = allowed_tools
        self.execute_with_tools_calls += 1
        return {"content": "ok"}


def test_planner_specialist_runs() -> None:
    backend = FakeBackend()
    planner = PlannerAgent(backend)
    response = asyncio.run(planner.run("Design auth", {"goal": "auth"}))

    assert response.role == "planner"
    assert "planned: Design auth" in response.content
    assert backend.execute_calls == 1
    assert backend.execute_with_tools_calls == 0


def test_specialist_can_use_execute_with_tools() -> None:
    backend = FakeBackend()
    planner = PlannerAgent(backend, model="gpt-5-codex")
    response = asyncio.run(
        planner.run(
            "Design auth with tools",
            {"goal": "auth", "task": {"id": "task-1"}},
            allowed_tools=["read_file", "write_file"],
        )
    )

    assert response.role == "planner"
    assert response.content == "planned: Design auth with tools"
    assert backend.execute_calls == 1
    assert backend.execute_with_tools_calls == 0
    assert backend.last_context is not None
    assert backend.last_context["goal"] == "auth"
    assert backend.last_context["model"] == "gpt-5-codex"
    assert backend.last_tools == ["read_file", "write_file"]
    assert response.metadata["tool_policy_enforced"] is True


def test_specialist_rejects_unknown_tools() -> None:
    backend = FakeBackend()
    planner = PlannerAgent(backend)

    try:
        asyncio.run(
            planner.run(
                "Design auth with tools",
                {"goal": "auth"},
                allowed_tools=["read_file", "drop_database"],
            )
        )
    except RuntimeError as exc:
        assert "Tool policy rejected unknown tools" in str(exc)
    else:
        raise AssertionError("Expected RuntimeError for unknown tool.")
