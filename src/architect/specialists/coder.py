from __future__ import annotations

from architect.specialists.base import SpecialistAgent


class CoderAgent(SpecialistAgent):
    role = "coder"
    prompt_file = "coder.md"
    fallback_prompt = """
You are the Coder/Engineer specialist.
Implement exactly what was planned.
Match repository conventions and keep commits atomic.
""".strip()
