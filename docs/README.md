# Architect Docs

Architect is a CLI-first multi-agent workflow for producing and reviewing stacked patches with
Supervisor + Specialists orchestration.

## Quickstart

1. Initialize repository configuration:

```bash
arch init
```

2. Run a goal end-to-end:

```bash
arch run "Implement feature X"
```

Resume an interrupted run graph:

```bash
arch run "Implement feature X" --resume-run
```

Control runtime behavior per run:

```bash
arch run "Implement feature X" --max-parallel-tasks 2 --autonomous
arch run "Implement feature X" --manual
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

`arch accept` now performs git finalization (tag + queue metadata), and `arch reject` automatically
queues retry implementation tasks for the next run.

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

7. Backend selection:

```bash
arch backend auto
arch backend codex
arch backend codex_sdk
arch backend claude
```

## Runtime Guarantees

- Dynamic planning to dependency-safe task graph.
- Supervisor decomposition prompt is executed before planner decomposition.
- Structured planning/review gates with persisted artifacts.
- Guardrails enforced before patch commit creation.
- Accept/reject/modify lifecycle is auditable through patch metadata and decisions.
- Reject workflow enqueues bounded retry tasks with traceability to origin patch.
- Run/lease namespaces provide heartbeat visibility for autonomous recovery.
- Shared state persisted in configurable backend:
  - git notes (`state.backend = "notes"`)
  - dedicated branch (`state.backend = "branch"`)
  - local fallback (`state.backend = "local"`)
- Stable patch IDs and auditable lifecycle transitions.

## Migration

See `docs/MIGRATION.md` for migration details between the previous sequential runtime and the
current orchestration model.
