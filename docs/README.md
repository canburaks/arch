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
arch review --all
arch accept patch-<id>
arch modify patch-<id>
arch reject patch-<id>
```

5. Control long-running workflow:

```bash
arch pause
arch resume
```

6. Work with checkpoints:

```bash
arch checkpoints
arch rollback architect/<checkpoint-tag>
arch resume --from-checkpoint architect/<checkpoint-tag> --goal "Continue goal"
```

## Runtime Guarantees

- Dynamic planning to implementation task graph.
- Supervisor decomposition prompt is executed before planner decomposition.
- Executable quality gates with persisted command artifacts.
- Guardrail enforcement from `architect.toml`.
- Shared state persisted in configurable backend:
  - git notes (`state.backend = "notes"`)
  - dedicated branch (`state.backend = "branch"`)
  - local fallback (`state.backend = "local"`)
- Stable patch IDs and auditable lifecycle transitions.
