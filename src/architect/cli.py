from __future__ import annotations

import asyncio
import fnmatch
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import click

from architect.backends import (
    ClaudeCodeBackend,
    CodexBackend,
    ResilientBackend,
    RetryPolicy,
)
from architect.config import ArchitectConfig, BackendName, load_config, save_config
from architect.specialists import (
    CoderAgent,
    CriticAgent,
    DocumenterAgent,
    PlannerAgent,
    SpecialistAgent,
    SupervisorAgent,
    TesterAgent,
)
from architect.state import GitNotesStore, PatchStackManager
from architect.state.git_notes import ArchitectStateError
from architect.supervisor import Supervisor


@dataclass(slots=True)
class Runtime:
    repo_root: Path
    config_path: Path
    config: ArchitectConfig
    state: GitNotesStore
    patches: PatchStackManager
    supervisor: Supervisor


def _resolve_config_path(repo_root: Path, config_value: str) -> Path:
    config_path = Path(config_value)
    if not config_path.is_absolute():
        config_path = repo_root / config_path
    return config_path.resolve()


def _build_single_backend(
    backend_name: BackendName, repo_root: Path
) -> CodexBackend | ClaudeCodeBackend:
    if backend_name == "codex":
        return CodexBackend(working_directory=repo_root)
    return ClaudeCodeBackend(working_directory=repo_root)


def _record_backend_event(state: GitNotesStore, event: dict[str, Any]) -> None:
    metrics = state.get_metrics()
    events = metrics.get("backend_events", [])
    if not isinstance(events, list):
        events = []
    event_payload = dict(event)
    event_payload["at"] = datetime.now(UTC).replace(microsecond=0).isoformat()
    events.append(event_payload)
    metrics["backend_events"] = events[-200:]

    if event.get("event") == "backend_retry":
        metrics["backend_retry_count"] = int(metrics.get("backend_retry_count", 0)) + 1
    if event.get("event") == "backend_fallback_success":
        metrics["backend_fallback_count"] = int(metrics.get("backend_fallback_count", 0)) + 1

    state.set_metrics(metrics)


def _build_backend(
    config: ArchitectConfig, repo_root: Path, state: GitNotesStore
) -> ResilientBackend:
    primary_name = config.backend.primary
    fallback_name = config.backend.fallback
    primary_backend = _build_single_backend(primary_name, repo_root)
    fallback_backend = _build_single_backend(fallback_name, repo_root)
    policy = RetryPolicy(
        max_retries=max(0, int(config.backend.max_retries)),
        backoff_seconds=max(0.0, float(config.backend.retry_backoff_seconds)),
        timeout_seconds=max(5.0, float(config.backend.timeout_seconds)),
    )
    return ResilientBackend(
        primary_name=primary_name,
        primary_backend=primary_backend,
        fallback_name=fallback_name,
        fallback_backend=fallback_backend,
        retry_policy=policy,
        event_hook=lambda event: _record_backend_event(state, event),
    )


def _build_specialists(
    backend: ResilientBackend, config: ArchitectConfig
) -> dict[str, SpecialistAgent]:
    return {
        "planner": PlannerAgent(backend, model=config.agents.specialist_model),
        "coder": CoderAgent(backend, model=config.agents.specialist_model),
        "tester": TesterAgent(backend, model=config.agents.specialist_model),
        "critic": CriticAgent(backend, model=config.agents.specialist_model),
        "documenter": DocumenterAgent(backend, model=config.agents.specialist_model),
    }


def _load_runtime(repo_root: Path, config_path: Path) -> Runtime:
    config = load_config(config_path)
    state = GitNotesStore(
        repo_root,
        backend_mode=config.state.backend,
        branch_ref=config.state.branch_ref,
    )
    patches = PatchStackManager(repo_root, state_store=state)
    backend = _build_backend(config, repo_root, state)
    supervisor_agent = SupervisorAgent(backend, model=config.agents.supervisor_model)
    supervisor = Supervisor(
        state_store=state,
        patch_manager=patches,
        specialists=_build_specialists(backend, config),
        supervisor_agent=supervisor_agent,
        config=config,
        repo_root=repo_root,
    )
    return Runtime(
        repo_root=repo_root,
        config_path=config_path,
        config=config,
        state=state,
        patches=patches,
        supervisor=supervisor,
    )


def _record_patch_metric(state: GitNotesStore, metric_key: str, patch_hash: str) -> None:
    metrics = state.get_metrics()
    values = metrics.get(metric_key, [])
    if not isinstance(values, list):
        values = []
    if patch_hash not in values:
        values.append(patch_hash)
    metrics[metric_key] = values
    state.set_metrics(metrics)


def _matches_forbidden_path(path: str, patterns: list[str]) -> str | None:
    normalized = path.replace("\\", "/")
    for pattern in patterns:
        if fnmatch.fnmatch(normalized, pattern):
            return pattern
    return None


def _ensure_patch_allowed(patch_files: list[str], config: ArchitectConfig) -> None:
    for file_path in patch_files:
        matched = _matches_forbidden_path(file_path, config.guardrails.forbidden_paths)
        if matched:
            raise click.ClickException(
                f"Patch touches forbidden path '{file_path}' (matched guardrail '{matched}')."
            )


def _patch_metadata(state: GitNotesStore, commit_hash: str) -> dict[str, Any]:
    metrics = state.get_metrics()
    stack = metrics.get("patch_stack", [])
    if not isinstance(stack, list):
        return {}
    for item in stack:
        if isinstance(item, dict) and item.get("commit_hash") == commit_hash:
            return item
    return {}


def _session_commit_scope(state: GitNotesStore) -> set[str]:
    context = state.get_context()
    run_id = context.get("current_run_id")
    session = context.get("session", {})
    commit_hashes: set[str] = set()
    if isinstance(session, dict):
        patch_stack = session.get("patch_stack", [])
        if isinstance(patch_stack, list):
            for item in patch_stack:
                if isinstance(item, dict):
                    commit_hash = item.get("commit_hash")
                    if isinstance(commit_hash, str):
                        commit_hashes.add(commit_hash)
    if commit_hashes:
        return commit_hashes

    metrics = state.get_metrics()
    patch_stack = metrics.get("patch_stack", [])
    if isinstance(patch_stack, list):
        for item in patch_stack:
            if not isinstance(item, dict):
                continue
            if run_id and item.get("run_id") != run_id:
                continue
            commit_hash = item.get("commit_hash")
            if isinstance(commit_hash, str):
                commit_hashes.add(commit_hash)
    return commit_hashes


@click.group()
def cli() -> None:
    """Architect CLI."""


@cli.command("init")
@click.option("--backend", type=click.Choice(["codex", "claude"]), default=None)
@click.option("--config", "config_value", default="architect.toml", show_default=True)
def init_command(backend: str | None, config_value: str) -> None:
    repo_root = Path.cwd().resolve()
    config_path = _resolve_config_path(repo_root, config_value)
    config = load_config(config_path)
    if backend:
        config.backend.primary = backend  # type: ignore[assignment]
    save_config(config_path, config)

    (repo_root / ".architect").mkdir(parents=True, exist_ok=True)

    state = GitNotesStore(
        repo_root,
        backend_mode=config.state.backend,
        branch_ref=config.state.branch_ref,
    )
    patches = PatchStackManager(repo_root, state_store=state)
    if not state.get_context():
        state.set_context(
            {
                "goal": "",
                "phase": "idle",
                "status": "ready",
                "active_branch": patches.current_branch(),
                "paused": False,
                "session": {
                    "run_id": None,
                    "phase_history": [],
                    "patch_stack": [],
                },
            }
        )

    click.echo(f"Initialized Architect in {repo_root}")
    click.echo(f"Config: {config_path}")
    click.echo(f"Backend: {config.backend.primary}")
    click.echo(
        f"State backend: {state.backend_mode}"
    )


@cli.command("run")
@click.argument("goal")
@click.option("--config", "config_value", default="architect.toml", show_default=True)
def run_command(goal: str, config_value: str) -> None:
    repo_root = Path.cwd().resolve()
    runtime = _load_runtime(repo_root, _resolve_config_path(repo_root, config_value))
    try:
        summary = asyncio.run(runtime.supervisor.run(goal))
    except (RuntimeError, ArchitectStateError) as exc:
        raise click.ClickException(str(exc)) from exc

    click.echo(f"Goal complete: {summary.goal}")
    click.echo(f"Run ID: {summary.run_id}")
    click.echo(f"Tasks: {summary.completed_tasks}/{summary.total_tasks}")
    if summary.checkpoint_id:
        click.echo(f"Checkpoint: {summary.checkpoint_id}")


@cli.command("pause")
@click.option("--config", "config_value", default="architect.toml", show_default=True)
def pause_command(config_value: str) -> None:
    repo_root = Path.cwd().resolve()
    runtime = _load_runtime(repo_root, _resolve_config_path(repo_root, config_value))
    runtime.supervisor.pause()
    click.echo("Workflow paused.")


@cli.command("resume")
@click.option("--from-checkpoint", "checkpoint_id", default=None)
@click.option("--goal", "goal_override", default=None)
@click.option("--config", "config_value", default="architect.toml", show_default=True)
def resume_command(checkpoint_id: str | None, goal_override: str | None, config_value: str) -> None:
    repo_root = Path.cwd().resolve()
    runtime = _load_runtime(repo_root, _resolve_config_path(repo_root, config_value))

    if checkpoint_id:
        try:
            branch_name = runtime.patches.rollback(checkpoint_id)
        except ArchitectStateError as exc:
            raise click.ClickException(str(exc)) from exc
        click.echo(f"Restored checkpoint on branch: {branch_name}")

    runtime.supervisor.resume()
    context = runtime.state.get_context()
    goal = goal_override or str(context.get("goal") or "").strip()
    if not goal:
        click.echo("Workflow resumed.")
        return

    try:
        summary = asyncio.run(runtime.supervisor.run(goal, resume=True))
    except (RuntimeError, ArchitectStateError) as exc:
        raise click.ClickException(str(exc)) from exc

    click.echo(f"Goal complete: {summary.goal}")
    click.echo(f"Run ID: {summary.run_id}")
    click.echo(f"Tasks: {summary.completed_tasks}/{summary.total_tasks}")
    if summary.checkpoint_id:
        click.echo(f"Checkpoint: {summary.checkpoint_id}")


@cli.command("status")
@click.option("--verbose", is_flag=True, default=False)
@click.option("--config", "config_value", default="architect.toml", show_default=True)
def status_command(verbose: bool, config_value: str) -> None:
    repo_root = Path.cwd().resolve()
    runtime = _load_runtime(repo_root, _resolve_config_path(repo_root, config_value))
    payload = runtime.supervisor.status(verbose=verbose)
    click.echo(json.dumps(payload, ensure_ascii=False, indent=2))


@cli.command("review")
@click.option("--patch", "patch_ref", default=None)
@click.option("--all", "include_all", is_flag=True, default=False)
@click.option("--config", "config_value", default="architect.toml", show_default=True)
def review_command(patch_ref: str | None, include_all: bool, config_value: str) -> None:
    repo_root = Path.cwd().resolve()
    runtime = _load_runtime(repo_root, _resolve_config_path(repo_root, config_value))
    scope = None if include_all else (_session_commit_scope(runtime.state) or None)
    patches = runtime.patches.list_patches(commit_hashes=scope)
    if not patches:
        click.echo("No patches available.")
        return

    if patch_ref:
        patch = runtime.patches.resolve_patch(patch_ref, commit_hashes=scope)
        if patch is None:
            raise click.ClickException(f"Patch not found: {patch_ref}")
        description = runtime.patches.describe_patch(patch.patch_id)
        payload = {
            "patch": patch.to_dict(),
            "metadata": _patch_metadata(runtime.state, patch.commit_hash),
            "summary": description,
        }
        click.echo(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    for patch in patches:
        click.echo(f"{patch.patch_id} {patch.commit_hash[:10]} {patch.status:<9} {patch.subject}")


@cli.command("accept")
@click.argument("patch_ref")
@click.option("--all", "include_all", is_flag=True, default=False)
@click.option("--config", "config_value", default="architect.toml", show_default=True)
def accept_command(patch_ref: str, include_all: bool, config_value: str) -> None:
    repo_root = Path.cwd().resolve()
    runtime = _load_runtime(repo_root, _resolve_config_path(repo_root, config_value))
    scope = None if include_all else (_session_commit_scope(runtime.state) or None)
    patch = runtime.patches.resolve_patch(patch_ref, commit_hashes=scope)
    if patch is None:
        raise click.ClickException(f"Patch not found: {patch_ref}")

    patch_files = runtime.patches.changed_files_for_commit(patch.commit_hash)
    _ensure_patch_allowed(patch_files, runtime.config)

    runtime.patches.update_patch_status(patch.commit_hash, "accepted")
    _record_patch_metric(runtime.state, "accepted_patches", patch.commit_hash)
    runtime.state.add_decision(
        {
            "id": f"dec-accept-{patch.commit_hash[:8]}",
            "topic": "patch_lifecycle",
            "decided_by": "user",
            "approved_by": "supervisor",
            "decision": f"Accepted patch {patch.patch_id}",
            "rationale": "Patch passed review and guardrail validation.",
            "created_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
        }
    )
    click.echo(f"Accepted {patch.patch_id} ({patch.commit_hash[:10]})")


@cli.command("reject")
@click.argument("patch_ref")
@click.option("--all", "include_all", is_flag=True, default=False)
@click.option("--config", "config_value", default="architect.toml", show_default=True)
def reject_command(patch_ref: str, include_all: bool, config_value: str) -> None:
    repo_root = Path.cwd().resolve()
    runtime = _load_runtime(repo_root, _resolve_config_path(repo_root, config_value))
    scope = None if include_all else (_session_commit_scope(runtime.state) or None)
    try:
        patch = runtime.patches.reject_patch(patch_ref, commit_hashes=scope)
    except ArchitectStateError as exc:
        raise click.ClickException(str(exc)) from exc

    _record_patch_metric(runtime.state, "rejected_patches", patch.commit_hash)
    runtime.state.add_decision(
        {
            "id": f"dec-reject-{patch.commit_hash[:8]}",
            "topic": "patch_lifecycle",
            "decided_by": "user",
            "approved_by": "supervisor",
            "decision": f"Rejected patch {patch.patch_id}",
            "rationale": "Patch removed from stack via reject workflow.",
            "created_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
        }
    )
    click.echo(f"Rejected {patch.patch_id} ({patch.commit_hash[:10]})")


@cli.command("modify")
@click.argument("patch_ref")
@click.option("--all", "include_all", is_flag=True, default=False)
@click.option("--config", "config_value", default="architect.toml", show_default=True)
def modify_command(patch_ref: str, include_all: bool, config_value: str) -> None:
    repo_root = Path.cwd().resolve()
    runtime = _load_runtime(repo_root, _resolve_config_path(repo_root, config_value))
    scope = None if include_all else (_session_commit_scope(runtime.state) or None)
    patch = runtime.patches.resolve_patch(patch_ref, commit_hashes=scope)
    if patch is None:
        raise click.ClickException(f"Patch not found: {patch_ref}")

    branch_name = None
    if runtime.patches.git_enabled:
        branch_name = (
            f"architect/amend-{patch.patch_id}-{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}"
        )
        runtime.patches.create_branch(branch_name, start_point=patch.commit_hash)

    tasks = runtime.state.get_tasks()
    tasks.append(
        {
            "id": f"task-modify-{patch.commit_hash[:8]}",
            "type": "implement",
            "assigned_to": "coder",
            "description": f"Amend patch {patch.patch_id} ({patch.commit_hash[:10]}).",
            "status": "pending",
            "depends_on": [],
            "created_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
            "attempt": 0,
        }
    )
    runtime.state.set_tasks(tasks)

    runtime.patches.update_patch_status(patch.commit_hash, "modified")
    runtime.state.add_decision(
        {
            "id": f"dec-modify-{patch.commit_hash[:8]}",
            "topic": "patch_modification",
            "decided_by": "user",
            "approved_by": "supervisor",
            "decision": f"Modify patch {patch.patch_id}",
            "rationale": "Manual patch modification requested.",
            "created_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
        }
    )

    context = runtime.state.get_context()
    context["phase"] = "implementation"
    context["status"] = "in_progress"
    if branch_name:
        context["active_branch"] = branch_name
    runtime.state.set_context(context)

    message = f"Marked {patch.patch_id} for modification."
    if branch_name:
        message += f" Amendment branch: {branch_name}"
    click.echo(message)


@cli.command("rollback")
@click.argument("checkpoint_id")
@click.option("--config", "config_value", default="architect.toml", show_default=True)
def rollback_command(checkpoint_id: str, config_value: str) -> None:
    repo_root = Path.cwd().resolve()
    runtime = _load_runtime(repo_root, _resolve_config_path(repo_root, config_value))
    try:
        branch_name = runtime.patches.rollback(checkpoint_id)
    except ArchitectStateError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"Checked out rollback branch: {branch_name}")


@cli.command("checkpoints")
@click.option("--config", "config_value", default="architect.toml", show_default=True)
def checkpoints_command(config_value: str) -> None:
    repo_root = Path.cwd().resolve()
    runtime = _load_runtime(repo_root, _resolve_config_path(repo_root, config_value))
    checkpoints = runtime.patches.list_checkpoints()
    if not checkpoints:
        click.echo("No checkpoints found.")
        return
    for checkpoint in checkpoints:
        click.echo(checkpoint)


@cli.command("backend")
@click.argument("backend_name", type=click.Choice(["codex", "claude"]))
@click.option("--config", "config_value", default="architect.toml", show_default=True)
def backend_command(backend_name: str, config_value: str) -> None:
    repo_root = Path.cwd().resolve()
    config_path = _resolve_config_path(repo_root, config_value)
    config = load_config(config_path)
    config.backend.primary = backend_name  # type: ignore[assignment]
    save_config(config_path, config)
    click.echo(f"Primary backend set to {backend_name}")
