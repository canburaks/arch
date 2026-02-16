from __future__ import annotations

from dataclasses import dataclass, field
from importlib import resources
from typing import Any

from architect.backends.base import AgentBackend


@dataclass(slots=True)
class SpecialistResponse:
    role: str
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)


class SpecialistAgent:
    role: str = "specialist"
    prompt_file: str | None = None
    fallback_prompt: str = "You are a software specialist."

    def __init__(self, backend: AgentBackend, *, model: str | None = None) -> None:
        self.backend = backend
        self.model = model
        self.system_prompt = self._load_system_prompt()

    def _load_system_prompt(self) -> str:
        if not self.prompt_file:
            return self.fallback_prompt.strip()
        try:
            prompt_path = resources.files("architect.prompts").joinpath(self.prompt_file)
            return prompt_path.read_text(encoding="utf-8").strip()
        except (FileNotFoundError, ModuleNotFoundError):
            return self.fallback_prompt.strip()

    async def run(
        self,
        instruction: str,
        context: dict[str, Any],
        allowed_tools: list[str] | None = None,
    ) -> SpecialistResponse:
        run_context = dict(context)
        if self.model:
            run_context["model"] = self.model

        if allowed_tools:
            payload = await self.backend.execute_with_tools(
                system_prompt=self.system_prompt,
                user_prompt=instruction,
                allowed_tools=allowed_tools,
            )
            content = str(payload.get("content", "")).strip()
            return SpecialistResponse(
                role=self.role,
                content=content,
                metadata={
                    "instruction": instruction,
                    "tool_mode": True,
                    "allowed_tools": list(allowed_tools),
                    "payload": payload,
                },
            )

        chunks: list[str] = []
        async for chunk in self.backend.execute(
            system_prompt=self.system_prompt,
            user_prompt=instruction,
            context=run_context,
        ):
            chunks.append(chunk)
        return SpecialistResponse(
            role=self.role,
            content="".join(chunks).strip(),
            metadata={"instruction": instruction, "tool_mode": False},
        )
