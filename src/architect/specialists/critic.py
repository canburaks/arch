from __future__ import annotations

from architect.specialists.base import SpecialistAgent


class CriticAgent(SpecialistAgent):
    role = "critic"
    prompt_file = "critic.md"
    fallback_prompt = """
You are the Critic/Code Reviewer specialist.
Find correctness, maintainability, and security issues.
Classify findings as BLOCKER, MAJOR, MINOR, or SUGGESTION.
""".strip()
