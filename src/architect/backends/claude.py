from __future__ import annotations

import asyncio
import json
import os
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from architect.backends.base import AgentBackend, BackendExecutionError, BackendProcessError


class ClaudeCodeBackend(AgentBackend):
    def __init__(self, binary: str = "claude", working_directory: Path | None = None) -> None:
        self.binary = binary
        self.working_directory = working_directory

    def build_command(self, user_prompt: str) -> list[str]:
        return [self.binary, "-p", user_prompt, "--output-format", "stream-json"]

    @staticmethod
    def _extract_content(event: dict[str, Any]) -> str:
        content = event.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, dict):
                    text = item.get("text")
                    if isinstance(text, str):
                        parts.append(text)
            return "".join(parts)
        delta = event.get("delta")
        if isinstance(delta, str):
            return delta
        return ""

    @staticmethod
    def _appears_partial_json(raw: str) -> bool:
        return raw.count("{") > raw.count("}") or raw.count("[") > raw.count("]")

    async def execute(
        self,
        system_prompt: str,
        user_prompt: str,
        context: dict[str, Any],
        tools: list[str] | None = None,
    ) -> AsyncIterator[str]:
        if context:
            user_prompt = (
                f"{user_prompt}\n\nContext JSON:\n"
                f"{json.dumps(context, ensure_ascii=False, indent=2)}"
            )
        if tools:
            user_prompt = (
                f"{user_prompt}\n\nAllowed tools:\n{json.dumps(tools, ensure_ascii=False)}"
            )

        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", encoding="utf-8") as temp_file:
            temp_file.write(system_prompt)
            temp_file.flush()

            env = os.environ.copy()
            env["CLAUDE_MD"] = temp_file.name

            command = self.build_command(user_prompt)
            try:
                process = await asyncio.create_subprocess_exec(
                    *command,
                    cwd=str(self.working_directory) if self.working_directory else None,
                    env=env,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            except FileNotFoundError as exc:
                raise BackendProcessError(
                    f"Claude binary not found: {self.binary}",
                    backend="claude",
                    retriable=False,
                ) from exc

            if process.stdout is None:
                raise BackendProcessError(
                    "Claude backend did not expose stdout.", backend="claude", retriable=False
                )

            parse_buffer = ""
            async for raw_line in process.stdout:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                candidate = f"{parse_buffer}{line}" if parse_buffer else line
                try:
                    event = json.loads(candidate)
                    parse_buffer = ""
                except json.JSONDecodeError:
                    if self._appears_partial_json(candidate):
                        parse_buffer = candidate
                        continue
                    parse_buffer = ""
                    yield line
                    continue

                content = self._extract_content(event)
                if content:
                    yield content

            if parse_buffer:
                yield parse_buffer

            return_code = await process.wait()
            stderr_output = ""
            if process.stderr is not None:
                stderr_output = (
                    (await process.stderr.read()).decode("utf-8", errors="replace").strip()
                )
            if return_code != 0:
                raise BackendExecutionError(
                    f"Claude backend failed with exit code {return_code}: {stderr_output}",
                    backend="claude",
                    exit_code=return_code,
                    retriable=True,
                )

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
            "backend": "claude",
            "content": "".join(chunks).strip(),
            "allowed_tools": allowed_tools,
        }
