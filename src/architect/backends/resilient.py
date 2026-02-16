from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from architect.backends.base import AgentBackend, BackendExecutionError, BackendTimeoutError

BackendEventHook = Callable[[dict[str, Any]], None]


@dataclass(slots=True)
class RetryPolicy:
    max_retries: int = 1
    backoff_seconds: float = 0.5
    timeout_seconds: float = 90.0


class ResilientBackend(AgentBackend):
    """Wraps primary/fallback backends with timeout, retry, and failover."""

    def __init__(
        self,
        primary_name: str,
        primary_backend: AgentBackend,
        fallback_name: str,
        fallback_backend: AgentBackend,
        retry_policy: RetryPolicy,
        event_hook: BackendEventHook | None = None,
    ) -> None:
        self.primary_name = primary_name
        self.primary_backend = primary_backend
        self.fallback_name = fallback_name
        self.fallback_backend = fallback_backend
        self.retry_policy = retry_policy
        self.event_hook = event_hook

    def _emit(self, event: dict[str, Any]) -> None:
        if self.event_hook:
            self.event_hook(event)

    async def _collect_chunks(
        self,
        backend: AgentBackend,
        *,
        system_prompt: str,
        user_prompt: str,
        context: dict[str, Any],
        tools: list[str] | None,
    ) -> list[str]:
        async def _consume() -> list[str]:
            chunks: list[str] = []
            async for chunk in backend.execute(system_prompt, user_prompt, context, tools):
                chunks.append(chunk)
            return chunks

        try:
            return await asyncio.wait_for(_consume(), timeout=self.retry_policy.timeout_seconds)
        except TimeoutError as exc:
            raise BackendTimeoutError(
                f"Backend request timed out after {self.retry_policy.timeout_seconds:.1f}s",
                retriable=True,
            ) from exc

    async def _execute_attempts(
        self,
        call_name: str,
        call: Callable[[AgentBackend], Awaitable[list[str]]],
    ) -> list[str]:
        attempts: list[tuple[str, AgentBackend]] = [(self.primary_name, self.primary_backend)]
        if self.fallback_name != self.primary_name:
            attempts.append((self.fallback_name, self.fallback_backend))

        errors: list[str] = []
        for backend_name, backend in attempts:
            for attempt in range(self.retry_policy.max_retries + 1):
                is_retry = attempt > 0
                if is_retry:
                    delay = self.retry_policy.backoff_seconds * (2 ** (attempt - 1))
                    self._emit(
                        {
                            "event": "backend_retry",
                            "backend": backend_name,
                            "attempt": attempt,
                            "delay_seconds": delay,
                            "call": call_name,
                        }
                    )
                    await asyncio.sleep(delay)
                try:
                    chunks = await call(backend)
                    if backend_name != self.primary_name:
                        self._emit(
                            {
                                "event": "backend_fallback_success",
                                "backend": backend_name,
                                "attempt": attempt,
                                "call": call_name,
                            }
                        )
                    return chunks
                except BackendExecutionError as exc:
                    errors.append(f"{backend_name}[{attempt}]: {exc}")
                    self._emit(
                        {
                            "event": "backend_attempt_failed",
                            "backend": backend_name,
                            "attempt": attempt,
                            "call": call_name,
                            "error": str(exc),
                            "retriable": exc.retriable,
                        }
                    )
                    if not exc.retriable:
                        break
                except Exception as exc:
                    errors.append(f"{backend_name}[{attempt}]: {exc}")
                    self._emit(
                        {
                            "event": "backend_attempt_failed",
                            "backend": backend_name,
                            "attempt": attempt,
                            "call": call_name,
                            "error": str(exc),
                            "retriable": True,
                        }
                    )

        summary = "; ".join(errors[-6:])
        raise BackendExecutionError(
            f"All backend attempts failed for {call_name}. {summary}",
            retriable=False,
        )

    async def execute(
        self,
        system_prompt: str,
        user_prompt: str,
        context: dict[str, Any],
        tools: list[str] | None = None,
    ) -> AsyncIterator[str]:
        chunks = await self._execute_attempts(
            "execute",
            lambda backend: self._collect_chunks(
                backend,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                context=context,
                tools=tools,
            ),
        )
        for chunk in chunks:
            yield chunk

    async def execute_with_tools(
        self,
        system_prompt: str,
        user_prompt: str,
        allowed_tools: list[str],
    ) -> dict[str, Any]:
        chunks = await self._execute_attempts(
            "execute_with_tools",
            lambda backend: self._collect_chunks(
                backend,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                context={"tool_mode": True},
                tools=allowed_tools,
            ),
        )
        return {
            "backend": "resilient",
            "content": "".join(chunks).strip(),
            "allowed_tools": allowed_tools,
        }
