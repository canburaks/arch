# AGENT.md
## Purpose
This repository contains **Architect**, a CLI-based multi-agent software engineering framework.
The system coordinates a Supervisor + Specialists workflow, stores shared state in git notes, and manages work as stacked patches.

## Core Objectives
- Keep changes **atomic, reviewable, and reversible**.
- Enforce **quality gates** before progressing phases.
- Maintain an auditable state trail in `refs/notes/architect/*` (or `.architect/state` fallback).
- Support both `codex` and `claude` backends via a unified interface.

## Repository Layout
- `src/architect/cli.py`: CLI entrypoints and command handling.
- `src/architect/supervisor.py`: orchestration logic.
- `src/architect/specialists/`: planner, coder, tester, critic, documenter agents.
- `src/architect/backends/`: backend abstraction + Codex/Claude integrations.
- `src/architect/state/`: git notes store + patch stack manager.
- `src/architect/prompts/`: specialist/supervisor prompt templates.
- `tests/`: unit and integration-oriented tests.

## Required Engineering Rules
1. Keep modifications minimal and scoped to the task.
2. Do not introduce unrelated refactors.
3. One logical unit of change should map to one patch.
4. Persist decisions/tasks/checkpoints in shared state.
5. Never bypass configured guardrails in `architect.toml`.

## Runtime Workflow (Expected)
1. `arch init` initializes config and state.
2. `arch run "<goal>"` decomposes goal and coordinates specialists.
3. Supervisor enforces planning/implementation/testing/review gates.
4. Patches are reviewed through `arch review`.
5. User finalizes through `arch accept` / `arch reject` / `arch modify`.

## Quality Gate Expectations
- **Planning gate**: clear implementation strategy and interfaces.
- **Implementation gate**: code quality checks pass and patch constraints hold.
- **Testing gate**: tests pass and regressions are not introduced.
- **Review gate**: no blocker findings from critic.

## Development Commands
- Run tests: `python -m pytest tests`
- Lint: `python -m ruff check src tests`
- Syntax validation fallback: `python -m compileall src tests`

## Definition of Done
- Behavior implemented according to task scope.
- Relevant tests added/updated and passing.
- Lint/type/syntax checks passing.
- State/patch metadata remains consistent.
- Output is clean, deterministic, and review-ready.
