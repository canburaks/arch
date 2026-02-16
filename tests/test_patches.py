import re
import subprocess
from pathlib import Path

from architect.state import GitNotesStore, PatchStackManager


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
