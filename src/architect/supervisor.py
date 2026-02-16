from __future__ import annotations

import fnmatch
import re
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from architect.config import ArchitectConfig
from architect.specialists.base import SpecialistAgent, SpecialistResponse
from architect.state.git_notes import GitNotesStore
from architect.state.patches import Patch, PatchStackManager

SEVERITY_PATTERN = re.compile(r"\b(BLOCKER|MAJOR|MINOR|SUGGESTION)\b", re.IGNORECASE)
COVERAGE_PATTERN = re.compile(r"\b(\d{1,3})%\b")


def _utcnow_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


@dataclass(slots=True)
class WorkTask:
    id: str
    type: str
    assigned_to: str
    description: str
    status: str = "pending"
    depends_on: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=_utcnow_iso)
    started_at: str | None = None
    completed_at: str | None = None
    output_summary: str = ""
    attempt: int = 0
    failure_reason: str | None = None
    patch_id: str | None = None
    allowed_tools: list[str] | None = None


@dataclass(slots=True)
class RunSummary:
    goal: str
    run_id: str
    started_at: str
    ended_at: str
    total_tasks: int
    completed_tasks: int
    checkpoint_id: str | None


class Supervisor:
    def __init__(
        self,
        state_store: GitNotesStore,
        patch_manager: PatchStackManager,
        specialists: dict[str, SpecialistAgent],
        config: ArchitectConfig,
        repo_root: Path,
        supervisor_agent: SpecialistAgent | None = None,
    ) -> None:
        self.state = state_store
        self.patches = patch_manager
        self.specialists = specialists
        self.config = config
        self.repo_root = repo_root.resolve()
        self.supervisor_agent = supervisor_agent

    @staticmethod
    def _gate_name(task_type: str) -> str:
        mapping = {
            "plan": "planning_gate",
            "implement": "implementation_gate",
            "test": "testing_gate",
            "review": "review_gate",
            "document": "documentation_gate",
        }
        return mapping.get(task_type, f"{task_type}_gate")

    @staticmethod
    def _extract_plan_steps(content: str) -> list[str]:
        steps: list[str] = []
        for raw_line in content.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            match = re.match(r"^(?:[-*]|\d+[.)])\s+(.+)$", line)
            if match:
                steps.append(match.group(1).strip())
        if not steps and content.strip():
            sentences = [item.strip() for item in re.split(r"[\n\.]", content) if item.strip()]
            steps = sentences[:6]
        return steps[:24]

    @staticmethod
    def _parse_review_findings(content: str) -> dict[str, int]:
        findings = {"BLOCKER": 0, "MAJOR": 0, "MINOR": 0, "SUGGESTION": 0}
        for match in SEVERITY_PATTERN.finditer(content):
            findings[match.group(1).upper()] += 1
        return findings

    @staticmethod
    def _extract_coverage_percent(result: dict[str, Any]) -> int | None:
        stdout_tail = str(result.get("stdout_tail", ""))
        stderr_tail = str(result.get("stderr_tail", ""))
        output = f"{stdout_tail}\n{stderr_tail}"
        matches = COVERAGE_PATTERN.findall(output)
        if not matches:
            return None
        percent = max(int(item) for item in matches)
        return min(100, max(0, percent))

    @staticmethod
    def _matches_forbidden_path(path: str, patterns: list[str]) -> str | None:
        normalized = path.replace("\\", "/")
        for pattern in patterns:
            if fnmatch.fnmatch(normalized, pattern):
                return pattern
        return None

    def _run_git(self, args: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
        proc = subprocess.run(
            ["git", "--no-pager", *args],
            cwd=self.repo_root,
            text=True,
            capture_output=True,
        )
        if check and proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip() or proc.stdout.strip())
        return proc

    def _ensure_clean_worktree(self) -> None:
        if not self.patches.git_enabled:
            return
        proc = self._run_git(["status", "--porcelain"], check=True)
        lines = [line for line in proc.stdout.splitlines() if line.strip()]
        if not lines:
            return
        # Ignore state artifacts used for local cache/audit trails.
        non_state = []
        for line in lines:
            if ".architect/" in line:
                continue
            if line.endswith(" architect.toml") or line.endswith("\tarchitect.toml"):
                continue
            non_state.append(line)
        if non_state:
            details = "\n".join(non_state[:20])
            raise RuntimeError(
                "Refusing to run with dirty worktree. Commit/stash changes first.\n"
                f"Detected:\n{details}"
            )

    def _persist_tasks(self, tasks: list[WorkTask]) -> None:
        self.state.set_tasks([asdict(task) for task in tasks])

    def _append_phase_history(
        self, context: dict[str, Any], phase: str, status: str
    ) -> dict[str, Any]:
        session = context.setdefault("session", {})
        history = session.setdefault("phase_history", [])
        history.append({"phase": phase, "status": status, "at": _utcnow_iso()})
        return context

    def _update_task_status(
        self,
        tasks: list[WorkTask],
        task_id: str,
        status: str,
        *,
        reason: str | None = None,
    ) -> None:
        for task in tasks:
            if task.id == task_id:
                task.status = status
                if status == "in_progress":
                    task.started_at = _utcnow_iso()
                if status in {"completed", "failed", "skipped"}:
                    task.completed_at = _utcnow_iso()
                if reason:
                    task.failure_reason = reason
                break
        self._persist_tasks(tasks)

    def _record_decision(self, task: WorkTask, response: SpecialistResponse) -> None:
        if task.assigned_to not in {"planner", "critic", "supervisor"}:
            return
        decision = {
            "id": f"dec-{task.id}",
            "topic": task.type,
            "decided_by": task.assigned_to,
            "approved_by": "supervisor",
            "decision": response.content[:4000],
            "rationale": f"Output from {task.assigned_to} for {task.id}",
            "created_at": _utcnow_iso(),
        }
        self.state.add_decision(decision)

    def _record_gate_result(self, gate: dict[str, Any]) -> None:
        metrics = self.state.get_metrics()
        quality_gates = metrics.get("quality_gates", [])
        if not isinstance(quality_gates, list):
            quality_gates = []
        quality_gates.append(gate)
        metrics["quality_gates"] = quality_gates[-200:]
        if not gate.get("passed"):
            failures = metrics.get("gate_failures", [])
            if not isinstance(failures, list):
                failures = []
            failures.append(
                {
                    "name": gate.get("name"),
                    "task_id": gate.get("task_id"),
                    "reason": gate.get("reason", "gate failed"),
                    "checked_at": gate.get("checked_at"),
                }
            )
            metrics["gate_failures"] = failures[-50:]
            metrics["last_gate_failure"] = failures[-1]
        self.state.set_metrics(metrics)

    def _run_command(self, command: str) -> dict[str, Any]:
        proc = subprocess.run(
            command,
            cwd=self.repo_root,
            shell=True,
            text=True,
            capture_output=True,
        )
        return {
            "type": "command",
            "command": command,
            "exit_code": proc.returncode,
            "stdout_tail": proc.stdout.strip()[-1000:],
            "stderr_tail": proc.stderr.strip()[-1000:],
        }

    def _assert_guardrail_test_coverage(self, run_patch_files: list[str]) -> tuple[bool, str]:
        guarded_patterns = self.config.guardrails.require_tests_for
        guarded_changes = []
        for file_path in run_patch_files:
            normalized = file_path.replace("\\", "/")
            for pattern in guarded_patterns:
                if fnmatch.fnmatch(normalized, pattern):
                    guarded_changes.append(normalized)
                    break
        if not guarded_changes:
            return True, ""
        test_touched = any(path.replace("\\", "/").startswith("tests/") for path in run_patch_files)
        if test_touched:
            return True, ""
        return False, (
            "Guardrail require_tests_for failed: source files changed without matching tests. "
            f"Patterns={guarded_patterns}"
        )

    def _evaluate_gate(
        self,
        task: WorkTask,
        response: SpecialistResponse,
        *,
        run_patch_files: list[str],
        current_patch: Patch | None,
    ) -> dict[str, Any]:
        gate_name = self._gate_name(task.type)
        artifacts: list[dict[str, Any]] = []
        reason = ""
        passed = True

        content = response.content.strip()
        if task.type == "plan":
            steps = self._extract_plan_steps(content)
            artifacts.append({"type": "planning_steps", "count": len(steps)})
            passed = bool(content) and len(steps) >= 1
            if not passed:
                reason = "Planning output must include at least one actionable step."

        elif task.type == "implement":
            if not content:
                passed = False
                reason = "Implementation output is empty."
            if passed and self.config.workflow.auto_lint:
                lint_result = self._run_command(self.config.project.lint_command)
                artifacts.append(lint_result)
                if lint_result["exit_code"] != 0:
                    passed = False
                    reason = "Lint command failed."
            if passed and self.config.project.type_check_command:
                type_result = self._run_command(self.config.project.type_check_command)
                artifacts.append(type_result)
                if type_result["exit_code"] != 0:
                    passed = False
                    reason = "Type-check command failed."
            if passed and current_patch is not None:
                max_files = self.config.guardrails.max_file_changes_per_patch
                file_count = len(current_patch.files_changed)
                artifacts.append(
                    {
                        "type": "guardrail",
                        "name": "max_file_changes_per_patch",
                        "max": max_files,
                        "actual": file_count,
                    }
                )
                if file_count > max_files:
                    passed = False
                    reason = (
                        "Guardrail max_file_changes_per_patch failed: "
                        f"{file_count} files changed (max {max_files})."
                    )
            if passed and current_patch is not None:
                for file_path in current_patch.files_changed:
                    matched = self._matches_forbidden_path(
                        file_path,
                        self.config.guardrails.forbidden_paths,
                    )
                    if matched:
                        passed = False
                        reason = (
                            "Forbidden path touched during implementation gate: "
                            f"{file_path} matched {matched}"
                        )
                        break

        elif task.type == "test":
            if self.config.workflow.auto_test:
                test_result = self._run_command(self.config.project.test_command)
                artifacts.append(test_result)
                if test_result["exit_code"] != 0:
                    passed = False
                    reason = "Test command failed."
                threshold = int(self.config.workflow.test_coverage_threshold)
                if passed and threshold > 0:
                    coverage_percent = self._extract_coverage_percent(test_result)
                    artifacts.append(
                        {
                            "type": "coverage",
                            "threshold": threshold,
                            "actual": coverage_percent,
                        }
                    )
                    if coverage_percent is None or coverage_percent < threshold:
                        passed = False
                        reason = (
                            "Coverage threshold failed: "
                            f"required {threshold}%, got {coverage_percent}."
                        )

        elif task.type == "review":
            findings = self._parse_review_findings(content)
            artifacts.append({"type": "findings", "counts": findings})
            if self.config.workflow.require_critic_approval and findings["BLOCKER"] > 0:
                passed = False
                reason = f"Critic reported {findings['BLOCKER']} blocker finding(s)."
            if passed:
                coverage_ok, coverage_reason = self._assert_guardrail_test_coverage(run_patch_files)
                artifacts.append(
                    {
                        "type": "guardrail",
                        "name": "require_tests_for",
                        "patterns": list(self.config.guardrails.require_tests_for),
                        "passed": coverage_ok,
                    }
                )
                if not coverage_ok:
                    passed = False
                    reason = coverage_reason

        elif task.type == "document":
            passed = bool(content)
            if not passed:
                reason = "Documentation output is empty."
            if passed:
                source_touched = any(
                    path.replace("\\", "/").startswith("src/")
                    for path in run_patch_files
                )
                if source_touched:
                    lower = content.lower()
                    if not any(token in lower for token in ("doc", "readme", "changelog")):
                        passed = False
                        reason = (
                            "Documentation gate requires explicit documentation "
                            "impact summary."
                        )

        return {
            "name": gate_name,
            "task_id": task.id,
            "passed": passed,
            "reason": reason,
            "artifacts": artifacts,
            "checked_at": _utcnow_iso(),
        }

    def _next_ready_task(self, tasks: list[WorkTask]) -> WorkTask | None:
        task_by_id = {task.id: task for task in tasks}
        for task in tasks:
            if task.status != "pending":
                continue
            if all(
                task_by_id.get(dep_id) and task_by_id[dep_id].status == "completed"
                for dep_id in task.depends_on
            ):
                return task
        return None

    @staticmethod
    def _allowed_tools_for_task(task: WorkTask) -> list[str] | None:
        if task.allowed_tools:
            return list(task.allowed_tools)
        if task.type == "implement":
            return ["read_file", "write_file", "edit_file", "run_command", "search"]
        if task.type == "test":
            return ["read_file", "run_command"]
        if task.type == "review":
            return ["read_file", "run_command", "search"]
        if task.type == "document":
            return ["read_file", "write_file", "edit_file", "search"]
        return None

    async def _run_specialist(self, task: WorkTask, goal: str) -> SpecialistResponse:
        specialist = self.specialists.get(task.assigned_to)
        if specialist is None:
            raise RuntimeError(f"No specialist registered for role '{task.assigned_to}'.")
        allowed_tools = self._allowed_tools_for_task(task)
        return await specialist.run(
            instruction=task.description,
            context={"goal": goal, "task": asdict(task)},
            allowed_tools=allowed_tools,
        )

    async def _run_supervisor_decomposition(self, goal: str) -> list[str]:
        if self.supervisor_agent is None:
            return []
        response = await self.supervisor_agent.run(
            instruction=(
                "Decompose this goal into implementation milestones and ordering constraints. "
                "Return concise numbered or bullet steps."
            ),
            context={"goal": goal, "phase": "decomposition"},
            allowed_tools=None,
        )
        steps = self._extract_plan_steps(response.content)
        self.state.add_decision(
            {
                "id": f"dec-supervisor-{uuid4().hex[:8]}",
                "topic": "goal_decomposition",
                "decided_by": "supervisor",
                "approved_by": "supervisor",
                "decision": response.content[:4000],
                "rationale": "Supervisor decomposition before planner task.",
                "created_at": _utcnow_iso(),
            }
        )
        return steps

    async def _run_replan(self, failed_task: WorkTask, reason: str, goal: str) -> None:
        planner = self.specialists.get("planner")
        if planner is None:
            return
        response = await planner.run(
            instruction=(
                "Re-plan after a failed quality gate. "
                f"Task={failed_task.id}. Reason={reason}. Provide concise corrective steps."
            ),
            context={"goal": goal, "failed_task": asdict(failed_task), "reason": reason},
        )
        self.state.add_decision(
            {
                "id": f"dec-replan-{failed_task.id}-{uuid4().hex[:8]}",
                "topic": "replan",
                "decided_by": "planner",
                "approved_by": "supervisor",
                "decision": response.content[:4000],
                "rationale": f"Automatic replanning after failure in {failed_task.id}",
                "created_at": _utcnow_iso(),
            }
        )

    def _task_artifact_path(self, run_id: str, task: WorkTask) -> Path:
        run_dir = self.repo_root / ".architect" / "runs" / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        return run_dir / f"{task.id}.md"

    def _write_task_artifact(
        self, run_id: str, task: WorkTask, response: SpecialistResponse
    ) -> Path:
        artifact_path = self._task_artifact_path(run_id, task)
        artifact_path.write_text(
            "\n".join(
                [
                    f"# Task {task.id}",
                    "",
                    f"Type: {task.type}",
                    f"Assigned: {task.assigned_to}",
                    f"Generated At: {_utcnow_iso()}",
                    "",
                    "## Output",
                    response.content.strip(),
                    "",
                ]
            ),
            encoding="utf-8",
        )
        return artifact_path

    def _tracked_fallback_patch_path(self, run_id: str, task: WorkTask) -> Path:
        target_dir = self.repo_root / "docs" / "architect-runs" / run_id
        return target_dir / f"{task.id}.md"

    def _tracked_fallback_patch_content(self, run_id: str, task: WorkTask, response: str) -> str:
        return "\n".join(
            [
                f"# {task.id}",
                "",
                f"Run: {run_id}",
                f"Type: {task.type}",
                f"Generated At: {_utcnow_iso()}",
                "",
                "## Fallback output",
                response.strip(),
                "",
            ]
        )

    def _append_session_patch(self, patch: Patch) -> None:
        session_context = self.state.get_context()
        session = session_context.get("session", {})
        if not isinstance(session, dict):
            return
        patch_stack = session.get("patch_stack", [])
        if not isinstance(patch_stack, list):
            patch_stack = []
        patch_stack.append(patch.to_dict())
        session["patch_stack"] = patch_stack
        session_context["session"] = session
        self.state.set_context(session_context)

    async def _run_conflict_resolution(
        self,
        review_task: WorkTask,
        critic_output: str,
        goal: str,
        run_id: str,
    ) -> Patch | None:
        coder = self.specialists.get("coder")
        if coder is None:
            return None
        response = await coder.run(
            instruction=(
                "Resolve critic blockers from review output and apply required repository changes. "
                "Return concise remediation actions and what was fixed.\n\n"
                f"Critic output:\n{critic_output[:4000]}"
            ),
            context={"goal": goal, "review_task": asdict(review_task), "remediation": True},
            allowed_tools=["read_file", "write_file", "edit_file", "run_command", "search"],
        )
        self.state.add_decision(
            {
                "id": f"dec-conflict-{review_task.id}-{uuid4().hex[:8]}",
                "topic": "conflict_resolution",
                "decided_by": "coder",
                "approved_by": "supervisor",
                "decision": response.content[:4000],
                "rationale": "Automated remediation loop after BLOCKER review findings.",
                "created_at": _utcnow_iso(),
            }
        )
        if not self.patches.git_enabled:
            return None
        patch = self.patches.create_task_patch_from_worktree(
            subject=f"architect: remediation-{review_task.id}",
            body=f"Run: {run_id}\nTask: {review_task.id}\n\n{response.content[:2000]}",
            task_id=f"{review_task.id}-remediation",
            run_id=run_id,
            fallback_file=self._tracked_fallback_patch_path(run_id, review_task),
            fallback_content=self._tracked_fallback_patch_content(
                run_id,
                review_task,
                response.content,
            ),
        )
        return patch

    @staticmethod
    def _task_from_dict(payload: dict[str, Any]) -> WorkTask | None:
        try:
            return WorkTask(
                id=str(payload["id"]),
                type=str(payload["type"]),
                assigned_to=str(payload["assigned_to"]),
                description=str(payload["description"]),
                status=str(payload.get("status", "pending")),
                depends_on=list(payload.get("depends_on", [])),
                created_at=str(payload.get("created_at") or _utcnow_iso()),
                started_at=payload.get("started_at"),
                completed_at=payload.get("completed_at"),
                output_summary=str(payload.get("output_summary", "")),
                attempt=int(payload.get("attempt", 0)),
                failure_reason=payload.get("failure_reason"),
                patch_id=payload.get("patch_id"),
                allowed_tools=payload.get("allowed_tools"),
            )
        except (KeyError, TypeError, ValueError):
            return None

    def _load_pending_modify_tasks(self) -> list[WorkTask]:
        tasks = self.state.get_tasks()
        pending: list[WorkTask] = []
        for item in tasks:
            if not isinstance(item, dict):
                continue
            task_id = str(item.get("id", ""))
            if not task_id.startswith("task-modify-"):
                continue
            status = str(item.get("status", "pending"))
            if status not in {"pending", "in_progress", "failed"}:
                continue
            task = self._task_from_dict(item)
            if task is None:
                continue
            task.status = "pending"
            task.started_at = None
            task.completed_at = None
            task.failure_reason = None
            task.depends_on = ["task-plan-001"]
            task.assigned_to = "coder"
            pending.append(task)
        return pending

    def _create_task_graph(
        self,
        goal: str,
        plan_steps: list[str],
        modify_tasks: list[WorkTask],
    ) -> list[WorkTask]:
        tasks: list[WorkTask] = [
            WorkTask(
                id="task-plan-001",
                type="plan",
                assigned_to="planner",
                description=f"Design a technical approach for: {goal}",
            )
        ]
        implementation_tasks: list[WorkTask] = []
        implementation_tasks.extend(modify_tasks)

        if not plan_steps:
            plan_steps = [f"Implement the approved plan for: {goal}"]

        for index, step in enumerate(plan_steps, start=1):
            task_id = f"task-implement-{index:03d}"
            implementation_tasks.append(
                WorkTask(
                    id=task_id,
                    type="implement",
                    assigned_to="coder",
                    description=f"Implement step {index}: {step}",
                    depends_on=["task-plan-001"],
                )
            )

        max_chunk = max(1, int(self.config.workflow.max_patches_before_review))
        previous_gate_id = "task-plan-001"
        for chunk_start in range(0, len(implementation_tasks), max_chunk):
            chunk_index = (chunk_start // max_chunk) + 1
            chunk = implementation_tasks[chunk_start : chunk_start + max_chunk]
            implement_ids: list[str] = []
            for task in chunk:
                task.depends_on = [previous_gate_id]
                tasks.append(task)
                implement_ids.append(task.id)

            test_task_id = f"task-test-{chunk_index:03d}"
            tasks.append(
                WorkTask(
                    id=test_task_id,
                    type="test",
                    assigned_to="tester",
                    description=f"Test implementation chunk {chunk_index} for: {goal}",
                    depends_on=implement_ids,
                )
            )
            if self.config.workflow.require_critic_approval:
                review_task_id = f"task-review-{chunk_index:03d}"
                tasks.append(
                    WorkTask(
                        id=review_task_id,
                        type="review",
                        assigned_to="critic",
                        description=f"Review implementation chunk {chunk_index} for: {goal}",
                        depends_on=[test_task_id],
                    )
                )
                previous_gate_id = review_task_id
            else:
                previous_gate_id = test_task_id

        tasks.append(
            WorkTask(
                id="task-document-001",
                type="document",
                assigned_to="documenter",
                description=f"Document final changes for: {goal}",
                depends_on=[previous_gate_id],
            )
        )
        return tasks

    async def run(self, goal: str, *, resume: bool = False) -> RunSummary:
        now = _utcnow_iso()
        context = self.state.get_context()
        if context.get("paused") and not resume:
            raise RuntimeError("Workflow is paused. Run `arch resume` first.")

        if not resume:
            self._ensure_clean_worktree()

        existing_tasks_payload = self.state.get_tasks()
        existing_tasks: list[WorkTask] = []
        for item in existing_tasks_payload:
            if not isinstance(item, dict):
                continue
            task = self._task_from_dict(item)
            if task is None:
                continue
            if task.status == "in_progress":
                task.status = "pending"
                task.started_at = None
            existing_tasks.append(task)
        resumable_tasks = [task for task in existing_tasks if task.status in {"pending", "failed"}]

        if resume and resumable_tasks and context.get("current_run_id"):
            run_id = str(context["current_run_id"])
            started_at = str(context.get("started_at") or now)
            base_branch = str(context.get("active_branch") or self.patches.current_branch())
            run_branch = self.patches.current_branch()
            tasks = existing_tasks
        else:
            started_at = now
            run_id = f"run-{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}-{uuid4().hex[:8]}"
            base_branch = self.patches.current_branch()
            run_branch = base_branch
            if self.patches.git_enabled:
                run_branch = f"architect/{run_id}"
                self.patches.create_branch(run_branch, start_point=base_branch)
            plan_task = WorkTask(
                id="task-plan-001",
                type="plan",
                assigned_to="planner",
                description=f"Design a technical approach for: {goal}",
            )
            tasks = [plan_task]

        context_payload = {
            "goal": goal,
            "phase": "planning",
            "status": "in_progress",
            "active_branch": run_branch,
            "started_at": started_at,
            "paused": False,
            "current_run_id": run_id,
            "session": {
                "run_id": run_id,
                "goal": goal,
                "base_branch": base_branch,
                "active_branch": run_branch,
                "started_at": started_at,
                "phase_history": [{"phase": "planning", "status": "started", "at": now}],
                "patch_stack": [],
            },
        }
        self.state.set_context(context_payload)
        self._persist_tasks(tasks)

        supervisor_steps = await self._run_supervisor_decomposition(goal)
        modify_tasks = self._load_pending_modify_tasks()
        run_patch_files: list[str] = []
        completed_tasks = 0

        while True:
            ready_task = self._next_ready_task(tasks)
            if ready_task is None:
                break
            self._update_task_status(tasks, ready_task.id, "in_progress")
            context = self._append_phase_history(
                self.state.get_context(),
                ready_task.type,
                "started",
            )
            context["phase"] = ready_task.type
            self.state.set_context(context)

            gate: dict[str, Any] | None = None
            response: SpecialistResponse | None = None
            created_patch: Patch | None = None

            for attempt in range(1, 3):
                ready_task.attempt = attempt
                response = await self._run_specialist(ready_task, goal)
                ready_task.output_summary = response.content[:4000]
                self._write_task_artifact(run_id, ready_task, response)

                if ready_task.type == "plan" and len(tasks) == 1:
                    plan_steps = self._extract_plan_steps(response.content)
                    if not plan_steps:
                        plan_steps = supervisor_steps
                    tasks = self._create_task_graph(goal, plan_steps, modify_tasks)
                    tasks[0].attempt = ready_task.attempt
                    tasks[0].output_summary = ready_task.output_summary
                    self._persist_tasks(tasks)
                    ready_task = tasks[0]

                if ready_task.type in {"implement", "document"} and self.patches.git_enabled:
                    created_patch = self.patches.create_task_patch_from_worktree(
                        subject=f"architect: {ready_task.id}",
                        body=(
                            f"Run: {run_id}\nTask: {ready_task.id}\n\n"
                            f"{response.content[:2000]}"
                        ),
                        task_id=ready_task.id,
                        run_id=run_id,
                        fallback_file=self._tracked_fallback_patch_path(run_id, ready_task),
                        fallback_content=self._tracked_fallback_patch_content(
                            run_id,
                            ready_task,
                            response.content,
                        ),
                    )
                    ready_task.patch_id = created_patch.patch_id
                    run_patch_files.extend(created_patch.files_changed)
                    self._append_session_patch(created_patch)

                gate = self._evaluate_gate(
                    ready_task,
                    response,
                    run_patch_files=run_patch_files,
                    current_patch=created_patch,
                )
                self._record_gate_result(gate)
                if gate["passed"]:
                    break

                if attempt == 1:
                    await self._run_replan(ready_task, gate["reason"], goal)
                    if ready_task.type == "review" and response.content.strip():
                        remediation_patch = await self._run_conflict_resolution(
                            ready_task,
                            response.content,
                            goal,
                            run_id,
                        )
                        if remediation_patch is not None:
                            run_patch_files.extend(remediation_patch.files_changed)
                            self._append_session_patch(remediation_patch)
                    continue

            if response is None or gate is None:
                raise RuntimeError(f"Task execution failed unexpectedly for {ready_task.id}")
            if not gate["passed"]:
                self._update_task_status(tasks, ready_task.id, "failed", reason=gate["reason"])
                context = self.state.get_context()
                context["status"] = "failed"
                context["phase"] = ready_task.type
                context["ended_at"] = _utcnow_iso()
                context = self._append_phase_history(context, ready_task.type, "failed")
                self.state.set_context(context)
                raise RuntimeError(
                    f"Quality gate failed: {gate['name']} ({ready_task.id}) - {gate['reason']}"
                )

            self._record_decision(ready_task, response)
            self._update_task_status(tasks, ready_task.id, "completed")
            completed_tasks += 1
            context = self._append_phase_history(
                self.state.get_context(),
                ready_task.type,
                "completed",
            )
            next_phase = {
                "plan": "implementation",
                "implement": "implementation",
                "test": "review",
                "review": "documentation",
                "document": "complete",
            }.get(ready_task.type, "in_progress")
            context["phase"] = next_phase
            self.state.set_context(context)

        if any(task.status != "completed" for task in tasks):
            pending = [task.id for task in tasks if task.status != "completed"]
            raise RuntimeError(f"Task graph did not complete. Pending tasks: {pending}")

        checkpoint_id = self.patches.create_checkpoint(f"{run_id}-complete")
        self.state.add_checkpoint(
            {
                "id": checkpoint_id,
                "created_at": _utcnow_iso(),
                "goal": goal,
                "run_id": run_id,
                "active_branch": self.patches.current_branch(),
            }
        )

        metrics = self.state.get_metrics()
        patch_stack = metrics.get("patch_stack", [])
        if isinstance(patch_stack, list):
            for item in patch_stack:
                if isinstance(item, dict) and item.get("run_id") == run_id:
                    item["checkpoint_id"] = checkpoint_id
            metrics["patch_stack"] = patch_stack
            metrics["last_run_completed_tasks"] = completed_tasks
            metrics["last_run_id"] = run_id
            self.state.set_metrics(metrics)

        ended_at = _utcnow_iso()
        final_context = self.state.get_context()
        final_context.update(
            {
                "goal": goal,
                "phase": "complete",
                "status": "complete",
                "active_branch": self.patches.current_branch(),
                "started_at": started_at,
                "ended_at": ended_at,
                "paused": False,
            }
        )
        session = final_context.get("session", {})
        if isinstance(session, dict):
            session["ended_at"] = ended_at
            session["checkpoint_id"] = checkpoint_id
            final_context["session"] = session
        final_context = self._append_phase_history(final_context, "complete", "completed")
        self.state.set_context(final_context)

        return RunSummary(
            goal=goal,
            run_id=run_id,
            started_at=started_at,
            ended_at=ended_at,
            total_tasks=len(tasks),
            completed_tasks=completed_tasks,
            checkpoint_id=checkpoint_id,
        )

    def status(self, verbose: bool = False) -> dict[str, Any]:
        tasks = self.state.get_tasks()
        if not verbose:
            tasks = [
                {
                    "id": task.get("id"),
                    "type": task.get("type"),
                    "assigned_to": task.get("assigned_to"),
                    "status": task.get("status"),
                    "attempt": task.get("attempt"),
                }
                for task in tasks
            ]

        metrics = self.state.get_metrics()
        gate_failures = metrics.get("gate_failures", [])
        if not isinstance(gate_failures, list):
            gate_failures = []
        recent_failures = gate_failures[-5:]

        return {
            "context": self.state.get_context(),
            "tasks": tasks,
            "decisions": self.state.get_decisions(),
            "metrics": metrics,
            "recent_gate_failures": recent_failures,
            "checkpoints": self.state.get_checkpoints(),
            "patches": [patch.to_dict() for patch in self.patches.list_patches()],
        }

    def pause(self) -> None:
        context = self.state.get_context()
        context["paused"] = True
        context["status"] = "paused"
        context.setdefault("phase", "paused")
        self.state.set_context(context)

    def resume(self) -> None:
        context = self.state.get_context()
        context["paused"] = False
        context["status"] = "in_progress"
        context.setdefault("phase", "implementation")
        self.state.set_context(context)
