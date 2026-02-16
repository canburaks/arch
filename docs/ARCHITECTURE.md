# Architecture

## Main Components

- `src/architect/cli.py`: command entrypoints and runtime wiring.
- `src/architect/supervisor.py`: task orchestration and quality gates.
- `src/architect/specialists/`: role-specific agent wrappers.
- `src/architect/backends/`: Codex/Claude adapters and resilient fallback wrapper.
- `src/architect/state/`: shared state store (git notes + local fallback) and patch stack manager.

## Run Session Model

`context.session` persists:

- `run_id`
- `goal`
- `base_branch`
- `active_branch`
- `started_at` / `ended_at`
- `phase_history`
- `patch_stack`

This session is updated during each task transition.

## Shared State Schema

Each namespace payload is stored in an envelope:

- `schema_version`
- `revision`
- `updated_at`
- `data`

Legacy payloads are read and migrated transparently on next write.

## Patch Lifecycle

- Patches receive stable IDs: `patch-<commit-prefix>`.
- Lifecycle status is persisted (`pending`, `accepted`, `modified`, `rejected`).
- Patch metadata links task IDs, run IDs, and checkpoints.

## Quality Gates

- `planning_gate`: non-empty planner output.
- `implementation_gate`: lint/type-check commands + per-patch file-change guardrail.
- `testing_gate`: test command execution.
- `review_gate`: critic severity parsing + test coverage guardrail.
- `documentation_gate`: non-empty documentation output.

All gate checks persist artifacts and failure reasons into metrics.
