import re
import subprocess
from pathlib import Path

import pytest

from architect.state import GitNotesStore, PatchStackManager
from architect.state.git_notes import ArchitectStateError


def _run(cmd: list[str], cwd: Path) -> str:
    proc = subprocess.run(cmd, cwd=cwd, check=True, text=True, capture_output=True)
    return proc.stdout.strip()


def _init_repo_with_commits(repo: Path) -> None:
    _run(["git", "init"], cwd=repo)
    _run(["git", "config", "user.email", "test@example.com"], cwd=repo)
    _run(["git", "config", "user.name", "Test User"], cwd=repo)

    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    _run(["git", "add", "a.txt"], cwd=repo)
    _run(["git", "commit", "-m", "first"], cwd=repo)

    (repo / "b.txt").write_text("b\n", encoding="utf-8")
    _run(["git", "add", "b.txt"], cwd=repo)
    _run(["git", "commit", "-m", "second"], cwd=repo)


def test_patch_ids_are_stable_and_resolvable(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo_with_commits(repo)

    state = GitNotesStore(repo)
    manager = PatchStackManager(repo, state_store=state)

    first_list = manager.list_patches()
    second_list = manager.list_patches()

    assert first_list
    assert [patch.patch_id for patch in first_list] == [patch.patch_id for patch in second_list]
    assert all(re.match(r"patch-[0-9a-f]{8}", patch.patch_id) for patch in first_list)

    resolved_legacy = manager.resolve_patch("patch-001")
    assert resolved_legacy is not None
    assert resolved_legacy.commit_hash == first_list[0].commit_hash


def test_patch_status_persisted_in_state(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo_with_commits(repo)

    state = GitNotesStore(repo)
    manager = PatchStackManager(repo, state_store=state)
    patch = manager.list_patches()[-1]

    manager.update_patch_status(patch.commit_hash, "accepted")
    refreshed = manager.resolve_patch(patch.patch_id)

    assert refreshed is not None
    assert refreshed.status == "accepted"


def test_reject_patch_uses_non_destructive_revert(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo_with_commits(repo)

    state = GitNotesStore(repo)
    manager = PatchStackManager(repo, state_store=state)
    patch = manager.list_patches()[-1]

    rejected = manager.reject_patch(patch.patch_id)
    assert rejected.commit_hash == patch.commit_hash

    refreshed = manager.resolve_patch(patch.patch_id)
    assert refreshed is not None
    assert refreshed.status == "rejected"

    subject = _run(["git", "log", "-1", "--pretty=%s"], cwd=repo)
    assert subject.startswith("Revert")


def test_rollback_checks_out_safe_branch(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo_with_commits(repo)

    state = GitNotesStore(repo)
    manager = PatchStackManager(repo, state_store=state)
    checkpoint = manager.create_checkpoint("before-rollback")

    (repo / "c.txt").write_text("c\n", encoding="utf-8")
    _run(["git", "add", "c.txt"], cwd=repo)
    _run(["git", "commit", "-m", "third"], cwd=repo)

    branch_name = manager.rollback(checkpoint)
    assert branch_name.startswith("architect/rollback-")
    current_branch = _run(["git", "branch", "--show-current"], cwd=repo)
    assert current_branch == branch_name


def test_precommit_guardrail_blocks_forbidden_paths_before_commit(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo_with_commits(repo)

    state = GitNotesStore(repo)
    manager = PatchStackManager(repo, state_store=state)
    before_head = _run(["git", "rev-parse", "HEAD"], cwd=repo)

    (repo / ".env").write_text("TOKEN=1\n", encoding="utf-8")

    with pytest.raises(ArchitectStateError, match="Pre-commit guardrail failed"):
        manager.create_task_patch_from_worktree(
            subject="architect: forbidden",
            body="test",
            task_id="task-implement-999",
            run_id="run-guardrail",
            fallback_mode="local_only",
            max_files=10,
            forbidden_paths=[".env"],
        )

    after_head = _run(["git", "rev-parse", "HEAD"], cwd=repo)
    assert before_head == after_head


def test_local_patch_lifecycle_without_git(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    state = GitNotesStore(repo, backend_mode="local")
    manager = PatchStackManager(repo, state_store=state)
    assert manager.git_enabled is False

    patch = manager.record_local_patch(
        subject="architect: task-implement-001",
        task_id="task-implement-001",
        run_id="run-local",
        files_changed=[".architect/runs/run-local/task-implement-001.md"],
    )

    patches = manager.list_patches()
    assert len(patches) == 1
    assert patches[0].patch_id == patch.patch_id
    assert manager.describe_patch(patch.patch_id)

    rejected = manager.reject_patch(patch.patch_id)
    assert rejected.status == "rejected"

    checkpoint = manager.create_checkpoint("local")
    rollback_target = manager.rollback(checkpoint)
    assert rollback_target.startswith("local/")


def test_create_task_patch_can_exclude_paths(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo_with_commits(repo)

    state = GitNotesStore(repo)
    manager = PatchStackManager(repo, state_store=state)

    (repo / "a.txt").write_text("dirty-a\n", encoding="utf-8")
    (repo / "b.txt").write_text("dirty-b\n", encoding="utf-8")

    patch = manager.create_task_patch_from_worktree(
        subject="architect: exclude-paths",
        body="exclude dirty paths",
        task_id="task-implement-exclude",
        run_id="run-exclude",
        fallback_mode="local_only",
        exclude_paths=["a.txt"],
    )

    changed = manager.changed_files_for_commit(patch.commit_hash)
    assert "b.txt" in changed
    assert "a.txt" not in changed
    unstaged = _run(["git", "diff", "--name-only"], cwd=repo)
    assert "a.txt" in unstaged
