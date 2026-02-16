from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from architect.state.git_notes import ArchitectStateError, GitNotesStore


@dataclass(slots=True)
class Patch:
    patch_id: str
    commit_hash: str
    subject: str
    status: str = "pending"
    task_id: str | None = None
    files_changed: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.patch_id,
            "commit_hash": self.commit_hash,
            "subject": self.subject,
            "status": self.status,
            "task_id": self.task_id,
            "files_changed": list(self.files_changed),
        }


class PatchStackManager:
    def __init__(self, repo_root: Path, state_store: GitNotesStore | None = None) -> None:
        self.repo_root = repo_root.resolve()
        self.state_store = state_store
        self.local_checkpoints_file = self.repo_root / ".architect" / "checkpoints.json"
        self.local_checkpoints_file.parent.mkdir(parents=True, exist_ok=True)
        self._git_enabled = self._is_git_repo()

    @property
    def git_enabled(self) -> bool:
        return self._git_enabled

    def _is_git_repo(self) -> bool:
        proc = subprocess.run(
            ["git", "--no-pager", "rev-parse", "--is-inside-work-tree"],
            cwd=self.repo_root,
            text=True,
            capture_output=True,
        )
        return proc.returncode == 0 and proc.stdout.strip() == "true"

    def _run_git(self, args: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
        if not self.git_enabled:
            raise ArchitectStateError(
                "No git repository found. Git patch-stack operations are disabled."
            )
        proc = subprocess.run(
            ["git", "--no-pager", *args],
            cwd=self.repo_root,
            text=True,
            capture_output=True,
        )
        if check and proc.returncode != 0:
            raise ArchitectStateError(proc.stderr.strip() or proc.stdout.strip())
        return proc

    @staticmethod
    def _patch_id_for_commit(commit_hash: str) -> str:
        return f"patch-{commit_hash[:8]}"

    def _metrics(self) -> dict[str, Any]:
        if self.state_store is None:
            return {}
        return self.state_store.get_metrics()

    def _set_metrics(self, metrics: dict[str, Any]) -> None:
        if self.state_store is None:
            return
        self.state_store.set_metrics(metrics)

    def _ensure_patch_indexes(self, patches: list[Patch]) -> None:
        if self.state_store is None:
            return
        metrics = self._metrics()
        patch_index = metrics.get("patch_index", {})
        lifecycle = metrics.get("patch_lifecycle", {})
        patch_stack = metrics.get("patch_stack", [])
        changed = False

        if not isinstance(patch_index, dict):
            patch_index = {}
            changed = True
        if not isinstance(lifecycle, dict):
            lifecycle = {}
            changed = True
        if not isinstance(patch_stack, list):
            patch_stack = []
            changed = True

        existing_stack_hashes = {
            item.get("commit_hash")
            for item in patch_stack
            if isinstance(item, dict) and isinstance(item.get("commit_hash"), str)
        }
        now = datetime.now(UTC).replace(microsecond=0).isoformat()

        for patch in patches:
            if patch.commit_hash not in patch_index:
                patch_index[patch.commit_hash] = patch.patch_id
                changed = True
            if patch.commit_hash not in lifecycle:
                lifecycle[patch.commit_hash] = patch.status
                changed = True
            if patch.commit_hash not in existing_stack_hashes:
                patch_stack.append(
                    {
                        "patch_id": patch.patch_id,
                        "commit_hash": patch.commit_hash,
                        "subject": patch.subject,
                        "status": patch.status,
                        "task_id": patch.task_id,
                        "created_at": now,
                        "files_changed": patch.files_changed,
                    }
                )
                changed = True

        if changed:
            metrics["patch_index"] = patch_index
            metrics["patch_lifecycle"] = lifecycle
            metrics["patch_stack"] = patch_stack
            self._set_metrics(metrics)

    def current_branch(self) -> str:
        if not self.git_enabled:
            return "no-git"
        proc = self._run_git(["rev-parse", "--abbrev-ref", "HEAD"], check=True)
        return proc.stdout.strip()

    def create_branch(self, branch_name: str, start_point: str = "HEAD") -> None:
        self._run_git(["checkout", "-B", branch_name, start_point], check=True)

    def list_patches(self, base_ref: str | None = None) -> list[Patch]:
        if not self.git_enabled:
            return []
        range_part = f"{base_ref}..HEAD" if base_ref else "HEAD"
        proc = self._run_git(
            ["log", "--reverse", "--pretty=format:%H%x09%s", range_part],
            check=False,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            return []

        metrics = self._metrics()
        patch_index = (
            metrics.get("patch_index", {}) if isinstance(metrics.get("patch_index"), dict) else {}
        )
        lifecycle = (
            metrics.get("patch_lifecycle", {})
            if isinstance(metrics.get("patch_lifecycle"), dict)
            else {}
        )
        task_map: dict[str, str] = {}
        patch_stack = metrics.get("patch_stack", [])
        if isinstance(patch_stack, list):
            for item in patch_stack:
                if isinstance(item, dict):
                    commit_hash = item.get("commit_hash")
                    task_id = item.get("task_id")
                    if isinstance(commit_hash, str) and isinstance(task_id, str):
                        task_map[commit_hash] = task_id

        patches: list[Patch] = []
        for _index, line in enumerate(proc.stdout.splitlines(), start=1):
            commit_hash, _, subject = line.partition("\t")
            commit_hash = commit_hash.strip()
            patch_id = patch_index.get(commit_hash) if isinstance(patch_index, dict) else None
            if not isinstance(patch_id, str) or not patch_id:
                patch_id = self._patch_id_for_commit(commit_hash)
            status = "pending"
            if isinstance(lifecycle, dict):
                status = str(lifecycle.get(commit_hash, "pending"))
            files_changed = self.changed_files_for_commit(commit_hash)
            patches.append(
                Patch(
                    patch_id=patch_id,
                    commit_hash=commit_hash,
                    subject=subject.strip(),
                    status=status,
                    task_id=task_map.get(commit_hash),
                    files_changed=files_changed,
                )
            )

        self._ensure_patch_indexes(patches)

        # Backward compatibility with legacy positional IDs
        for index, patch in enumerate(patches, start=1):
            if patch.patch_id == self._patch_id_for_commit(patch.commit_hash):
                continue
            legacy_id = f"patch-{index:03d}"
            if patch.patch_id == legacy_id:
                patch.patch_id = self._patch_id_for_commit(patch.commit_hash)
        return patches

    def resolve_patch(self, patch_ref: str, base_ref: str | None = None) -> Patch | None:
        patches = self.list_patches(base_ref=base_ref)

        # Legacy positional reference support.
        if patch_ref.startswith("patch-") and patch_ref[6:].isdigit():
            patch_index = int(patch_ref.split("-", maxsplit=1)[1]) - 1
            if 0 <= patch_index < len(patches):
                return patches[patch_index]
            return None

        for patch in patches:
            if patch.patch_id == patch_ref:
                return patch
            if patch.commit_hash.startswith(patch_ref):
                return patch
            if patch.patch_id.startswith(patch_ref):
                return patch
        return None

    def changed_files_for_commit(self, commit_hash: str) -> list[str]:
        if not self.git_enabled:
            return []
        proc = self._run_git(["show", "--pretty=format:", "--name-only", commit_hash], check=False)
        if proc.returncode != 0:
            return []
        return [line.strip() for line in proc.stdout.splitlines() if line.strip()]

    def describe_patch(self, patch_ref: str, base_ref: str | None = None) -> str:
        patch = self.resolve_patch(patch_ref, base_ref=base_ref)
        if patch is None:
            raise ArchitectStateError(f"Patch not found: {patch_ref}")
        if not self.git_enabled:
            return ""
        proc = self._run_git(
            ["show", "--stat", "--pretty=format:%H%n%s%n%b", patch.commit_hash],
            check=True,
        )
        return proc.stdout.strip()

    def record_patch(
        self,
        commit_hash: str,
        subject: str,
        task_id: str,
        *,
        status: str = "pending",
        run_id: str | None = None,
    ) -> Patch:
        patch = Patch(
            patch_id=self._patch_id_for_commit(commit_hash),
            commit_hash=commit_hash,
            subject=subject,
            status=status,
            task_id=task_id,
            files_changed=self.changed_files_for_commit(commit_hash),
        )
        if self.state_store is not None:
            metrics = self.state_store.get_metrics()
            patch_index = metrics.get("patch_index", {})
            lifecycle = metrics.get("patch_lifecycle", {})
            stack = metrics.get("patch_stack", [])
            if not isinstance(patch_index, dict):
                patch_index = {}
            if not isinstance(lifecycle, dict):
                lifecycle = {}
            if not isinstance(stack, list):
                stack = []

            patch_index[commit_hash] = patch.patch_id
            lifecycle[commit_hash] = status
            stack.append(
                {
                    "patch_id": patch.patch_id,
                    "commit_hash": commit_hash,
                    "subject": subject,
                    "status": status,
                    "task_id": task_id,
                    "run_id": run_id,
                    "created_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
                    "files_changed": patch.files_changed,
                }
            )
            metrics["patch_index"] = patch_index
            metrics["patch_lifecycle"] = lifecycle
            metrics["patch_stack"] = stack
            self.state_store.set_metrics(metrics)
        return patch

    def update_patch_status(self, commit_hash: str, status: str, note: str | None = None) -> None:
        if self.state_store is None:
            return
        metrics = self.state_store.get_metrics()
        lifecycle = metrics.get("patch_lifecycle", {})
        stack = metrics.get("patch_stack", [])
        if not isinstance(lifecycle, dict):
            lifecycle = {}
        if not isinstance(stack, list):
            stack = []

        lifecycle[commit_hash] = status
        now = datetime.now(UTC).replace(microsecond=0).isoformat()
        for item in stack:
            if not isinstance(item, dict):
                continue
            if item.get("commit_hash") == commit_hash:
                item["status"] = status
                item["updated_at"] = now
                if note:
                    item["status_note"] = note

        metrics["patch_lifecycle"] = lifecycle
        metrics["patch_stack"] = stack
        self.state_store.set_metrics(metrics)

    def create_task_patch(
        self, artifact_path: Path, *, subject: str, body: str, task_id: str, run_id: str
    ) -> Patch:
        if not self.git_enabled:
            raise ArchitectStateError("Creating a patch requires a git repository.")
        rel_path = artifact_path.relative_to(self.repo_root)
        self._run_git(["add", str(rel_path)], check=True)
        self._run_git(["commit", "-m", subject, "-m", body], check=True)
        commit_hash = self._run_git(["rev-parse", "HEAD"], check=True).stdout.strip()
        return self.record_patch(commit_hash, subject, task_id, status="pending", run_id=run_id)

    def reject_patch(self, patch_ref: str, base_ref: str | None = None) -> Patch:
        patch = self.resolve_patch(patch_ref, base_ref=base_ref)
        if patch is None:
            raise ArchitectStateError(f"Patch not found: {patch_ref}")

        head_before = self._run_git(["rev-parse", "HEAD"], check=True).stdout.strip()
        parent_proc = self._run_git(["rev-parse", f"{patch.commit_hash}^"], check=True)
        parent_hash = parent_proc.stdout.strip()
        branch_name = self.current_branch()
        rebase_proc = self._run_git(
            ["rebase", "--onto", parent_hash, patch.commit_hash, branch_name],
            check=False,
        )
        if rebase_proc.returncode != 0:
            self._run_git(["rebase", "--abort"], check=False)
            self._run_git(["reset", "--hard", head_before], check=False)
            raise ArchitectStateError(
                "Reject failed due to rebase conflict. Repository restored to previous HEAD."
            )

        self.update_patch_status(patch.commit_hash, "rejected", note="Removed via rebase --onto")
        return patch

    def _read_local_checkpoints(self) -> list[str]:
        if not self.local_checkpoints_file.exists():
            return []
        try:
            payload = json.loads(self.local_checkpoints_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return []
        checkpoints = payload.get("checkpoints", [])
        if isinstance(checkpoints, list):
            return [str(item) for item in checkpoints]
        return []

    def _write_local_checkpoints(self, checkpoints: list[str]) -> None:
        payload: dict[str, Any] = {"checkpoints": checkpoints}
        self.local_checkpoints_file.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @staticmethod
    def _sanitize_checkpoint_name(name: str) -> str:
        safe = re.sub(r"[^a-zA-Z0-9._-]+", "-", name.strip().lower())
        return safe or "checkpoint"

    def create_checkpoint(self, name: str | None = None) -> str:
        timestamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
        label = self._sanitize_checkpoint_name(name or "checkpoint")
        checkpoint_id = f"architect/{label}-{timestamp}"

        if self.git_enabled:
            self._run_git(["tag", "-f", checkpoint_id], check=True)
            return checkpoint_id

        checkpoints = self._read_local_checkpoints()
        checkpoints.append(checkpoint_id)
        self._write_local_checkpoints(checkpoints)
        return checkpoint_id

    def list_checkpoints(self) -> list[str]:
        if self.git_enabled:
            proc = self._run_git(
                ["tag", "--list", "architect/*", "--sort=creatordate"],
                check=False,
            )
            if proc.returncode != 0:
                return []
            return [line.strip() for line in proc.stdout.splitlines() if line.strip()]
        return self._read_local_checkpoints()

    def rollback(self, checkpoint_id: str) -> None:
        if not self.git_enabled:
            raise ArchitectStateError("Rollback requires a git repository.")
        self._run_git(["reset", "--hard", checkpoint_id], check=True)
