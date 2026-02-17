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
from architect.supervisor import Supervisor, WorkTask


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
        if "Review the following plan" in user_prompt:
            yield "MINOR: Plan quality acceptable"
            return
        if "Review quality/security" in user_prompt or "Review implementation chunk" in user_prompt:
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


class BlockingPlanCriticBackend(FakeBackend):
    async def execute(
        self,
        system_prompt: str,
        user_prompt: str,
        context: dict[str, Any],
        tools: list[str] | None = None,
    ) -> AsyncIterator[str]:
        if "Review the following plan" in user_prompt:
            yield "BLOCKER: Missing interface definitions"
            return
        async for chunk in super().execute(system_prompt, user_prompt, context, tools):
            yield chunk


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


def _build_supervisor(
    repo: Path,
    config: ArchitectConfig,
    backend: AgentBackend | None = None,
) -> Supervisor:
    runtime_backend = backend or FakeBackend()
    state = GitNotesStore(repo)
    patches = PatchStackManager(repo, state_store=state)
    return Supervisor(
        state_store=state,
        patch_manager=patches,
        specialists={
            "planner": PlannerAgent(runtime_backend),
            "coder": CoderAgent(runtime_backend),
            "tester": TesterAgent(runtime_backend),
            "critic": CriticAgent(runtime_backend),
            "documenter": DocumenterAgent(runtime_backend),
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
    tracked_fallback = subprocess.run(
        ["git", "ls-files", "docs/architect-runs"],
        cwd=repo,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()
    assert tracked_fallback == ""


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


def test_guardrail_require_tests_for_accepts_spec_style_tests(tmp_path: Path) -> None:
    config = ArchitectConfig.default()
    config.guardrails.require_tests_for = ["packages/**/*.ts"]
    supervisor = _build_supervisor(tmp_path, config)

    passed, _reason = supervisor._assert_guardrail_test_coverage(  # noqa: SLF001
        [
            "packages/core/index.ts",
            "spec/core/index.spec.ts",
        ]
    )

    assert passed is True


def test_supervisor_can_skip_critic_when_not_required(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    config = ArchitectConfig.default()
    config.workflow.require_critic_approval = False
    config.workflow.max_patches_before_review = 1
    config.project.lint_command = "python -c \"print('lint ok')\""
    config.project.type_check_command = "python -c \"print('type ok')\""
    config.project.test_command = "python -c \"print('test ok')\""

    supervisor = _build_supervisor(repo, config)
    summary = asyncio.run(supervisor.run("Build JWT auth"))
    status = supervisor.status(verbose=True)

    assert summary.completed_tasks >= 4
    assert not any(task["type"] == "review" for task in status["tasks"])


def test_supervisor_records_runs_and_leases(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    config = ArchitectConfig.default()
    config.project.lint_command = "python -c \"print('lint ok')\""
    config.project.type_check_command = "python -c \"print('type ok')\""
    config.project.test_command = "python -c \"print('test ok')\""
    supervisor = _build_supervisor(repo, config)

    summary = asyncio.run(supervisor.run("Build JWT auth"))
    payload = supervisor.status(verbose=True)

    assert summary.run_id in payload["runs"]
    assert payload["runs"][summary.run_id]["status"] == "complete"
    assert "active" in payload["leases"]


def test_plan_gate_can_enforce_critic_blockers(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    config = ArchitectConfig.default()
    config.workflow.plan_requires_critic = True
    config.project.lint_command = "python -c \"print('lint ok')\""
    config.project.type_check_command = "python -c \"print('type ok')\""
    config.project.test_command = "python -c \"print('test ok')\""
    supervisor = _build_supervisor(repo, config, backend=BlockingPlanCriticBackend())

    with pytest.raises(RuntimeError, match="Planning gate failed due to critic blockers"):
        asyncio.run(supervisor.run("Build JWT auth"))


def test_path_classifiers_detect_docs_and_tests(tmp_path: Path) -> None:
    supervisor = _build_supervisor(tmp_path, ArchitectConfig.default())

    assert supervisor._is_test_path("packages/api/user.test.ts")  # noqa: SLF001
    assert supervisor._is_test_path("spec/auth/login.spec.ts")  # noqa: SLF001
    assert supervisor._is_test_path("tests/test_login.py")  # noqa: SLF001
    assert supervisor._is_documentation_path("README.md")  # noqa: SLF001
    assert supervisor._is_documentation_path("docs/api.md")  # noqa: SLF001
    assert supervisor._is_internal_runtime_path(".architect/runs/run/task.md")  # noqa: SLF001
    assert not supervisor._is_documentation_path("src/auth/service.py")  # noqa: SLF001
    assert supervisor._is_documentation_evidence_path("guides/overview.txt") is False  # noqa: SLF001


def test_documentation_evidence_uses_configured_patterns(tmp_path: Path) -> None:
    config = ArchitectConfig.default()
    config.workflow.review_docs_patterns = ["guides/**"]
    supervisor = _build_supervisor(tmp_path, config)

    assert supervisor._is_documentation_evidence_path("guides/overview.txt")  # noqa: SLF001


def test_supervisor_parses_structured_review_findings(tmp_path: Path) -> None:
    supervisor = _build_supervisor(tmp_path, ArchitectConfig.default())
    findings = supervisor._parse_review_findings('{"counts":{"BLOCKER":1,"MAJOR":2}}')  # noqa: SLF001

    assert findings["BLOCKER"] == 1
    assert findings["MAJOR"] == 2
    assert findings["MINOR"] == 0


def test_supervisor_parses_structured_coverage_payload(tmp_path: Path) -> None:
    supervisor = _build_supervisor(tmp_path, ArchitectConfig.default())
    percent = supervisor._extract_coverage_percent(  # noqa: SLF001
        {"stdout_tail": '{"coverage_percent":87}', "stderr_tail": ""}
    )

    assert percent == 87


def test_supervisor_preflight_fails_when_required_command_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = ArchitectConfig.default()
    config.project.lint_command = "python -c \"print('lint ok')\""
    config.project.type_check_command = "python -c \"print('type ok')\""
    config.project.test_command = "python -c \"print('test ok')\""
    supervisor = _build_supervisor(tmp_path, config)
    monkeypatch.setattr(
        supervisor,
        "_command_available",
        lambda executable: executable != "python",
    )

    with pytest.raises(RuntimeError, match="Runtime preflight failed"):
        asyncio.run(supervisor.run("Build JWT auth"))

    status = supervisor.status(verbose=True)
    preflight = status["metrics"]["preflight"]
    assert preflight["ok"] is False
    assert any("python" in message for message in preflight["errors"])
    assert status["context"]["preflight"]["ok"] is False


def test_supervisor_preflight_records_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = ArchitectConfig.default()
    config.project.lint_command = "python -c \"print('lint ok')\""
    config.project.type_check_command = "python -c \"print('type ok')\""
    config.project.test_command = "python -c \"print('test ok')\""
    supervisor = _build_supervisor(tmp_path, config)
    monkeypatch.setattr(supervisor, "_command_available", lambda executable: True)

    summary = asyncio.run(supervisor.run("Build JWT auth"))
    status = supervisor.status(verbose=True)

    assert summary.completed_tasks >= 5
    assert status["metrics"]["preflight"]["ok"] is True
    assert status["context"]["preflight"]["ok"] is True


def test_supervisor_preflight_warns_when_fallback_is_same_backend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = ArchitectConfig.default()
    config.backend.primary = "codex"
    config.backend.fallback = "codex"
    config.project.lint_command = "python -c \"print('lint ok')\""
    config.project.type_check_command = "python -c \"print('type ok')\""
    config.project.test_command = "python -c \"print('test ok')\""
    supervisor = _build_supervisor(tmp_path, config)
    monkeypatch.setattr(supervisor, "_command_available", lambda executable: True)

    asyncio.run(supervisor.run("Build JWT auth"))
    status = supervisor.status(verbose=True)

    warnings = status["metrics"]["preflight"]["warnings"]
    assert any("failover is effectively disabled" in message for message in warnings)


def test_supervisor_fails_with_dirty_worktree_by_default(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    (repo / "seed.txt").write_text("dirty\n", encoding="utf-8")
    supervisor = _build_supervisor(repo, ArchitectConfig.default())

    with pytest.raises(RuntimeError, match="dirty worktree"):
        asyncio.run(supervisor.run("Build JWT auth"))


def test_supervisor_can_isolate_dirty_worktree(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    (repo / "seed.txt").write_text("dirty\n", encoding="utf-8")

    config = ArchitectConfig.default()
    config.workflow.dirty_worktree_mode = "isolate"
    config.project.lint_command = "python -c \"print('lint ok')\""
    config.project.type_check_command = "python -c \"print('type ok')\""
    config.project.test_command = "python -c \"print('test ok')\""
    supervisor = _build_supervisor(repo, config)

    summary = asyncio.run(supervisor.run("Build JWT auth"))
    assert summary.completed_tasks >= 5
    preflight = supervisor.status(verbose=True)["metrics"]["preflight"]
    assert any("Dirty worktree isolation enabled" in item for item in preflight["warnings"])

    unstaged = subprocess.run(
        ["git", "diff", "--name-only"],
        cwd=repo,
        check=True,
        text=True,
        capture_output=True,
    ).stdout
    assert "seed.txt" in unstaged


def test_supervisor_rejects_unknown_task_tools(tmp_path: Path) -> None:
    supervisor = _build_supervisor(tmp_path, ArchitectConfig.default())
    task = WorkTask(
        id="task-unknown-tools",
        type="implement",
        assigned_to="coder",
        description="x",
        allowed_tools=["read_file", "drop_database"],
    )

    with pytest.raises(RuntimeError, match="Tool policy rejected unknown tools"):
        supervisor._allowed_tools_for_task(task)  # noqa: SLF001


def test_run_command_prefers_exec_mode_for_simple_commands(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor = _build_supervisor(tmp_path, ArchitectConfig.default())
    observed: dict[str, Any] = {}

    def fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        observed["payload"] = args[0]
        observed["shell"] = kwargs.get("shell")
        return subprocess.CompletedProcess(args=args[0], returncode=0, stdout="ok\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = supervisor._run_command("python -c \"print('ok')\"")  # noqa: SLF001

    assert result["exit_code"] == 0
    assert result["used_shell"] is False
    assert observed["shell"] is False
    assert isinstance(observed["payload"], list)


def test_run_command_uses_shell_for_shell_operators(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor = _build_supervisor(tmp_path, ArchitectConfig.default())
    observed: dict[str, Any] = {}

    def fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        observed["payload"] = args[0]
        observed["shell"] = kwargs.get("shell")
        return subprocess.CompletedProcess(args=args[0], returncode=0, stdout="ok\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = supervisor._run_command("python -c \"print('ok')\" | cat")  # noqa: SLF001

    assert result["exit_code"] == 0
    assert result["used_shell"] is True
    assert observed["shell"] is True
    assert isinstance(observed["payload"], str)
