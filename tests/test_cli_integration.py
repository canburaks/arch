import re
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from click.testing import CliRunner

from architect.backends.base import AgentBackend
from architect.cli import cli
from architect.config import load_config, save_config


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
            yield "- Create implementation artifact\n- Validate behavior"
            return
        if "Review quality/security" in user_prompt:
            yield "MINOR: All checks passed"
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
    (repo_path / "README.md").write_text("seed\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "README.md"], cwd=repo_path, check=True, text=True, capture_output=True
    )
    subprocess.run(
        ["git", "commit", "-m", "seed"],
        cwd=repo_path,
        check=True,
        text=True,
        capture_output=True,
    )


def _set_safe_commands(config_path: Path) -> None:
    config = load_config(config_path)
    config.project.lint_command = "python -c \"print('lint ok')\""
    config.project.type_check_command = "python -c \"print('type ok')\""
    config.project.test_command = "python -c \"print('test ok')\""
    save_config(config_path, config)


def _extract_patch_id(review_output: str) -> str:
    for line in review_output.splitlines():
        if "architect: task-implement" in line:
            return line.split()[0]
    raise AssertionError("No architect implementation patch found in review output")


def _extract_checkpoint_id(checkpoints_output: str) -> str:
    lines = [line.strip() for line in checkpoints_output.splitlines() if line.strip()]
    assert lines
    return lines[-1]


def test_cli_full_lifecycle_commands(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    monkeypatch.chdir(repo)
    monkeypatch.setattr(
        "architect.cli._build_backend", lambda config, repo_root, state: FakeBackend()
    )

    runner = CliRunner()

    init_result = runner.invoke(cli, ["init"])
    assert init_result.exit_code == 0

    _set_safe_commands(repo / "architect.toml")

    run_result = runner.invoke(cli, ["run", "Implement workflow"])
    assert run_result.exit_code == 0
    assert "Run ID:" in run_result.output

    status_result = runner.invoke(cli, ["status"])
    assert status_result.exit_code == 0
    assert '"status": "complete"' in status_result.output

    review_result = runner.invoke(cli, ["review"])
    assert review_result.exit_code == 0
    patch_id = _extract_patch_id(review_result.output)

    review_patch = runner.invoke(cli, ["review", "--patch", patch_id])
    assert review_patch.exit_code == 0
    assert '"patch"' in review_patch.output

    accept_result = runner.invoke(cli, ["accept", patch_id])
    assert accept_result.exit_code == 0

    modify_result = runner.invoke(cli, ["modify", patch_id])
    assert modify_result.exit_code == 0
    assert "Amendment branch:" in modify_result.output

    second_run_result = runner.invoke(cli, ["run", "Implement second workflow"])
    assert second_run_result.exit_code == 0

    second_review_result = runner.invoke(cli, ["review"])
    assert second_review_result.exit_code == 0
    second_patch_id = _extract_patch_id(second_review_result.output)
    assert re.match(r"patch-[0-9a-f]{8}", second_patch_id)

    reject_result = runner.invoke(cli, ["reject", second_patch_id])
    assert reject_result.exit_code == 0

    checkpoints_result = runner.invoke(cli, ["checkpoints"])
    assert checkpoints_result.exit_code == 0
    checkpoint_id = _extract_checkpoint_id(checkpoints_result.output)

    rollback_result = runner.invoke(cli, ["rollback", checkpoint_id])
    assert rollback_result.exit_code == 0


def test_accept_rejects_forbidden_paths(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    (repo / ".env").write_text("SECRET=1\n", encoding="utf-8")
    subprocess.run(["git", "add", ".env"], cwd=repo, check=True, text=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "add secret"],
        cwd=repo,
        check=True,
        text=True,
        capture_output=True,
    )
    commit_hash = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()

    monkeypatch.chdir(repo)
    monkeypatch.setattr(
        "architect.cli._build_backend", lambda config, repo_root, state: FakeBackend()
    )
    runner = CliRunner()
    init_result = runner.invoke(cli, ["init"])
    assert init_result.exit_code == 0

    _set_safe_commands(repo / "architect.toml")

    accept_result = runner.invoke(cli, ["accept", commit_hash[:8]])
    assert accept_result.exit_code != 0
    assert "forbidden path" in accept_result.output.lower()
