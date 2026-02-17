from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

from architect.backends.base import AgentBackend, BackendExecutionError, BackendProcessError


class CodexBackend(AgentBackend):
    def __init__(
        self,
        binary: str = "codex",
        working_directory: Path | None = None,
        event_hook: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.binary = binary
        self.working_directory = working_directory
        self.event_hook = event_hook

    def _emit(self, payload: dict[str, Any]) -> None:
        if self.event_hook is not None:
            self.event_hook(payload)

    def build_command(
        self,
        system_prompt: str,
        user_prompt: str,
        context: dict[str, Any],
        tools: list[str] | None = None,
    ) -> list[str]:
        rendered_prompt = self._build_user_prompt(user_prompt, context, tools)
        requested_model = context.get("model")
        command = [
            self.binary,
            "exec",
            "--json",
            "-c",
            f"instructions={json.dumps(system_prompt, ensure_ascii=False)}",
        ]
        if isinstance(requested_model, str) and requested_model.strip():
            command.extend(["-m", requested_model.strip()])
        command.append(rendered_prompt)
        return command

    @staticmethod
    def _build_user_prompt(
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
        cwd_override = context.get("_working_directory")
        if isinstance(cwd_override, str) and cwd_override.strip():
            cwd = cwd_override
        else:
            cwd = str(self.working_directory) if self.working_directory else None
        self._emit(
            {
                "event": "codex_cli_start",
                "command": command[:4],
                "has_context": bool(context),
                "tool_mode": bool(tools),
                "model": context.get("model"),
            }
        )
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=cwd,
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
                    self._emit({"event": "codex_json_partial", "bytes": len(candidate)})
                    continue
                parse_buffer = ""
                self._emit({"event": "codex_json_parse_fallback", "line": line[:200]})
                continue

            content = self._extract_content(event)
            self._emit(
                {
                    "event": "codex_json_event",
                    "type": str(event.get("type", "")),
                    "has_content": bool(content),
                }
            )
            if content:
                yield content

        if parse_buffer:
            self._emit({"event": "codex_json_buffer_flush", "bytes": len(parse_buffer)})

        return_code = await process.wait()
        stderr_output = ""
        if process.stderr is not None:
            stderr_output = (await process.stderr.read()).decode("utf-8", errors="replace").strip()
        if return_code != 0:
            self._emit(
                {
                    "event": "codex_cli_exit",
                    "exit_code": return_code,
                    "stderr": stderr_output[:400],
                }
            )
            raise BackendExecutionError(
                f"Codex backend failed with exit code {return_code}: {stderr_output}",
                backend="codex",
                exit_code=return_code,
                retriable=True,
            )
        self._emit({"event": "codex_cli_exit", "exit_code": 0})

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
