# Architect Docs

Architect is a CLI-first multi-agent workflow for producing and reviewing stacked patches.

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
