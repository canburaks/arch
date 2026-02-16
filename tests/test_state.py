import json
import subprocess
from pathlib import Path

from architect.state.git_notes import GitNotesStore


def _run(cmd: list[str], cwd: Path) -> None:
    subprocess.run(cmd, cwd=cwd, check=True, text=True, capture_output=True)


def _init_git_repo(repo_path: Path) -> None:
    _run(["git", "init"], cwd=repo_path)
    _run(["git", "config", "user.email", "test@example.com"], cwd=repo_path)
    _run(["git", "config", "user.name", "Test User"], cwd=repo_path)
    (repo_path / "seed.txt").write_text("seed\n", encoding="utf-8")
    _run(["git", "add", "seed.txt"], cwd=repo_path)
    _run(["git", "commit", "-m", "seed"], cwd=repo_path)


def test_git_notes_store_roundtrip_in_git_repo(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    store = GitNotesStore(repo)
    payload = {"goal": "build auth", "phase": "planning"}
    store.set_json("context", payload)

    assert store.git_enabled is True
    assert store.get_json("context") == payload


def test_git_notes_store_fallback_to_local_state(tmp_path: Path) -> None:
    store = GitNotesStore(tmp_path)
    payload = {"goal": "build auth", "phase": "planning"}
    store.set_json("context", payload)

    assert store.git_enabled is False
    assert store.get_json("context") == payload


def test_state_schema_migrates_legacy_payload(tmp_path: Path) -> None:
    store = GitNotesStore(tmp_path)
    local_path = tmp_path / ".architect" / "state" / "context.json"
    local_path.parent.mkdir(parents=True, exist_ok=True)
    local_path.write_text(json.dumps({"legacy": True}), encoding="utf-8")

    payload = store.get_json("context")
    assert payload == {"legacy": True}

    store.set_json("context", {"legacy": False})
    on_disk = json.loads(local_path.read_text(encoding="utf-8"))
    assert on_disk["schema_version"] == GitNotesStore.SCHEMA_VERSION
    assert on_disk["data"] == {"legacy": False}


def test_update_json_increments_revision(tmp_path: Path) -> None:
    store = GitNotesStore(tmp_path)
    store.set_json("metrics", {"count": 1})
    first_revision = store.get_envelope("metrics")["revision"]

    store.update_json(
        "metrics", lambda payload: {"count": payload["count"] + 1}, default={"count": 0}
    )
    second_revision = store.get_envelope("metrics")["revision"]

    assert store.get_json("metrics")["count"] == 2
    assert second_revision > first_revision
