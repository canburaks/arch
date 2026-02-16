import asyncio
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from architect.backends.base import AgentBackend
from architect.config import ArchitectConfig
from architect.specialists import (
    CoderAgent,
    CriticAgent,
    DocumenterAgent,
    PlannerAgent,
    TesterAgent,
)
from architect.state import GitNotesStore, PatchStackManager
from architect.supervisor import Supervisor


class FakeBackend(AgentBackend):
    async def execute(
        self,
        system_prompt: str,
        user_prompt: str,
        context: dict[str, Any],
        tools: list[str] | None = None,
    ) -> AsyncIterator[str]:
        _ = system_prompt, context, tools
        if "Design a technical approach" in user_prompt:
            yield "- Implement core flow\n- Add validation"
            return
        if "Review quality/security" in user_prompt:
            yield "MINOR: Naming could be improved"
            return
        yield f"done: {user_prompt}"

    async def execute_with_tools(
        self,
        system_prompt: str,
        user_prompt: str,
        allowed_tools: list[str],
    ) -> dict[str, Any]:
        _ = system_prompt, user_prompt, allowed_tools
        return {"content": "ok"}


def _init_git_repo(repo_path: Path) -> None:
    subprocess.run(["git", "init"], cwd=repo_path, check=True, text=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=repo_path,
        check=True,
        text=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=repo_path,
        check=True,
        text=True,
        capture_output=True,
    )
    (repo_path / "seed.txt").write_text("seed\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "seed.txt"], cwd=repo_path, check=True, text=True, capture_output=True
    )
    subprocess.run(
        ["git", "commit", "-m", "seed"],
        cwd=repo_path,
        check=True,
        text=True,
        capture_output=True,
    )


def _build_supervisor(repo: Path, config: ArchitectConfig) -> Supervisor:
    backend = FakeBackend()
    state = GitNotesStore(repo)
    patches = PatchStackManager(repo, state_store=state)
    return Supervisor(
        state_store=state,
        patch_manager=patches,
        specialists={
            "planner": PlannerAgent(backend),
            "coder": CoderAgent(backend),
            "tester": TesterAgent(backend),
            "critic": CriticAgent(backend),
            "documenter": DocumenterAgent(backend),
        },
        config=config,
        repo_root=repo,
    )


def test_supervisor_run_updates_state_and_creates_patches(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    config = ArchitectConfig.default()
    config.project.lint_command = "python -c \"print('lint ok')\""
    config.project.type_check_command = "python -c \"print('type ok')\""
    config.project.test_command = "python -c \"print('test ok')\""

    supervisor = _build_supervisor(repo, config)
    summary = asyncio.run(supervisor.run("Build JWT auth"))
    status = supervisor.status(verbose=True)

    assert summary.completed_tasks >= 5
    assert status["context"]["status"] == "complete"
    assert len(status["tasks"]) >= 5
    assert len(status["metrics"]["quality_gates"]) >= 5
    assert any(task["type"] == "implement" for task in status["tasks"])
    assert len(status["patches"]) >= 1


def test_supervisor_fails_when_test_gate_command_fails(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    config = ArchitectConfig.default()
    config.project.lint_command = "python -c \"print('lint ok')\""
    config.project.type_check_command = "python -c \"print('type ok')\""
    config.project.test_command = 'python -c "import sys; sys.exit(1)"'

    supervisor = _build_supervisor(repo, config)

    with pytest.raises(RuntimeError, match="Quality gate failed"):
        asyncio.run(supervisor.run("Build JWT auth"))

    status = supervisor.status(verbose=True)
    assert status["context"]["status"] == "failed"
    assert status["recent_gate_failures"]


def test_guardrail_require_tests_for_detects_missing_tests(tmp_path: Path) -> None:
    config = ArchitectConfig.default()
    supervisor = _build_supervisor(tmp_path, config)

    passed, _reason = supervisor._assert_guardrail_test_coverage(["src/architect/cli.py"])  # noqa: SLF001

    assert passed is False
