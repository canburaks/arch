# Contributing

## Development

Install dependencies and run checks:

```bash
uv sync --extra dev
uv run --extra dev ruff check src tests
uv run --extra dev pytest -q
python -m compileall src tests
```

## Patch Scope Rules

- Keep changes focused and atomic.
- Prefer one logical behavior change per patch.
- Preserve state/audit trail quality.
- Avoid bypassing configured guardrails.

## Testing Expectations

Contributions should include:

- Unit coverage for new modules.
- Integration coverage for CLI flows where behavior changes.
- Failure-path assertions for gates/state/lifecycle when relevant.

## Release Hygiene

CI validates:

- Lint
- Tests
- Package build
- Package metadata validation (`twine check`)
- CLI install smoke check

If your change affects packaging or entrypoints, ensure smoke checks still pass.

## PyPI Publishing

The repository includes `.github/workflows/publish.yml`, which:

1. Triggers on `v*` tags.
2. Verifies tag version matches `pyproject.toml`.
3. Builds distributions.
4. Validates distributions with `twine check`.
5. Publishes to PyPI via trusted publishing.

Before first publish, configure trusted publishing on PyPI for this repository.
