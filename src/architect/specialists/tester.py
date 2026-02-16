from __future__ import annotations

from architect.specialists.base import SpecialistAgent


class TesterAgent(SpecialistAgent):
    role = "tester"
    prompt_file = "tester.md"
    fallback_prompt = """
You are the Tester/QA specialist.
Design and run tests for happy path, edge cases, and failures.
Report clear pass/fail outcomes.
""".strip()
