from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

BackendName = Literal["codex", "claude"]
StateBackendName = Literal["notes", "branch", "local"]


@dataclass(slots=True)
class ProjectConfig:
    name: str = "my-project"
    language: str = "python"
    test_command: str = "uv run --extra dev pytest -q"
    lint_command: str = "uv run --extra dev ruff check src tests"
    type_check_command: str = "python -m compileall src tests"


@dataclass(slots=True)
class BackendConfig:
    primary: BackendName = "claude"
    fallback: BackendName = "codex"
    max_retries: int = 1
    retry_backoff_seconds: float = 0.5
    timeout_seconds: float = 90.0


@dataclass(slots=True)
class AgentsConfig:
    supervisor_model: str = "claude-sonnet-4-5"
    specialist_model: str = "claude-sonnet-4-5"


@dataclass(slots=True)
class WorkflowConfig:
    max_patches_before_review: int = 5
    auto_test: bool = True
    auto_lint: bool = True
    require_critic_approval: bool = True
    test_coverage_threshold: int = 0


@dataclass(slots=True)
class GuardrailsConfig:
    max_file_changes_per_patch: int = 10
    forbidden_paths: list[str] = field(
        default_factory=lambda: [".env", "secrets/*", "production.config.*"]
    )
    require_tests_for: list[str] = field(default_factory=lambda: ["src/**/*.py"])


@dataclass(slots=True)
class StateConfig:
    backend: StateBackendName = "notes"
    branch_ref: str = "architect/state"


@dataclass(slots=True)
class ArchitectConfig:
    project: ProjectConfig = field(default_factory=ProjectConfig)
    backend: BackendConfig = field(default_factory=BackendConfig)
    agents: AgentsConfig = field(default_factory=AgentsConfig)
    workflow: WorkflowConfig = field(default_factory=WorkflowConfig)
    guardrails: GuardrailsConfig = field(default_factory=GuardrailsConfig)
    state: StateConfig = field(default_factory=StateConfig)

    @classmethod
    def default(cls) -> ArchitectConfig:
        return cls()

    @classmethod
    def from_dict(cls, data: dict) -> ArchitectConfig:
        return cls(
            project=ProjectConfig(**data.get("project", {})),
            backend=BackendConfig(**data.get("backend", {})),
            agents=AgentsConfig(**data.get("agents", {})),
            workflow=WorkflowConfig(**data.get("workflow", {})),
            guardrails=GuardrailsConfig(**data.get("guardrails", {})),
            state=StateConfig(**data.get("state", {})),
        )

    def to_dict(self) -> dict:
        return {
            "project": {
                "name": self.project.name,
                "language": self.project.language,
                "test_command": self.project.test_command,
                "lint_command": self.project.lint_command,
                "type_check_command": self.project.type_check_command,
            },
            "backend": {
                "primary": self.backend.primary,
                "fallback": self.backend.fallback,
                "max_retries": self.backend.max_retries,
                "retry_backoff_seconds": self.backend.retry_backoff_seconds,
                "timeout_seconds": self.backend.timeout_seconds,
            },
            "agents": {
                "supervisor_model": self.agents.supervisor_model,
                "specialist_model": self.agents.specialist_model,
            },
            "workflow": {
                "max_patches_before_review": self.workflow.max_patches_before_review,
                "auto_test": self.workflow.auto_test,
                "auto_lint": self.workflow.auto_lint,
                "require_critic_approval": self.workflow.require_critic_approval,
                "test_coverage_threshold": self.workflow.test_coverage_threshold,
            },
            "guardrails": {
                "max_file_changes_per_patch": self.guardrails.max_file_changes_per_patch,
                "forbidden_paths": list(self.guardrails.forbidden_paths),
                "require_tests_for": list(self.guardrails.require_tests_for),
            },
            "state": {
                "backend": self.state.backend,
                "branch_ref": self.state.branch_ref,
            },
        }


def _toml_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        rendered = f"{value:.3f}".rstrip("0").rstrip(".")
        return rendered if rendered else "0"
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    return json.dumps(str(value), ensure_ascii=False)


def dumps_toml(config: ArchitectConfig) -> str:
    data = config.to_dict()
    lines: list[str] = []
    section_order = ["project", "backend", "agents", "workflow", "guardrails", "state"]
    for section in section_order:
        lines.append(f"[{section}]")
        for key, value in data[section].items():
            lines.append(f"{key} = {_toml_value(value)}")
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def load_config(path: Path) -> ArchitectConfig:
    if not path.exists():
        return ArchitectConfig.default()
    return ArchitectConfig.from_dict(tomllib.loads(path.read_text(encoding="utf-8")))


def save_config(path: Path, config: ArchitectConfig) -> None:
    path.write_text(dumps_toml(config), encoding="utf-8")
