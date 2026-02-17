# Migration Guide

This guide summarizes behavior changes introduced by the orchestration/runtime upgrade.

## 1. Patch Lifecycle

- `arch accept <patch-id>` now performs git finalization:
  - Creates/updates `architect/accepted/<patch-id>` tag.
  - Persists finalization metadata in patch stack metrics.
- `arch reject <patch-id>` now queues retry tasks automatically:
  - Creates `task-retry-*` entries linked to the rejected patch.
  - Retry tasks are executed by `arch run` in subsequent runs.

## 2. Branch Strategy

`workflow.branch_strategy` controls runtime branching behavior:

- `single_branch_queue` (default):
  - Runs stay on the current branch.
  - `modify` stays on current branch.
- `auxiliary_branches`:
  - Runs create `architect/run-*` branches.
  - `modify` can create `architect/amend-*` branches.

## 3. Guardrails and Gates

- Forbidden-path and file-count guardrails are enforced pre-commit.
- Planning gate now validates plan quality signals beyond non-empty output.
- Review gate supports:
  - major finding threshold (`review_max_major_findings`),
  - docs update enforcement (`review_require_docs_update`),
  - changelog update enforcement (`review_require_changelog_update`).

## 4. Resilience and Recovery

- Task retry behavior is configurable:
  - `task_max_attempts`
  - `task_retry_backoff_seconds`
- Failure checkpoints are created automatically on unrecovered gate failure.
- `runs` and `leases` namespaces persist heartbeat and run state metadata for recovery visibility.

## 5. Fallback Artifact Footprint

- Default mode is `workflow.fallback_artifact_mode = "local_only"`:
  - No tracked fallback docs are added by default.
  - Empty commits are used when no staged code delta exists.
- Optional `tracked` mode can persist fallback artifacts under `tracked_fallback_dir`.

## 6. Prompt Contracts

Specialist prompts now use strict, structured output contracts to reduce orchestration ambiguity.
