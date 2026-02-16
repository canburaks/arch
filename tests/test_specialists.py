import asyncio
from collections.abc import AsyncIterator
from typing import Any

from architect.backends.base import AgentBackend
from architect.specialists import PlannerAgent


class FakeBackend(AgentBackend):
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
        return {"content": "ok"}


def test_planner_specialist_runs() -> None:
    planner = PlannerAgent(FakeBackend())
    response = asyncio.run(planner.run("Design auth", {"goal": "auth"}))

    assert response.role == "planner"
    assert "planned: Design auth" in response.content
