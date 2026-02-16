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
- CLI install smoke check

If your change affects packaging or entrypoints, ensure smoke checks still pass.
