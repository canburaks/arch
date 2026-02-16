from pathlib import Path

from architect.config import ArchitectConfig, dumps_toml, load_config, save_config


def test_config_roundtrip(tmp_path: Path) -> None:
    config_path = tmp_path / "architect.toml"
    config = ArchitectConfig.default()
    config.project.name = "architect-test"
    config.backend.primary = "codex"
    config.backend.max_retries = 3
    config.project.type_check_command = "python -m compileall src tests"

    save_config(config_path, config)
    loaded = load_config(config_path)

    assert loaded.project.name == "architect-test"
    assert loaded.backend.primary == "codex"
    assert loaded.workflow.max_patches_before_review == 5
    assert loaded.backend.max_retries == 3
    assert "compileall" in loaded.project.type_check_command


def test_toml_dump_contains_backend_retry_fields() -> None:
    rendered = dumps_toml(ArchitectConfig.default())

    assert "max_retries" in rendered
    assert "retry_backoff_seconds" in rendered
    assert "timeout_seconds" in rendered
