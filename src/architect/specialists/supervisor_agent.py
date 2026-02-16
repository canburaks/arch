from __future__ import annotations

from architect.specialists.base import SpecialistAgent


class SupervisorAgent(SpecialistAgent):
    role = "supervisor"
    prompt_file = "supervisor.md"
    fallback_prompt = """
You are the Supervisor of a multi-agent software development team.
Decompose goals into tasks, coordinate specialists, and enforce quality gates.
You do not write production code directly.
""".strip()
