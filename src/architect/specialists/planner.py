from __future__ import annotations

from architect.specialists.base import SpecialistAgent


class PlannerAgent(SpecialistAgent):
    role = "planner"
    prompt_file = "planner.md"
    fallback_prompt = """
You are the Planner/Architect specialist.
Analyze requirements, define interfaces, propose implementation steps,
and provide risks with alternatives.
You produce plans, not code.
""".strip()
