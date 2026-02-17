import tomllib
from pathlib import Path

from architect import __version__
from architect.config import ArchitectConfig, dumps_toml, load_config, save_config


def test_config_roundtrip(tmp_path: Path) -> None:
    config_path = tmp_path / "architect.toml"
    config = ArchitectConfig.default()
    config.project.name = "architect-test"
    config.backend.primary = "codex"
    config.backend.max_retries = 3
    config.project.type_check_command = "python -m compileall src tests"
    config.workflow.test_coverage_threshold = 80
    config.workflow.max_parallel_tasks = 3
    config.workflow.plan_requires_critic = True
    config.workflow.branch_strategy = "auxiliary_branches"
    config.workflow.dirty_worktree_mode = "isolate"
    config.workflow.review_docs_patterns = ["guides/**"]
    config.workflow.review_changelog_patterns = ["history/**"]
    config.state.backend = "branch"
    config.state.branch_ref = "architect/state-test"

    save_config(config_path, config)
    loaded = load_config(config_path)

    assert loaded.project.name == "architect-test"
    assert loaded.backend.primary == "codex"
    assert loaded.workflow.max_patches_before_review == 5
    assert loaded.backend.max_retries == 3
    assert "compileall" in loaded.project.type_check_command
    assert loaded.workflow.test_coverage_threshold == 80
    assert loaded.workflow.max_parallel_tasks == 3
    assert loaded.workflow.plan_requires_critic is True
    assert loaded.workflow.branch_strategy == "auxiliary_branches"
    assert loaded.workflow.dirty_worktree_mode == "isolate"
    assert loaded.workflow.review_docs_patterns == ["guides/**"]
    assert loaded.workflow.review_changelog_patterns == ["history/**"]
    assert loaded.state.backend == "branch"
    assert loaded.state.branch_ref == "architect/state-test"


def test_toml_dump_contains_backend_retry_fields() -> None:
    rendered = dumps_toml(ArchitectConfig.default())

    assert "max_retries" in rendered
    assert "retry_backoff_seconds" in rendered
    assert "timeout_seconds" in rendered
    assert "test_coverage_threshold" in rendered
    assert "max_parallel_tasks" in rendered
    assert "branch_strategy" in rendered
    assert "fallback_artifact_mode" in rendered
    assert "dirty_worktree_mode" in rendered
    assert "review_docs_patterns" in rendered
    assert "review_changelog_patterns" in rendered
    assert "[state]" in rendered


def test_package_version_constant_matches_pyproject() -> None:
    project_root = Path(__file__).resolve().parents[1]
    pyproject = tomllib.loads((project_root / "pyproject.toml").read_text(encoding="utf-8"))

    assert __version__ == pyproject["project"]["version"]
