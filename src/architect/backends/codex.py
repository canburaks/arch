from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from architect.backends.base import AgentBackend, BackendExecutionError, BackendProcessError


class CodexBackend(AgentBackend):
    def __init__(self, binary: str = "codex", working_directory: Path | None = None) -> None:
        self.binary = binary
        self.working_directory = working_directory

    def build_command(
        self,
        system_prompt: str,
        user_prompt: str,
        context: dict[str, Any],
        tools: list[str] | None = None,
    ) -> list[str]:
        command = [
            self.binary,
            "exec",
            "--output-format",
            "jsonl",
            "-c",
            f"system_prompt={json.dumps(system_prompt, ensure_ascii=False)}",
        ]
        if context:
            command.extend(["-c", f"context={json.dumps(context, ensure_ascii=False)}"])
        if tools:
            command.extend(["-c", f"allowed_tools={json.dumps(tools, ensure_ascii=False)}"])
        command.append(user_prompt)
        return command

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

        message = event.get("message")
        if isinstance(message, str):
            return message
        if isinstance(message, dict):
            msg_content = message.get("content")
            if isinstance(msg_content, str):
                return msg_content

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
        command = self.build_command(system_prompt, user_prompt, context, tools)
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(self.working_directory) if self.working_directory else None,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise BackendProcessError(
                f"Codex binary not found: {self.binary}",
                backend="codex",
                retriable=False,
            ) from exc

        if process.stdout is None:
            raise BackendProcessError(
                "Codex backend did not expose stdout.", backend="codex", retriable=False
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
            stderr_output = (await process.stderr.read()).decode("utf-8", errors="replace").strip()
        if return_code != 0:
            raise BackendExecutionError(
                f"Codex backend failed with exit code {return_code}: {stderr_output}",
                backend="codex",
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
            "backend": "codex",
            "content": "".join(chunks).strip(),
            "allowed_tools": allowed_tools,
        }
