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
    ) -> None:
        self.state = state_store
        self.patches = patch_manager
        self.specialists = specialists
        self.config = config
        self.repo_root = repo_root.resolve()

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
            steps = sentences[:3]
        return steps[:6]

    @staticmethod
    def _parse_review_findings(content: str) -> dict[str, int]:
        findings = {"BLOCKER": 0, "MAJOR": 0, "MINOR": 0, "SUGGESTION": 0}
        for match in SEVERITY_PATTERN.finditer(content):
            findings[match.group(1).upper()] += 1
        return findings

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
        if task.assigned_to not in {"planner", "critic"}:
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
            passed = bool(content)
            if not passed:
                reason = "Planning output is empty."
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
        elif task.type == "test":
            if self.config.workflow.auto_test:
                test_result = self._run_command(self.config.project.test_command)
                artifacts.append(test_result)
                if test_result["exit_code"] != 0:
                    passed = False
                    reason = "Test command failed."
        elif task.type == "review":
            findings = self._parse_review_findings(content)
            artifacts.append({"type": "findings", "counts": findings})
            if findings["BLOCKER"] > 0:
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

    async def _run_specialist(self, task: WorkTask, goal: str) -> SpecialistResponse:
        specialist = self.specialists.get(task.assigned_to)
        if specialist is None:
            raise RuntimeError(f"No specialist registered for role '{task.assigned_to}'.")
        return await specialist.run(
            instruction=task.description, context={"goal": goal, "task": asdict(task)}
        )

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

    async def _run_conflict_resolution(
        self, review_task: WorkTask, critic_output: str, goal: str
    ) -> None:
        coder = self.specialists.get("coder")
        if coder is None:
            return
        response = await coder.run(
            instruction=(
                "Resolve critic blockers from review output. "
                "Return concise remediation actions and what was fixed.\n\n"
                f"Critic output:\n{critic_output[:4000]}"
            ),
            context={"goal": goal, "review_task": asdict(review_task)},
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

    def _create_task_graph(self, goal: str, plan_steps: list[str]) -> list[WorkTask]:
        tasks: list[WorkTask] = [
            WorkTask(
                id="task-plan-001",
                type="plan",
                assigned_to="planner",
                description=f"Design a technical approach for: {goal}",
            )
        ]

        if not plan_steps:
            plan_steps = [f"Implement the approved plan for: {goal}"]

        implementation_ids: list[str] = []
        for index, step in enumerate(plan_steps, start=1):
            task_id = f"task-implement-{index:03d}"
            implementation_ids.append(task_id)
            tasks.append(
                WorkTask(
                    id=task_id,
                    type="implement",
                    assigned_to="coder",
                    description=f"Implement step {index}: {step}",
                    depends_on=["task-plan-001"],
                )
            )

        tasks.append(
            WorkTask(
                id="task-test-001",
                type="test",
                assigned_to="tester",
                description=f"Test the implementation for: {goal}",
                depends_on=list(implementation_ids),
            )
        )
        tasks.append(
            WorkTask(
                id="task-review-001",
                type="review",
                assigned_to="critic",
                description=f"Review quality/security for: {goal}",
                depends_on=["task-test-001"],
            )
        )
        tasks.append(
            WorkTask(
                id="task-document-001",
                type="document",
                assigned_to="documenter",
                description=f"Document final changes for: {goal}",
                depends_on=["task-review-001"],
            )
        )
        return tasks

    async def run(self, goal: str) -> RunSummary:
        started_at = _utcnow_iso()
        run_id = f"run-{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}-{uuid4().hex[:8]}"
        context = self.state.get_context()
        if context.get("paused"):
            raise RuntimeError("Workflow is paused. Run `arch resume` first.")

        base_branch = self.patches.current_branch()
        run_branch = base_branch
        if self.patches.git_enabled:
            run_branch = f"architect/{run_id}"
            self.patches.create_branch(run_branch, start_point=base_branch)

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
                "phase_history": [{"phase": "planning", "status": "started", "at": started_at}],
                "patch_stack": [],
            },
        }
        self.state.set_context(context_payload)

        plan_task = WorkTask(
            id="task-plan-001",
            type="plan",
            assigned_to="planner",
            description=f"Design a technical approach for: {goal}",
        )
        tasks: list[WorkTask] = [plan_task]
        self._persist_tasks(tasks)

        run_patch_files: list[str] = []
        completed_tasks = 0

        while True:
            ready_task = self._next_ready_task(tasks)
            if ready_task is None:
                break

            self._update_task_status(tasks, ready_task.id, "in_progress")
            context = self._append_phase_history(
                self.state.get_context(), ready_task.type, "started"
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

                # Dynamic decomposition from the approved plan output.
                if ready_task.type == "plan" and len(tasks) == 1:
                    plan_steps = self._extract_plan_steps(response.content)
                    tasks = self._create_task_graph(goal, plan_steps)
                    tasks[0].attempt = ready_task.attempt
                    tasks[0].output_summary = ready_task.output_summary
                    self._persist_tasks(tasks)
                    ready_task = tasks[0]

                if ready_task.type == "implement" and self.patches.git_enabled:
                    artifact = self._write_task_artifact(run_id, ready_task, response)
                    created_patch = self.patches.create_task_patch(
                        artifact,
                        subject=f"architect: {ready_task.id}",
                        body=f"Run: {run_id}\nTask: {ready_task.id}\n\n{response.content[:2000]}",
                        task_id=ready_task.id,
                        run_id=run_id,
                    )
                    ready_task.patch_id = created_patch.patch_id
                    run_patch_files.extend(created_patch.files_changed)
                    session = self.state.get_context().get("session", {})
                    if isinstance(session, dict):
                        patch_stack = session.get("patch_stack", [])
                        if isinstance(patch_stack, list):
                            patch_stack.append(created_patch.to_dict())
                            session["patch_stack"] = patch_stack
                            context = self.state.get_context()
                            context["session"] = session
                            self.state.set_context(context)

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
                        await self._run_conflict_resolution(ready_task, response.content, goal)
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
                self.state.get_context(), ready_task.type, "completed"
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

        # Link checkpoint to run patches.
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
