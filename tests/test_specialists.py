import asyncio
from collections.abc import AsyncIterator
from typing import Any

from architect.backends.base import AgentBackend
from architect.specialists import PlannerAgent


class FakeBackend(AgentBackend):
    def __init__(self) -> None:
        self.execute_calls = 0
        self.execute_with_tools_calls = 0

    async def execute(
        self,
        system_prompt: str,
        user_prompt: str,
        context: dict[str, Any],
        tools: list[str] | None = None,
    ) -> AsyncIterator[str]:
        _ = system_prompt
        _ = context
        _ = tools
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
    planner = PlannerAgent(backend)
    response = asyncio.run(
        planner.run(
            "Design auth with tools",
            {"goal": "auth"},
            allowed_tools=["read_file", "write_file"],
        )
    )

    assert response.role == "planner"
    assert response.content == "ok"
    assert backend.execute_calls == 0
    assert backend.execute_with_tools_calls == 1
