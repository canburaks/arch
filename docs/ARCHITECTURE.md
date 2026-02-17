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

`runs` namespace persists run-scoped lifecycle telemetry (`status`, `heartbeat_at`, retries,
checkpoint linkage, branch strategy).

`leases` namespace persists active run lease/heartbeat metadata to protect long-running autonomous
execution from overlap.

This session is updated during each task transition.

## Shared State Schema

Each namespace payload is stored in an envelope:

- `schema_version`
- `revision`
- `updated_at`
- `data`

Legacy payloads are read and migrated transparently on next write.

Additional namespaces used by runtime:

- `runs`
- `leases`

## Patch Lifecycle

- Patches receive stable IDs: `patch-<commit-prefix>`.
- Lifecycle status is persisted (`pending`, `accepted`, `modified`, `rejected`).
- Patch metadata links task IDs, run IDs, and checkpoints.
- Accepted patches are finalized with git tags under `architect/accepted/*`.
- Review/accept/reject/modify are session-scoped by default (current run patch queue).
- Reject queues retry implementation tasks and attempts non-destructive revert.
- Rollback creates a dedicated rollback branch instead of hard-resetting current branch.

## Quality Gates

- `planning_gate`: requires actionable plan output with design-quality signals.
- `implementation_gate`: lint/type-check commands + file-count and forbidden-path guardrails
  (enforced pre-commit and validated in gate artifacts).
- `testing_gate`: test command execution + optional coverage threshold.
- `review_gate`: critic severity parsing, major-threshold policy, `require_tests_for`, and optional
  docs/changelog file-evidence checks.
- `documentation_gate`: non-empty output plus documentation-impact verification for source changes.

All gate checks persist artifacts and failure reasons into metrics.

## Runtime Controls

Config and CLI support:

- `workflow.max_parallel_tasks`
- `workflow.task_max_attempts`
- `workflow.task_retry_backoff_seconds`
- `workflow.max_conflict_cycles`
- `workflow.plan_requires_critic`
- `workflow.review_max_major_findings`
- `workflow.review_require_docs_update`
- `workflow.review_require_changelog_update`
- `workflow.branch_strategy` (`single_branch_queue` or `auxiliary_branches`)
- `workflow.fallback_artifact_mode` (`local_only` or `tracked`)

Default behavior favors minimal repository footprint (`local_only` fallback artifacts).
