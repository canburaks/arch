from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from collections.abc import Callable
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class ArchitectStateError(RuntimeError):
    """Raised when shared-state operations fail."""


class GitNotesStore:
    NAMESPACES = {"tasks", "decisions", "context", "checkpoints", "metrics"}
    SCHEMA_VERSION = 1

    def __init__(
        self,
        repo_root: Path,
        *,
        backend_mode: str = "notes",
        branch_ref: str = "architect/state",
    ) -> None:
        self.repo_root = repo_root.resolve()
        self.local_state_dir = self.repo_root / ".architect" / "state"
        self.local_state_dir.mkdir(parents=True, exist_ok=True)
        self.anchor_file = self.repo_root / ".architect" / "anchor"
        self.lock_file = self.local_state_dir / ".lock"
        self._git_repo_available = self._is_git_repo()
        self._requested_backend_mode = backend_mode
        self._branch_ref = branch_ref
        if backend_mode not in {"notes", "branch", "local"}:
            raise ArchitectStateError(f"Unsupported state backend mode: {backend_mode}")
        if backend_mode == "local" or not self._git_repo_available:
            self._backend_mode = "local"
        else:
            self._backend_mode = backend_mode

    @property
    def git_enabled(self) -> bool:
        return self._backend_mode in {"notes", "branch"} and self._git_repo_available

    @property
    def backend_mode(self) -> str:
        return self._backend_mode

    @staticmethod
    def _utcnow_iso() -> str:
        return datetime.now(UTC).replace(microsecond=0).isoformat()

    def _is_git_repo(self) -> bool:
        proc = subprocess.run(
            ["git", "--no-pager", "rev-parse", "--is-inside-work-tree"],
            cwd=self.repo_root,
            text=True,
            capture_output=True,
        )
        return proc.returncode == 0 and proc.stdout.strip() == "true"

    def _run_git(
        self,
        args: list[str],
        input_text: str | None = None,
        env: dict[str, str] | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        proc = subprocess.run(
            ["git", "--no-pager", *args],
            cwd=self.repo_root,
            text=True,
            capture_output=True,
            input=input_text,
            env=env,
        )
        if check and proc.returncode != 0:
            raise ArchitectStateError(proc.stderr.strip() or proc.stdout.strip())
        return proc

    @staticmethod
    def _validate_namespace(namespace: str) -> None:
        if namespace not in GitNotesStore.NAMESPACES:
            raise ArchitectStateError(f"Unsupported namespace: {namespace}")

    def _local_file(self, namespace: str) -> Path:
        return self.local_state_dir / f"{namespace}.json"

    def _notes_ref(self, namespace: str) -> str:
        return f"refs/notes/architect/{namespace}"

    def _state_branch_ref(self) -> str:
        if self._branch_ref.startswith("refs/"):
            return self._branch_ref
        return f"refs/heads/{self._branch_ref}"

    def _state_branch_exists(self) -> bool:
        proc = self._run_git(
            ["show-ref", "--verify", "--quiet", self._state_branch_ref()],
            check=False,
        )
        return proc.returncode == 0

    def _read_branch_json(self, namespace: str) -> Any:
        proc = self._run_git(
            ["show", f"{self._state_branch_ref()}:{namespace}.json"],
            check=False,
        )
        if proc.returncode != 0:
            return None
        content = proc.stdout.strip()
        if not content:
            return None
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            return None

    def _write_branch_json(self, namespace: str, serialized: str) -> None:
        ref = self._state_branch_ref()
        parent_commit: str | None = None
        parent_tree: str | None = None
        if self._state_branch_exists():
            parent_commit = self._run_git(["rev-parse", ref], check=True).stdout.strip()
            parent_tree = self._run_git(
                ["rev-parse", f"{parent_commit}^{{tree}}"],
                check=True,
            ).stdout.strip()

        with tempfile.NamedTemporaryFile(
            prefix="architect-state-index-",
            delete=False,
        ) as index_file:
            index_path = index_file.name

        try:
            env = os.environ.copy()
            env["GIT_INDEX_FILE"] = index_path
            try:
                Path(index_path).unlink()
            except FileNotFoundError:
                pass
            if parent_tree:
                self._run_git(["read-tree", parent_tree], env=env, check=True)
            blob_hash = self._run_git(
                ["hash-object", "-w", "--stdin"],
                input_text=serialized,
                check=True,
            ).stdout.strip()
            index_info = f"100644 blob {blob_hash}\t{namespace}.json\n"
            self._run_git(
                ["update-index", "--index-info"],
                input_text=index_info,
                env=env,
                check=True,
            )
            new_tree = self._run_git(["write-tree"], env=env, check=True).stdout.strip()
            commit_args = ["commit-tree", new_tree]
            if parent_commit:
                commit_args.extend(["-p", parent_commit])
            commit_message = f"architect-state: update {namespace}\n"
            commit_hash = self._run_git(
                commit_args,
                input_text=commit_message,
                check=True,
            ).stdout.strip()
            self._run_git(["update-ref", ref, commit_hash], check=True)
        finally:
            try:
                os.unlink(index_path)
            except OSError:
                pass

    def _anchor_object(self) -> str:
        if not self.git_enabled:
            return "local-anchor"
        if self.anchor_file.exists():
            return self.anchor_file.read_text(encoding="utf-8").strip()
        proc = self._run_git(
            ["hash-object", "-w", "--stdin"],
            input_text="architect-state-anchor\n",
            check=True,
        )
        anchor = proc.stdout.strip()
        self.anchor_file.parent.mkdir(parents=True, exist_ok=True)
        self.anchor_file.write_text(anchor, encoding="utf-8")
        return anchor

    @contextmanager
    def _state_lock(self, timeout_seconds: float = 3.0):
        start = time.monotonic()
        while True:
            try:
                fd = os.open(self.lock_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode("utf-8"))
                os.close(fd)
                break
            except FileExistsError as exc:
                if time.monotonic() - start > timeout_seconds:
                    raise ArchitectStateError("Timed out waiting for state lock.") from exc
                time.sleep(0.02)

        try:
            yield
        finally:
            try:
                self.lock_file.unlink()
            except FileNotFoundError:
                pass

    def _read_raw_json(self, namespace: str) -> Any:
        if self.git_enabled and self.backend_mode == "notes":
            anchor = self._anchor_object()
            proc = self._run_git(
                ["notes", "--ref", self._notes_ref(namespace), "show", anchor],
                check=False,
            )
            if proc.returncode != 0:
                return None
            content = proc.stdout.strip()
            if not content:
                return None
            try:
                return json.loads(content)
            except json.JSONDecodeError:
                return None
        if self.git_enabled and self.backend_mode == "branch":
            return self._read_branch_json(namespace)

        local_file = self._local_file(namespace)
        if not local_file.exists():
            return None
        try:
            return json.loads(local_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None

    def _write_raw_json(self, namespace: str, payload: Any) -> None:
        serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        if self.git_enabled and self.backend_mode == "notes":
            anchor = self._anchor_object()
            self._run_git(
                [
                    "notes",
                    "--ref",
                    self._notes_ref(namespace),
                    "add",
                    "-f",
                    "-m",
                    serialized,
                    anchor,
                ],
                check=True,
            )
            return
        if self.git_enabled and self.backend_mode == "branch":
            self._write_branch_json(namespace, serialized)
            return
        self._local_file(namespace).write_text(serialized, encoding="utf-8")

    def _normalize_envelope(self, raw_payload: Any, default: Any) -> dict[str, Any]:
        if (
            isinstance(raw_payload, dict)
            and "schema_version" in raw_payload
            and "data" in raw_payload
            and "revision" in raw_payload
        ):
            schema_version = int(raw_payload.get("schema_version") or self.SCHEMA_VERSION)
            revision = int(raw_payload.get("revision") or 1)
            updated_at = raw_payload.get("updated_at") or self._utcnow_iso()
            return {
                "schema_version": schema_version,
                "revision": revision,
                "updated_at": updated_at,
                "data": raw_payload.get("data", default),
            }

        data = default if raw_payload is None else raw_payload
        return {
            "schema_version": self.SCHEMA_VERSION,
            "revision": 1,
            "updated_at": self._utcnow_iso(),
            "data": data,
        }

    def get_envelope(self, namespace: str, default: Any | None = None) -> dict[str, Any]:
        self._validate_namespace(namespace)
        default_value = {} if default is None else default
        raw = self._read_raw_json(namespace)
        return self._normalize_envelope(raw, default_value)

    def get_json(self, namespace: str, default: Any | None = None) -> Any:
        envelope = self.get_envelope(namespace, default=default)
        return envelope.get("data")

    def set_json(self, namespace: str, data: Any, expected_revision: int | None = None) -> None:
        self._validate_namespace(namespace)
        with self._state_lock():
            current = self.get_envelope(namespace, default={})
            current_revision = int(current.get("revision", 1))
            if expected_revision is not None and expected_revision != current_revision:
                raise ArchitectStateError(
                    f"Concurrent state update detected for namespace '{namespace}'."
                )
            envelope = {
                "schema_version": self.SCHEMA_VERSION,
                "revision": current_revision + 1,
                "updated_at": self._utcnow_iso(),
                "data": data,
            }
            self._write_raw_json(namespace, envelope)

    def update_json(
        self,
        namespace: str,
        updater: Callable[[Any], Any],
        default: Any | None = None,
    ) -> Any:
        default_value = {} if default is None else default
        last_error: Exception | None = None
        for _ in range(4):
            current = self.get_envelope(namespace, default=default_value)
            updated = updater(current.get("data", default_value))
            try:
                self.set_json(namespace, updated, expected_revision=int(current.get("revision", 1)))
                return updated
            except ArchitectStateError as exc:
                last_error = exc
                if "Concurrent state update detected" not in str(exc):
                    raise
                time.sleep(0.01)
        raise ArchitectStateError(str(last_error) if last_error else "State update failed.")

    def get_context(self) -> dict[str, Any]:
        context = self.get_json("context", default={})
        if isinstance(context, dict):
            return context
        return {}

    def set_context(self, context: dict[str, Any]) -> None:
        self.set_json("context", context)

    def get_tasks(self) -> list[dict[str, Any]]:
        payload = self.get_json("tasks", default={"task_queue": []})
        if not isinstance(payload, dict):
            return []
        queue = payload.get("task_queue", [])
        if isinstance(queue, list):
            return queue
        return []

    def set_tasks(self, tasks: list[dict[str, Any]]) -> None:
        self.set_json("tasks", {"task_queue": tasks})

    def get_decisions(self) -> list[dict[str, Any]]:
        payload = self.get_json("decisions", default={"decisions": []})
        if not isinstance(payload, dict):
            return []
        decisions = payload.get("decisions", [])
        if isinstance(decisions, list):
            return decisions
        return []

    def add_decision(self, decision: dict[str, Any]) -> None:
        def _updater(payload: Any) -> dict[str, Any]:
            result = payload if isinstance(payload, dict) else {"decisions": []}
            result.setdefault("decisions", [])
            result["decisions"].append(decision)
            return result

        self.update_json("decisions", _updater, default={"decisions": []})

    def get_checkpoints(self) -> list[dict[str, Any]]:
        payload = self.get_json("checkpoints", default={"checkpoints": []})
        if not isinstance(payload, dict):
            return []
        checkpoints = payload.get("checkpoints", [])
        if isinstance(checkpoints, list):
            return checkpoints
        return []

    def add_checkpoint(self, checkpoint: dict[str, Any]) -> None:
        def _updater(payload: Any) -> dict[str, Any]:
            result = payload if isinstance(payload, dict) else {"checkpoints": []}
            result.setdefault("checkpoints", [])
            result["checkpoints"].append(checkpoint)
            return result

        self.update_json("checkpoints", _updater, default={"checkpoints": []})

    def get_metrics(self) -> dict[str, Any]:
        metrics = self.get_json("metrics", default={})
        return metrics if isinstance(metrics, dict) else {}

    def set_metrics(self, metrics: dict[str, Any]) -> None:
        self.set_json("metrics", metrics)
