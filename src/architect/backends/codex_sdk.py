from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from architect.backends.base import AgentBackend, BackendExecutionError
from architect.backends.codex import CodexBackend


class CodexSDKBackend(AgentBackend):
    """Optional SDK backend with automatic fallback to Codex CLI."""

    def __init__(
        self,
        *,
        model: str = "gpt-5-codex",
        working_directory: Path | None = None,
    ) -> None:
        self.model = model
        self.working_directory = working_directory
        self.cli_fallback = CodexBackend(working_directory=working_directory)
        self._client: Any | None = None
        try:
            from openai import OpenAI  # type: ignore

            self._client = OpenAI()
        except Exception:
            self._client = None

    def _build_user_input(
        self,
        user_prompt: str,
        context: dict[str, Any],
        tools: list[str] | None,
    ) -> str:
        parts = [user_prompt]
        if context:
            parts.append("Context JSON:")
            parts.append(json.dumps(context, ensure_ascii=False, indent=2))
        if tools:
            parts.append("Allowed tools:")
            parts.append(json.dumps(tools, ensure_ascii=False))
        return "\n\n".join(parts)

    @staticmethod
    def _extract_text(payload: Any) -> str:
        if payload is None:
            return ""
        output_text = getattr(payload, "output_text", None)
        if isinstance(output_text, str):
            return output_text
        if isinstance(payload, dict):
            value = payload.get("output_text")
            if isinstance(value, str):
                return value
        return str(output_text or "")

    async def execute(
        self,
        system_prompt: str,
        user_prompt: str,
        context: dict[str, Any],
        tools: list[str] | None = None,
    ) -> AsyncIterator[str]:
        requested_model = context.get("model")
        model_name = (
            requested_model.strip()
            if isinstance(requested_model, str) and requested_model.strip()
            else self.model
        )
        if self._client is None:
            async for chunk in self.cli_fallback.execute(
                system_prompt,
                user_prompt,
                context,
                tools,
            ):
                yield chunk
            return

        prompt = self._build_user_input(user_prompt, context, tools)

        def _request() -> Any:
            return self._client.responses.create(
                model=model_name,
                input=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt},
                ],
            )

        try:
            payload = await asyncio.to_thread(_request)
        except Exception as exc:
            raise BackendExecutionError(
                f"Codex SDK execution failed: {exc}",
                backend="codex_sdk",
                retriable=True,
            ) from exc

        content = self._extract_text(payload).strip()
        if content:
            yield content

    async def execute_with_tools(
        self,
        system_prompt: str,
        user_prompt: str,
        allowed_tools: list[str],
    ) -> dict[str, Any]:
        chunks: list[str] = []
        async for chunk in self.execute(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            context={"tool_mode": True},
            tools=allowed_tools,
        ):
            chunks.append(chunk)
        return {
            "backend": "codex_sdk" if self._client is not None else "codex",
            "content": "".join(chunks).strip(),
            "allowed_tools": allowed_tools,
        }
