# Architect Docs

Architect is a CLI-first multi-agent workflow for producing and reviewing stacked patches.

## Install

```bash
pip install archcli
```

## Quickstart

1. Initialize repository configuration:

```bash
arch init
```

2. Run a goal end-to-end:

```bash
arch run "Implement feature X"
```

3. Inspect lifecycle and gate outcomes:

```bash
arch status --verbose
```

4. Review and manage patches:

```bash
arch review
arch review --patch patch-<id>
arch accept patch-<id>
arch modify patch-<id>
arch reject patch-<id>
```

5. Work with checkpoints:

```bash
arch checkpoints
arch rollback architect/<checkpoint-tag>
```

## Runtime Guarantees

- Dynamic planning to implementation task graph.
- Executable quality gates with persisted command artifacts.
- Guardrail enforcement from `architect.toml`.
- Shared state persisted in git notes (fallback `.architect/state`).
- Stable patch IDs and auditable lifecycle transitions.

## Build and PyPI Publish Requirements

The project is now wired for a standard PyPI release flow. Requirements:

1. Packaging metadata is defined in `pyproject.toml` (`name`, `version`, `readme`, Python requirement, classifiers, URLs, entrypoint).
2. Build backend is configured (`hatchling`) and package assets are included in distributions.
3. Release tooling is available via optional dependency group `release` (`build`, `twine`).
4. Distributions pass validation via `twine check`.
5. GitHub Actions publishes tag releases (`v*`) to PyPI using trusted publishing.

One-time setup outside this repo:

1. Create the `archcli` project on PyPI (or reserve the name).
2. Configure a trusted publisher in PyPI for this repository/workflow.

Local release commands:

```bash
uv sync --extra release
uv build
uv run --extra release twine check dist/*
```
