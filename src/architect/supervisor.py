from __future__ import annotations

import asyncio
import fnmatch
import importlib.util
import json
import re
import shlex
import subprocess
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from architect.config import ArchitectConfig
from architect.specialists.base import SpecialistAgent, SpecialistResponse
from architect.state.git_notes import ArchitectStateError, GitNotesStore
from architect.state.patches import Patch, PatchStackManager

SEVERITY_PATTERN = re.compile(r"\b(BLOCKER|MAJOR|MINOR|SUGGESTION)\b", re.IGNORECASE)
COVERAGE_PATTERN = re.compile(r"\b(\d{1,3})%\b")
SHELL_REQUIRED_PATTERN = re.compile(r"(?:\|\||&&|[|;<>`]|[$]\()")
TOOL_POLICY_ALLOWLIST = {
    "read_file",
    "write_file",
    "edit_file",
    "run_command",
    "search",
}


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
        self._isolated_dirty_paths: list[str] = []

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
        parsed_structured = False
        for payload in Supervisor._extract_json_objects(content):
            if not isinstance(payload, dict):
                continue
            counts = payload.get("counts")
            if isinstance(counts, dict):
                for key, value in counts.items():
                    normalized = str(key).upper()
                    if normalized in findings:
                        try:
                            findings[normalized] += int(value)
                            parsed_structured = True
                        except (TypeError, ValueError):
                            continue
            severity = payload.get("severity")
            if isinstance(severity, str):
                normalized = severity.upper()
                if normalized in findings:
                    findings[normalized] += 1
                    parsed_structured = True
            items = payload.get("findings")
            if isinstance(items, list):
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    severity = item.get("severity")
                    if isinstance(severity, str):
                        normalized = severity.upper()
                        if normalized in findings:
                            findings[normalized] += 1
                            parsed_structured = True

        if parsed_structured:
            return findings

        for match in SEVERITY_PATTERN.finditer(content):
            findings[match.group(1).upper()] += 1
        return findings

    @staticmethod
    def _extract_coverage_percent(result: dict[str, Any]) -> int | None:
        stdout_tail = str(result.get("stdout_tail", ""))
        stderr_tail = str(result.get("stderr_tail", ""))
        output = f"{stdout_tail}\n{stderr_tail}"
        for payload in Supervisor._extract_json_objects(output):
            percent = Supervisor._coverage_from_payload(payload)
            if percent is not None:
                return percent
        matches = COVERAGE_PATTERN.findall(output)
        if not matches:
            return None
        percent = max(int(item) for item in matches)
        return min(100, max(0, percent))

    @staticmethod
    def _extract_json_objects(raw_text: str) -> list[dict[str, Any]]:
        payloads: list[dict[str, Any]] = []
        for raw_line in raw_text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if not (line.startswith("{") and line.endswith("}")):
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                payloads.append(parsed)
        return payloads

    @staticmethod
    def _coverage_from_payload(payload: dict[str, Any]) -> int | None:
        if "coverage_percent" in payload:
            try:
                percent = int(float(payload["coverage_percent"]))
                return min(100, max(0, percent))
            except (TypeError, ValueError):
                return None

        coverage = payload.get("coverage")
        if isinstance(coverage, (int, float)):
            percent = int(float(coverage))
            return min(100, max(0, percent))
        if isinstance(coverage, dict):
            raw_percent = coverage.get("percent")
            if isinstance(raw_percent, (int, float)):
                percent = int(float(raw_percent))
                return min(100, max(0, percent))
        return None

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

    @staticmethod
    def _status_line_path(status_line: str) -> str:
        candidate = status_line[3:].strip()
        if " -> " in candidate:
            candidate = candidate.split(" -> ", maxsplit=1)[1].strip()
        return candidate

    def _dirty_worktree_paths(self) -> list[str]:
        if not self.patches.git_enabled:
            return []
        proc = self._run_git(["status", "--porcelain"], check=True)
        lines = [line for line in proc.stdout.splitlines() if line.strip()]
        if not lines:
            return []
        dirty_paths: list[str] = []
        for line in lines:
            if ".architect/" in line:
                continue
            if line.endswith(" architect.toml") or line.endswith("\tarchitect.toml"):
                continue
            path = self._status_line_path(line)
            if not path:
                continue
            dirty_paths.append(path)
        return dirty_paths

    def _ensure_clean_worktree(self) -> list[str]:
        dirty_paths = self._dirty_worktree_paths()
        if not dirty_paths:
            self._isolated_dirty_paths = []
            return []

        if self.config.workflow.dirty_worktree_mode == "isolate":
            self._isolated_dirty_paths = sorted(set(dirty_paths))
            return self._isolated_dirty_paths

        details = "\n".join(dirty_paths[:20])
        raise RuntimeError(
            "Refusing to run with dirty worktree. Commit/stash changes first.\n"
            f"Detected:\n{details}"
        )

    def _record_dirty_isolation(self, dirty_paths: list[str]) -> None:
        if not dirty_paths:
            return
        metrics = self.state.get_metrics()
        history = metrics.get("dirty_worktree_isolation", [])
        if not isinstance(history, list):
            history = []
        history.append(
            {
                "at": _utcnow_iso(),
                "count": len(dirty_paths),
                "paths": dirty_paths[:100],
            }
        )
        metrics["dirty_worktree_isolation"] = history[-20:]
        self.state.set_metrics(metrics)

    @staticmethod
    def _normalize_tools(allowed_tools: list[str] | None) -> list[str] | None:
        if not allowed_tools:
            return None
        normalized = sorted({str(tool).strip() for tool in allowed_tools if str(tool).strip()})
        unknown = [tool for tool in normalized if tool not in TOOL_POLICY_ALLOWLIST]
        if unknown:
            raise RuntimeError(
                "Tool policy rejected unknown tools: "
                + ", ".join(unknown)
            )
        return normalized

    def _command_available(self, executable: str) -> bool:
        if not executable.strip():
            return False
        check = subprocess.run(
            ["sh", "-lc", f"command -v {shlex.quote(executable)} >/dev/null 2>&1"],
            cwd=self.repo_root,
            text=True,
            capture_output=True,
        )
        return check.returncode == 0

    @staticmethod
    def _backend_probe_result(backend_name: str, *, command_available: bool) -> tuple[bool, str]:
        if backend_name == "codex":
            return command_available, "codex CLI binary not found in PATH."
        if backend_name == "claude":
            return command_available, "claude CLI binary not found in PATH."
        if backend_name in {"codex_sdk", "auto"}:
            sdk_available = importlib.util.find_spec("openai") is not None
            available = sdk_available or command_available
            return (
                available,
                "openai package missing and codex CLI binary not found in PATH.",
            )
        return False, f"Unsupported backend configured: {backend_name}"

    def _preflight_backend_checks(self) -> list[dict[str, Any]]:
        checks: list[dict[str, Any]] = []
        if self.config.backend.primary == self.config.backend.fallback:
            checks.append(
                {
                    "type": "backend",
                    "slot": "configuration",
                    "backend": self.config.backend.primary,
                    "ok": True,
                    "severity": "warning",
                    "message": (
                        "Primary and fallback backends are identical; failover is effectively "
                        "disabled."
                    ),
                }
            )

        runtime_backends = [
            getattr(agent, "backend", None)
            for agent in [*self.specialists.values(), self.supervisor_agent]
            if agent is not None
        ]
        requires_backend_probe = any(
            hasattr(backend, "primary_name") and hasattr(backend, "fallback_name")
            for backend in runtime_backends
            if backend is not None
        )
        if not requires_backend_probe:
            checks.append(
                {
                    "type": "backend",
                    "slot": "runtime",
                    "backend": "custom",
                    "ok": True,
                    "severity": "info",
                    "message": "Backend preflight probe skipped for custom runtime backend.",
                }
            )
            return checks

        slots = [
            ("primary", self.config.backend.primary),
            ("fallback", self.config.backend.fallback),
        ]
        slot_results: list[dict[str, Any]] = []
        for slot, backend_name in slots:
            if backend_name in {"codex", "codex_sdk", "auto"}:
                command_token = "codex"
            else:
                command_token = backend_name
            command_available = self._command_available(command_token)
            available, unavailable_reason = self._backend_probe_result(
                backend_name,
                command_available=command_available,
            )
            slot_results.append(
                {
                    "type": "backend",
                    "slot": slot,
                    "backend": backend_name,
                    "ok": available,
                    "reason": "" if available else unavailable_reason,
                }
            )

        any_available = any(bool(item.get("ok")) for item in slot_results)
        for item in slot_results:
            if item["ok"]:
                item["severity"] = "info"
                item["message"] = (
                    f"{item['slot']} backend '{item['backend']}' is available."
                )
            elif any_available:
                item["severity"] = "warning"
                item["message"] = (
                    f"{item['slot']} backend '{item['backend']}' is unavailable: {item['reason']}"
                )
            else:
                item["severity"] = "error"
                item["message"] = (
                    f"{item['slot']} backend '{item['backend']}' is unavailable: {item['reason']}"
                )
        checks.extend(slot_results)
        return checks

    def _preflight_command_checks(self) -> list[dict[str, Any]]:
        checks: list[dict[str, Any]] = []
        command_specs: list[tuple[str, str]] = []
        if self.config.workflow.auto_lint:
            command_specs.append(("lint", self.config.project.lint_command))
        type_command = self.config.project.type_check_command.strip()
        if type_command:
            command_specs.append(("type_check", type_command))
        if self.config.workflow.auto_test:
            command_specs.append(("test", self.config.project.test_command))

        for check_name, command in command_specs:
            try:
                tokens = shlex.split(command)
            except ValueError as exc:
                checks.append(
                    {
                        "type": "command",
                        "name": check_name,
                        "command": command,
                        "ok": False,
                        "severity": "error",
                        "message": f"{check_name} command could not be parsed: {exc}",
                    }
                )
                continue

            if not tokens:
                checks.append(
                    {
                        "type": "command",
                        "name": check_name,
                        "command": command,
                        "ok": False,
                        "severity": "error",
                        "message": f"{check_name} command is empty.",
                    }
                )
                continue

            executable = tokens[0]
            available = self._command_available(executable)
            checks.append(
                {
                    "type": "command",
                    "name": check_name,
                    "command": command,
                    "ok": available,
                    "severity": "info" if available else "error",
                    "message": (
                        f"{check_name} command executable '{executable}' is available."
                        if available
                        else (
                            f"{check_name} command executable '{executable}' "
                            "not found in PATH."
                        )
                    ),
                }
            )
        return checks

    def _record_preflight(self, payload: dict[str, Any]) -> None:
        metrics = self.state.get_metrics()
        history = metrics.get("preflight_history", [])
        if not isinstance(history, list):
            history = []
        history.append(payload)
        metrics["preflight"] = payload
        metrics["preflight_history"] = history[-30:]
        self.state.set_metrics(metrics)

        context = self.state.get_context()
        context["preflight"] = {
            "checked_at": payload.get("checked_at"),
            "ok": payload.get("ok", False),
            "errors": payload.get("errors", []),
            "warnings": payload.get("warnings", []),
        }
        self.state.set_context(context)

    def _run_preflight(self, *, resume: bool) -> dict[str, Any]:
        checks: list[dict[str, Any]] = []
        checks.extend(self._preflight_backend_checks())
        checks.extend(self._preflight_command_checks())
        if self._isolated_dirty_paths:
            checks.append(
                {
                    "type": "worktree",
                    "name": "dirty_isolation",
                    "ok": True,
                    "severity": "warning",
                    "message": (
                        "Dirty worktree isolation enabled; pre-existing changed files will be "
                        "excluded from patch staging."
                    ),
                    "paths": list(self._isolated_dirty_paths)[:100],
                }
            )

        errors = [item["message"] for item in checks if item.get("severity") == "error"]
        warnings = [item["message"] for item in checks if item.get("severity") == "warning"]
        payload = {
            "checked_at": _utcnow_iso(),
            "ok": not errors,
            "resume": resume,
            "errors": errors,
            "warnings": warnings,
            "checks": checks,
        }
        self._record_preflight(payload)
        if errors:
            raise RuntimeError(
                "Runtime preflight failed:\n" + "\n".join(f"- {item}" for item in errors)
            )
        return payload

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

    def _increment_metric(self, key: str, value: int = 1) -> None:
        metrics = self.state.get_metrics()
        metrics[key] = int(metrics.get(key, 0)) + value
        self.state.set_metrics(metrics)

    def _upsert_run_record(self, run_id: str, updates: dict[str, Any]) -> None:
        def _updater(payload: Any) -> dict[str, Any]:
            runs = payload if isinstance(payload, dict) else {}
            run = runs.get(run_id, {})
            if not isinstance(run, dict):
                run = {}
            run.update(updates)
            runs[run_id] = run
            return runs

        self.state.update_json("runs", _updater, default={})

    def _acquire_run_lease(self, run_id: str, *, resume: bool) -> None:
        now_epoch = time.time()
        lease_ttl = max(30.0, float(self.config.backend.timeout_seconds) * 2.0)
        expires_epoch = now_epoch + lease_ttl
        now_iso = _utcnow_iso()

        def _updater(payload: Any) -> dict[str, Any]:
            leases = payload if isinstance(payload, dict) else {}
            active = leases.get("active")
            if isinstance(active, dict):
                active_run = str(active.get("run_id", ""))
                active_expiry = float(active.get("expires_epoch", 0))
                if active_run and active_run != run_id and active_expiry > now_epoch and not resume:
                    raise ArchitectStateError(
                        "Another run lease is still active. Use `arch resume --goal ...` or wait."
                    )
            leases["active"] = {
                "run_id": run_id,
                "heartbeat_at": now_iso,
                "expires_epoch": expires_epoch,
            }
            return leases

        self.state.update_json("leases", _updater, default={})
        self._upsert_run_record(
            run_id,
            {
                "run_id": run_id,
                "lease_acquired_at": now_iso,
                "heartbeat_at": now_iso,
                "status": "in_progress",
            },
        )

    def _heartbeat_run(self, run_id: str, *, task_id: str | None = None) -> None:
        now_epoch = time.time()
        lease_ttl = max(30.0, float(self.config.backend.timeout_seconds) * 2.0)
        expires_epoch = now_epoch + lease_ttl
        now_iso = _utcnow_iso()

        def _updater(payload: Any) -> dict[str, Any]:
            leases = payload if isinstance(payload, dict) else {}
            active = leases.get("active")
            if not isinstance(active, dict) or str(active.get("run_id", "")) != run_id:
                active = {"run_id": run_id}
            active["heartbeat_at"] = now_iso
            active["expires_epoch"] = expires_epoch
            if task_id:
                active["task_id"] = task_id
            leases["active"] = active
            return leases

        self.state.update_json("leases", _updater, default={})
        run_updates: dict[str, Any] = {"heartbeat_at": now_iso}
        if task_id:
            run_updates["active_task_id"] = task_id
        self._upsert_run_record(run_id, run_updates)

    def _release_run_lease(self, run_id: str, *, status: str) -> None:
        now_iso = _utcnow_iso()

        def _updater(payload: Any) -> dict[str, Any]:
            leases = payload if isinstance(payload, dict) else {}
            active = leases.get("active")
            if isinstance(active, dict) and str(active.get("run_id", "")) == run_id:
                leases["active"] = None
            return leases

        self.state.update_json("leases", _updater, default={})
        self._upsert_run_record(
            run_id,
            {
                "status": status,
                "ended_at": now_iso,
            },
        )

    def _record_decision(self, task: WorkTask, response: SpecialistResponse) -> None:
        decision = {
            "id": f"dec-{task.id}-{uuid4().hex[:8]}",
            "topic": task.type,
            "decided_by": task.assigned_to,
            "approved_by": "supervisor",
            "decision": response.content[:4000],
            "rationale": f"Output from {task.assigned_to} for {task.id}",
            "task_id": task.id,
            "task_status": task.status,
            "patch_id": task.patch_id,
            "attempt": task.attempt,
            "evidence": {
                "allowed_tools": self._allowed_tools_for_task(task),
                "metadata": response.metadata,
            },
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
        command_text = command.strip()
        if not command_text:
            return {
                "type": "command",
                "command": command,
                "exit_code": 1,
                "stdout_tail": "",
                "stderr_tail": "Command is empty.",
                "used_shell": False,
            }

        used_shell = bool(SHELL_REQUIRED_PATTERN.search(command_text))
        command_payload: str | list[str] = command_text
        if not used_shell:
            try:
                command_payload = shlex.split(command_text)
            except ValueError:
                used_shell = True
                command_payload = command_text

        proc = subprocess.run(
            command_payload,
            cwd=self.repo_root,
            shell=used_shell,
            text=True,
            capture_output=True,
        )
        return {
            "type": "command",
            "command": command,
            "exit_code": proc.returncode,
            "stdout_tail": proc.stdout.strip()[-1000:],
            "stderr_tail": proc.stderr.strip()[-1000:],
            "used_shell": used_shell,
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
        test_touched = any(self._is_test_path(path) for path in run_patch_files)
        if test_touched:
            return True, ""
        return False, (
            "Guardrail require_tests_for failed: source files changed without matching tests. "
            f"Patterns={guarded_patterns}"
        )

    def _is_guarded_source_path(self, path: str) -> bool:
        normalized = path.replace("\\", "/")
        patterns = list(self.config.guardrails.require_tests_for)
        if patterns:
            return any(fnmatch.fnmatch(normalized, pattern) for pattern in patterns)
        return (
            not self._is_internal_runtime_path(path)
            and not self._is_test_path(path)
            and not self._is_documentation_evidence_path(path)
        )

    @staticmethod
    def _is_test_path(path: str) -> bool:
        normalized = path.replace("\\", "/").lower()
        name = normalized.rsplit("/", maxsplit=1)[-1]
        segments = set(normalized.split("/"))
        if {"tests", "test", "__tests__", "spec", "specs"} & segments:
            return True
        if name.startswith("test_") or name.endswith("_test.py"):
            return True
        return any(
            name.endswith(suffix)
            for suffix in (
                ".test.js",
                ".test.jsx",
                ".test.ts",
                ".test.tsx",
                ".spec.js",
                ".spec.jsx",
                ".spec.ts",
                ".spec.tsx",
            )
        )

    @staticmethod
    def _is_documentation_path(path: str) -> bool:
        normalized = path.replace("\\", "/").lower()
        name = normalized.rsplit("/", maxsplit=1)[-1]
        if name.startswith("readme") or "changelog" in name:
            return True
        if any(token in normalized.split("/") for token in ("docs", "doc", "documentation")):
            return True
        return name.endswith((".md", ".rst", ".adoc"))

    def _matches_any_pattern(self, path: str, patterns: list[str]) -> bool:
        normalized = path.replace("\\", "/")
        for pattern in patterns:
            if fnmatch.fnmatch(normalized, pattern):
                return True
        return False

    def _is_documentation_evidence_path(self, path: str) -> bool:
        if self._matches_any_pattern(path, list(self.config.workflow.review_docs_patterns)):
            return True
        return self._is_documentation_path(path)

    def _is_changelog_evidence_path(self, path: str) -> bool:
        if self._matches_any_pattern(path, list(self.config.workflow.review_changelog_patterns)):
            return True
        return "changelog" in path.lower()

    @staticmethod
    def _is_internal_runtime_path(path: str) -> bool:
        normalized = path.replace("\\", "/").lower()
        return normalized.startswith(".architect/")

    def _plan_quality_signals(self, content: str) -> dict[str, Any]:
        lower = content.lower()
        steps = self._extract_plan_steps(content)
        return {
            "steps": len(steps),
            "has_interface": any(token in lower for token in ("interface", "boundary", "api")),
            "has_risks": any(token in lower for token in ("risk", "mitigation", "tradeoff")),
            "has_analysis": any(token in lower for token in ("analysis", "problem", "context")),
            "has_milestones": any(token in lower for token in ("milestone", "phase", "step")),
        }

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
            quality = self._plan_quality_signals(content)
            artifacts.append({"type": "planning_quality", **quality})
            passed = bool(content) and quality["steps"] >= 1
            if passed:
                required_flags = (
                    quality["has_interface"],
                    quality["has_risks"],
                    quality["has_analysis"],
                    quality["has_milestones"],
                )
                # Allow either full structured plan sections or a dense step plan.
                if not all(required_flags) and quality["steps"] < 2:
                    passed = False
                    reason = (
                        "Planning output missing required quality signals "
                        "(interfaces, risks, analysis, milestones)."
                    )
            if not passed:
                reason = reason or "Planning output must include at least one actionable step."

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
            major_threshold = int(self.config.workflow.review_max_major_findings)
            artifacts.append(
                {
                    "type": "critic_threshold",
                    "name": "review_max_major_findings",
                    "threshold": major_threshold,
                    "actual": findings["MAJOR"],
                }
            )
            if passed and major_threshold >= 0 and findings["MAJOR"] > major_threshold:
                passed = False
                reason = (
                    "Critic major threshold failed: "
                    f"{findings['MAJOR']} major findings (max {major_threshold})."
                )
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
            source_files = [
                p
                for p in run_patch_files
                if self._is_guarded_source_path(p)
            ]
            doc_files = [p for p in run_patch_files if self._is_documentation_evidence_path(p)]
            changelog_files = [p for p in run_patch_files if self._is_changelog_evidence_path(p)]
            artifacts.append(
                {
                    "type": "review_file_evidence",
                    "source_files": source_files,
                    "doc_files": doc_files,
                    "changelog_files": changelog_files,
                }
            )
            if (
                passed
                and self.config.workflow.review_require_docs_update
                and source_files
                and not doc_files
            ):
                passed = False
                reason = (
                    "Review gate requires documentation file updates for source code changes."
                )
            if (
                passed
                and self.config.workflow.review_require_changelog_update
                and source_files
                and not changelog_files
            ):
                passed = False
                reason = "Review gate requires changelog update for source code changes."

        elif task.type == "document":
            passed = bool(content)
            if not passed:
                reason = "Documentation output is empty."
            if passed:
                source_touched = any(
                    self._is_guarded_source_path(path)
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
        ready = self._ready_tasks(tasks)
        if not ready:
            return None
        return ready[0]

    def _ready_tasks(self, tasks: list[WorkTask]) -> list[WorkTask]:
        task_by_id = {task.id: task for task in tasks}
        ready: list[WorkTask] = []
        for task in tasks:
            if task.status != "pending":
                continue
            if all(
                task_by_id.get(dep_id) and task_by_id[dep_id].status == "completed"
                for dep_id in task.depends_on
            ):
                ready.append(task)
        return ready

    def _allowed_tools_for_task(self, task: WorkTask) -> list[str] | None:
        if task.allowed_tools:
            return self._normalize_tools(task.allowed_tools)
        if task.type == "implement":
            return self._normalize_tools(
                ["read_file", "write_file", "edit_file", "run_command", "search"]
            )
        if task.type == "test":
            return self._normalize_tools(["read_file", "run_command"])
        if task.type == "review":
            return self._normalize_tools(["read_file", "run_command", "search"])
        if task.type == "document":
            return self._normalize_tools(["read_file", "write_file", "edit_file", "search"])
        return None

    async def _run_specialist(
        self,
        task: WorkTask,
        goal: str,
        *,
        working_directory: Path | None = None,
    ) -> SpecialistResponse:
        specialist = self.specialists.get(task.assigned_to)
        if specialist is None:
            raise RuntimeError(f"No specialist registered for role '{task.assigned_to}'.")
        allowed_tools = self._allowed_tools_for_task(task)
        context = {"goal": goal, "task": asdict(task)}
        if working_directory is not None:
            context["_working_directory"] = str(working_directory)
        return await specialist.run(
            instruction=task.description,
            context=context,
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
        if self.config.workflow.fallback_artifact_mode == "tracked":
            target_dir = self.repo_root / self.config.workflow.tracked_fallback_dir / run_id
        else:
            target_dir = self.repo_root / ".architect" / "runs" / run_id
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
        planner = self.specialists.get("planner")
        critic = self.specialists.get("critic")
        coder = self.specialists.get("coder")
        if coder is None:
            return None

        critic_clarification = critic_output
        if critic is not None:
            critic_response = await critic.run(
                instruction=(
                    "Clarify blocker findings with concise remediation guidance.\n\n"
                    f"Review output:\n{critic_output[:4000]}"
                ),
                context={"goal": goal, "review_task": asdict(review_task), "phase": "conflict"},
            )
            critic_clarification = critic_response.content.strip() or critic_output
            self.state.add_decision(
                {
                    "id": f"dec-conflict-critic-{review_task.id}-{uuid4().hex[:8]}",
                    "topic": "conflict_resolution",
                    "decided_by": "critic",
                    "approved_by": "supervisor",
                    "decision": critic_clarification[:4000],
                    "rationale": "Critic clarification for remediation planning.",
                    "created_at": _utcnow_iso(),
                }
            )

        planner_recommendation = ""
        if planner is not None:
            planner_response = await planner.run(
                instruction=(
                    "Given critic blockers, propose remediation alternatives with tradeoffs. "
                    "Return a concise selected recommendation."
                ),
                context={
                    "goal": goal,
                    "review_task": asdict(review_task),
                    "critic_clarification": critic_clarification[:4000],
                },
            )
            planner_recommendation = planner_response.content.strip()
            self.state.add_decision(
                {
                    "id": f"dec-conflict-planner-{review_task.id}-{uuid4().hex[:8]}",
                    "topic": "conflict_resolution",
                    "decided_by": "planner",
                    "approved_by": "supervisor",
                    "decision": planner_recommendation[:4000],
                    "rationale": "Planner alternatives for blocker remediation.",
                    "created_at": _utcnow_iso(),
                }
            )

        supervisor_decision = planner_recommendation or critic_clarification
        if self.supervisor_agent is not None:
            supervisor_response = await self.supervisor_agent.run(
                instruction=(
                    "Adjudicate conflict-resolution inputs and pick one remediation strategy. "
                    "Respond with a concise decision and rationale."
                ),
                context={
                    "goal": goal,
                    "review_task": asdict(review_task),
                    "critic_clarification": critic_clarification[:4000],
                    "planner_recommendation": planner_recommendation[:4000],
                },
            )
            supervisor_decision = supervisor_response.content.strip() or supervisor_decision
            self.state.add_decision(
                {
                    "id": f"dec-conflict-supervisor-{review_task.id}-{uuid4().hex[:8]}",
                    "topic": "conflict_resolution",
                    "decided_by": "supervisor",
                    "approved_by": "supervisor",
                    "decision": supervisor_decision[:4000],
                    "rationale": "Supervisor adjudication over specialist conflict inputs.",
                    "created_at": _utcnow_iso(),
                }
            )

        response = await coder.run(
            instruction=(
                "Apply remediation selected by supervisor to resolve review blockers. "
                "Return concise actions and confirmed fixes.\n\n"
                f"Supervisor decision:\n{supervisor_decision[:3000]}\n\n"
                f"Critic clarification:\n{critic_clarification[:3000]}"
            ),
            context={
                "goal": goal,
                "review_task": asdict(review_task),
                "remediation": True,
                "planner_recommendation": planner_recommendation[:4000],
                "supervisor_decision": supervisor_decision[:4000],
            },
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
            fallback_mode=self.config.workflow.fallback_artifact_mode,
            max_files=self.config.guardrails.max_file_changes_per_patch,
            forbidden_paths=list(self.config.guardrails.forbidden_paths),
            exclude_paths=list(self._isolated_dirty_paths),
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
        seen_ids: set[str] = set()
        for item in tasks:
            if not isinstance(item, dict):
                continue
            task_id = str(item.get("id", ""))
            if not (
                task_id.startswith("task-modify-")
                or task_id.startswith("task-retry-")
            ):
                continue
            status = str(item.get("status", "pending"))
            if status not in {"pending", "in_progress", "failed"}:
                continue
            if task_id in seen_ids:
                continue
            seen_ids.add(task_id)
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
        pending.sort(key=lambda task: task.created_at)
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

        dirty_paths: list[str] = []
        if not resume:
            dirty_paths = self._ensure_clean_worktree()
            self._record_dirty_isolation(dirty_paths)
        else:
            self._isolated_dirty_paths = []

        preflight = self._run_preflight(resume=resume)

        pending_modify_tasks = self._load_pending_modify_tasks()
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
            session = context.get("session", {})
            if isinstance(session, dict):
                base_branch = str(session.get("base_branch") or self.patches.current_branch())
            else:
                base_branch = str(context.get("active_branch") or self.patches.current_branch())
            run_branch = self.patches.current_branch()
            tasks = existing_tasks
        else:
            started_at = now
            run_id = f"run-{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}-{uuid4().hex[:8]}"
            base_branch = self.patches.current_branch()
            run_branch = base_branch
            if (
                self.patches.git_enabled
                and self.config.workflow.branch_strategy == "auxiliary_branches"
            ):
                run_branch = f"architect/{run_id}"
                self.patches.create_branch(run_branch, start_point=base_branch)
            plan_task = WorkTask(
                id="task-plan-001",
                type="plan",
                assigned_to="planner",
                description=f"Design a technical approach for: {goal}",
            )
            tasks = [plan_task]

        self._acquire_run_lease(run_id, resume=resume)
        self._upsert_run_record(
            run_id,
            {
                "run_id": run_id,
                "goal": goal,
                "status": "in_progress",
                "started_at": started_at,
                "base_branch": base_branch,
                "active_branch": run_branch,
                "branch_strategy": self.config.workflow.branch_strategy,
                "max_parallel_tasks": int(self.config.workflow.max_parallel_tasks),
            },
        )

        context_payload = {
            "goal": goal,
            "phase": "planning",
            "status": "in_progress",
            "active_branch": run_branch,
            "started_at": started_at,
            "paused": False,
            "current_run_id": run_id,
            "preflight": {
                "checked_at": preflight["checked_at"],
                "ok": preflight["ok"],
                "errors": preflight["errors"],
                "warnings": preflight["warnings"],
            },
            "dirty_worktree": {
                "mode": self.config.workflow.dirty_worktree_mode,
                "isolated_paths": list(dirty_paths),
            },
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
        self._heartbeat_run(run_id)

        supervisor_steps = await self._run_supervisor_decomposition(goal)
        run_patch_files: list[str] = []
        completed_tasks = 0
        conflict_cycles = 0
        max_conflict_cycles = max(0, int(self.config.workflow.max_conflict_cycles))
        max_attempts = max(1, int(self.config.workflow.task_max_attempts))
        retry_backoff = max(0.0, float(self.config.workflow.task_retry_backoff_seconds))
        max_parallel = max(1, int(self.config.workflow.max_parallel_tasks))

        try:
            while True:
                ready_tasks = self._ready_tasks(tasks)
                if not ready_tasks:
                    break

                batch: list[WorkTask]
                if max_parallel <= 1:
                    batch = [ready_tasks[0]]
                else:
                    first_type = ready_tasks[0].type
                    same_type_ready = [task for task in ready_tasks if task.type == first_type]
                    # Git worktree mutation remains serial for now; non-mutating tasks can batch.
                    if self.patches.git_enabled and first_type in {"implement", "document"}:
                        batch = [same_type_ready[0]]
                    else:
                        batch = same_type_ready[:max_parallel]

                for ready_task in batch:
                    self._update_task_status(tasks, ready_task.id, "in_progress")
                    self._heartbeat_run(run_id, task_id=ready_task.id)
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

                    for attempt in range(1, max_attempts + 1):
                        ready_task.attempt = attempt
                        if attempt > 1 and retry_backoff > 0:
                            delay = retry_backoff * (2 ** (attempt - 2))
                            await asyncio.sleep(delay)
                            self._increment_metric("task_retry_count")

                        response = await self._run_specialist(ready_task, goal)
                        ready_task.output_summary = response.content[:4000]
                        artifact_path = self._write_task_artifact(run_id, ready_task, response)

                        if ready_task.type == "plan" and len(tasks) == 1:
                            plan_steps = self._extract_plan_steps(response.content)
                            if not plan_steps:
                                plan_steps = supervisor_steps
                            tasks = self._create_task_graph(goal, plan_steps, pending_modify_tasks)
                            tasks[0].attempt = ready_task.attempt
                            tasks[0].output_summary = ready_task.output_summary
                            self._persist_tasks(tasks)
                            ready_task = tasks[0]

                        if ready_task.type in {"implement", "document"}:
                            if self.patches.git_enabled:
                                created_patch = self.patches.create_task_patch_from_worktree(
                                    subject=f"architect: {ready_task.id}",
                                    body=(
                                        f"Run: {run_id}\nTask: {ready_task.id}\n\n"
                                        f"{response.content[:2000]}"
                                    ),
                                    task_id=ready_task.id,
                                    run_id=run_id,
                                    fallback_file=self._tracked_fallback_patch_path(
                                        run_id, ready_task
                                    ),
                                    fallback_content=self._tracked_fallback_patch_content(
                                        run_id,
                                        ready_task,
                                        response.content,
                                    ),
                                    fallback_mode=self.config.workflow.fallback_artifact_mode,
                                    max_files=self.config.guardrails.max_file_changes_per_patch,
                                    forbidden_paths=list(self.config.guardrails.forbidden_paths),
                                    exclude_paths=list(self._isolated_dirty_paths),
                                )
                            else:
                                local_path = artifact_path.relative_to(self.repo_root)
                                created_patch = self.patches.record_local_patch(
                                    subject=f"architect: {ready_task.id}",
                                    task_id=ready_task.id,
                                    run_id=run_id,
                                    files_changed=[str(local_path).replace("\\", "/")],
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

                        if (
                            ready_task.type == "plan"
                            and gate["passed"]
                            and self.config.workflow.plan_requires_critic
                        ):
                            critic = self.specialists.get("critic")
                            if critic is not None:
                                plan_review = await critic.run(
                                    instruction=(
                                        "Review the following plan for design clarity, interface "
                                        "completeness, milestone quality, and risk handling. "
                                        "Use BLOCKER|MAJOR|MINOR|SUGGESTION labels.\n\n"
                                        f"{response.content[:4000]}"
                                    ),
                                    context={"goal": goal, "task": asdict(ready_task)},
                                )
                                plan_findings = self._parse_review_findings(plan_review.content)
                                gate["artifacts"].append(
                                    {"type": "plan_critic_findings", "counts": plan_findings}
                                )
                                self.state.add_decision(
                                    {
                                        "id": f"dec-plan-critic-{ready_task.id}-{uuid4().hex[:8]}",
                                        "topic": "plan_review",
                                        "decided_by": "critic",
                                        "approved_by": "supervisor",
                                        "decision": plan_review.content[:4000],
                                        "rationale": "Critic review for planning quality gate.",
                                        "created_at": _utcnow_iso(),
                                    }
                                )
                                if plan_findings["BLOCKER"] > 0:
                                    gate["passed"] = False
                                    gate["reason"] = (
                                        "Planning gate failed due to critic blockers: "
                                        f"{plan_findings['BLOCKER']}"
                                    )

                        self._record_gate_result(gate)
                        if gate["passed"]:
                            break

                        if attempt < max_attempts:
                            await self._run_replan(ready_task, gate["reason"], goal)
                            self._increment_metric("replan_count")
                            if (
                                ready_task.type == "review"
                                and response.content.strip()
                                and conflict_cycles < max_conflict_cycles
                            ):
                                conflict_cycles += 1
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
                        raise RuntimeError(
                            f"Task execution failed unexpectedly for {ready_task.id}"
                        )
                    if not gate["passed"]:
                        self._update_task_status(
                            tasks,
                            ready_task.id,
                            "failed",
                            reason=gate["reason"],
                        )
                        failure_checkpoint = self.patches.create_checkpoint(
                            f"{run_id}-failed-{ready_task.id}"
                        )
                        self.state.add_checkpoint(
                            {
                                "id": failure_checkpoint,
                                "created_at": _utcnow_iso(),
                                "goal": goal,
                                "run_id": run_id,
                                "active_branch": self.patches.current_branch(),
                                "failure_task_id": ready_task.id,
                                "failure_reason": gate["reason"],
                            }
                        )
                        context = self.state.get_context()
                        context["status"] = "failed"
                        context["phase"] = ready_task.type
                        context["ended_at"] = _utcnow_iso()
                        context["last_failure_checkpoint"] = failure_checkpoint
                        context = self._append_phase_history(context, ready_task.type, "failed")
                        self.state.set_context(context)
                        self._upsert_run_record(
                            run_id,
                            {
                                "status": "failed",
                                "failed_task_id": ready_task.id,
                                "failure_reason": gate["reason"],
                                "last_failure_checkpoint": failure_checkpoint,
                            },
                        )
                        self._release_run_lease(run_id, status="failed")
                        raise RuntimeError(
                            "Quality gate failed: "
                            f"{gate['name']} ({ready_task.id}) - {gate['reason']}"
                        )

                    self._record_decision(ready_task, response)
                    self._update_task_status(tasks, ready_task.id, "completed")
                    completed_tasks += 1
                    self._heartbeat_run(run_id)
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
                metrics["scheduler_parallelism"] = max_parallel
                metrics["conflict_resolution_cycles"] = conflict_cycles
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
            self._upsert_run_record(
                run_id,
                {
                    "status": "complete",
                    "ended_at": ended_at,
                    "checkpoint_id": checkpoint_id,
                    "completed_tasks": completed_tasks,
                    "total_tasks": len(tasks),
                },
            )
            self._release_run_lease(run_id, status="complete")

            return RunSummary(
                goal=goal,
                run_id=run_id,
                started_at=started_at,
                ended_at=ended_at,
                total_tasks=len(tasks),
                completed_tasks=completed_tasks,
                checkpoint_id=checkpoint_id,
            )
        except Exception:
            # In case of unexpected runtime errors, release stale lease.
            context = self.state.get_context()
            if context.get("current_run_id") == run_id:
                self._release_run_lease(run_id, status="failed")
            raise

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
            "runs": self.state.get_runs(),
            "leases": self.state.get_leases(),
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
