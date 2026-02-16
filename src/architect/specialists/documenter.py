from __future__ import annotations

from architect.specialists.base import SpecialistAgent


class DocumenterAgent(SpecialistAgent):
    role = "documenter"
    prompt_file = "documenter.md"
    fallback_prompt = """
You are the Documenter/Technical Writer specialist.
Maintain concise and accurate technical documentation and changelog quality.
""".strip()
