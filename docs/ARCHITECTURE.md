# Architecture

## Main Components

- `src/architect/cli.py`: command entrypoints and runtime wiring.
- `src/architect/supervisor.py`: task orchestration and quality gates.
- `src/architect/specialists/`: role-specific agent wrappers.
- `src/architect/backends/`: Codex/Claude adapters and resilient fallback wrapper.
- `src/architect/state/`: shared state store (`notes` / `branch` / `local`) and patch stack manager.

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
- Review/accept/reject/modify are session-scoped by default (current run patch queue).
- Reject is non-destructive (`git revert` strategy).
- Rollback creates a dedicated rollback branch instead of hard-resetting current branch.

## Quality Gates

- `planning_gate`: requires actionable plan output (non-empty structured steps).
- `implementation_gate`: lint/type-check commands + file-count and forbidden-path guardrails.
- `testing_gate`: test command execution + optional coverage threshold.
- `review_gate`: critic severity parsing + `require_tests_for` guardrail.
- `documentation_gate`: non-empty output plus documentation-impact verification for source changes.

All gate checks persist artifacts and failure reasons into metrics.
